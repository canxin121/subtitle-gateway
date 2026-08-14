"""FastAPI application + routes + entry point (ported from serve_dual.py).

Endpoint paths / methods / bodies / status codes / x-metric-* headers are kept
byte-for-byte; only the app title and the config plumbing changed.
"""

import logging
import os
import tempfile
import time

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from . import asr, translate
from .auth import (
    ferrum_auth_token,
    ferrum_cipher,
    ferrum_decrypt,
    ferrum_encrypt,
)
from .config import apply_cache_env, get_cfg, parse_args, setup_logging
import gateway.config as _config

logger = logging.getLogger(__name__)

app = FastAPI(title="subtitle-gateway", version="1.0.0")


@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    model: str = Form(default="sensevoice"),
    language: str | None = Form(default=None),
    response_format: str | None = Form(default="json"),
    sentence_timestamp: bool = Form(default=False),
):
    """OpenAI-compatible audio transcription endpoint.

    - file: Audio file (wav, mp3, flac, m4a, ogg, webm)
    - model: fun-asr-mlt-nano | sensevoice
    - language: 可选语言提示（ja/zh/en...）
    - response_format: json | verbose_json
    - sentence_timestamp: 为 true 时返回 sentence_info 分段（供字幕插件用）
    """
    if model not in asr.MODEL_CONFIGS:
        return JSONResponse(
            {
                "detail": f"Model '{model}' not found. Available: {list(asr.MODEL_CONFIGS.keys())}"
            },
            status_code=400,
        )

    # Save uploaded file
    suffix = os.path.splitext(file.filename)[1] if file.filename else ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        text, segments, elapsed = asr.run_transcription(
            model, tmp_path, language, sentence_timestamp
        )

        if response_format == "verbose_json":
            return JSONResponse(
                {
                    "text": text,
                    "segments": segments,
                    "language": language or "auto",
                    "duration": round(elapsed, 3),
                    "model": model,
                }
            )
        else:
            return JSONResponse({"text": text})

    except Exception as e:
        logger.error(f"Transcription error: {e}")
        return JSONResponse({"detail": str(e)}, status_code=500)
    finally:
        os.unlink(tmp_path)


@app.post("/transcribe")
async def transcribe_ferrum(request: Request):
    """Ferrum protocol endpoint (custom http backend of the mpv plugin).

    Raw-body POST (no multipart), audio carried as-is in the body. Optional
    headers (all matching the Rust `stt_ferrum` client):
      - x-model:        model id (default "sensevoice")
      - x-language:     language hint (ja/zh/en...; empty/absent = auto-detect)
      - x-compression:  pcm | wav | opus (default pcm)
      - x-encrypted:    "1" + AES-256-GCM encryption (--encryption-key)
      - x-auth-token:   hex(sha256(secret)) when --auth-secret is set
      - x-request-id / x-duration-ms: mirrored in logs / metrics

    Response body is SRT text (optionally encrypted), with metrics headers.
    """
    cfg = get_cfg()  # read at request time (set by main()); defaults before that
    if cfg.auth_secret:
        expected = ferrum_auth_token(cfg.auth_secret)
        actual = request.headers.get("x-auth-token", "")
        if actual != expected:
            return Response(b"unauthorized", status_code=401)
        # Constant-time compare to avoid leaking via timing.
        if len(actual) != len(expected) or sum(
            a != b for a, b in zip(actual, expected)
        ):
            return Response(b"unauthorized", status_code=401)

    model_name = request.headers.get("x-model", "sensevoice")
    if model_name not in asr.MODEL_CONFIGS:
        return Response(
            f"unknown model '{model_name}'".encode(),
            status_code=400,
        )

    # Optional language hint (e.g. "ja"/"zh"/"en"); empty/absent = auto-detect.
    language = request.headers.get("x-language", "").strip() or None

    compression = request.headers.get("x-compression", "pcm")
    duration_ms = request.headers.get("x-duration-ms", "0")
    request_id = request.headers.get("x-request-id", "0")
    encrypted = request.headers.get("x-encrypted") == "1"

    body = await request.body()
    bytes_in = len(body)

    try:
        if encrypted:
            if not cfg.encryption_key:
                return Response(b"encryption not enabled", status_code=400)
            body = ferrum_decrypt(ferrum_cipher(cfg.encryption_key), body)

        if compression == "opus":
            audio_data = asr.get_opus_decoder().decode_to_wav(body)
        elif compression in ("pcm", "wav"):
            audio_data = body
        else:
            return Response(b"unsupported compression", status_code=400)

        asr.validate_wav(audio_data)
    except (ValueError, RuntimeError) as e:
        logger.warning("Ferrum decode error: %s", e)
        return Response(str(e).encode(), status_code=400)

    # Save to a temp WAV so FunASR can open it.
    t0 = time.time()
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
        tmp.write(audio_data)
        tmp_path = tmp.name

    try:
        text, segments, elapsed = asr.run_transcription(
            model_name, tmp_path, language, sentence_timestamp=True
        )
        if not segments and text:
            segments = [{"start": 0, "end": elapsed, "text": text}]
        srt = asr.segments_to_srt(segments)

        if encrypted:
            srt = ferrum_encrypt(ferrum_cipher(cfg.encryption_key), srt.encode())

        headers = {
            "x-metric-queue-ms": "0",
            "x-metric-infer-ms": str(int(elapsed * 1000)),
            "x-metric-worker-ms": str(int(elapsed * 1000)),
            "x-bytes-in": str(bytes_in),
            "x-bytes-out": str(len(srt)),
            # Echo back what was actually used so the client can log/verify.
            "x-model": model_name,
        }
        if language:
            headers["x-language"] = language
        logger.info(
            "Ferrum req %s model=%s language=%s duration_ms=%s wall=%.0fms resp=%dB",
            request_id,
            model_name,
            language or "auto",
            duration_ms,
            elapsed * 1000,
            len(srt),
        )
        return Response(srt, media_type="text/plain", headers=headers)
    except Exception as e:
        logger.error(f"Ferrum transcription error: {e}")
        return Response(str(e).encode(), status_code=500)
    finally:
        os.unlink(tmp_path)


@app.post("/v1/translate")
async def translate_gateway(request: Request):
    """DeepL-compatible translation endpoint (gateway).

    Forwards to the configured upstream DeepL-compatible service
    (--translate-upstream). Auth: the client must send
    `Authorization: DeepL-Auth-Key {key}` when --translate-api-key is set.

    Request body (DeepL format):
      {"text": "hello" | ["hello", "world"], "target_lang": "ZH",
       "source_lang": "EN"}   # source_lang optional (= auto)

    Response:
      {"translations": [{"detected_source_language": "EN", "text": "..."}]}
    """
    authorization = request.headers.get("authorization", "")
    raw = await request.body()
    status, body, headers = await translate.deepl_translate(raw, authorization)
    return JSONResponse(body, status_code=status, headers=headers)


@app.post("/translate")
async def translate_gateway_libretranslate(request: Request):
    """LibreTranslate-compatible translation endpoint (gateway).

    Forwards to the configured upstream LibreTranslate service
    (--libretranslate-upstream). Auth: the client sends `api_key` in the request
    BODY (LibreTranslate does not use an Authorization header) when
    --libretranslate-api-key is set.

    Request body: {"q": "hello" | ["hello", "world"], "source": "auto",
                   "target": "zh", "format": "text", "api_key": "..."}
    Response:  single: {"translatedText": "..."}
               array:  {"translations": [{"translatedText": "..."}]}
    """
    raw = await request.body()
    status, body, headers = await translate.libretranslate_translate(raw)
    return JSONResponse(body, status_code=status, headers=headers)


@app.get("/v1/models")
async def list_models():
    """List available models (OpenAI-compatible)."""
    models = []
    for name, cfg in asr.MODEL_CONFIGS.items():
        models.append(
            {
                "id": name,
                "object": "model",
                "created": 1700000000,
                "owned_by": "funasr",
                "ready": name in asr.MODEL_REGISTRY,
                "languages": cfg.get("languages", []),
            }
        )
    return JSONResponse({"object": "list", "data": models})


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "ok",
        "device": get_cfg().device,
        "models_loaded": list(asr.MODEL_REGISTRY.keys()),
        "models_available": list(asr.MODEL_CONFIGS.keys()),
    }


def main():
    setup_logging()
    cfg = parse_args()
    # Assign to config.CURRENT, not a local server-module global, so get_cfg()
    # (used by request handlers) sees the CLI-parsed config.
    _config.CURRENT = cfg
    # Resolve "auto"/unavailable devices to a runnable one before models load,
    # so pure-CPU servers (no MPS/CUDA) work without touching the config.
    requested = cfg.device
    cfg.device = _config.resolve_device(cfg.device)
    if cfg.device != requested:
        logger.warning(
            "Requested device '%s' unavailable — falling back to '%s'",
            requested,
            cfg.device,
        )
    apply_cache_env(cfg.cache_dir)

    # Pre-load default model(s)
    for name in cfg.preload:
        asr.load_model(name)

    logger.info(f"subtitle-gateway starting on http://{cfg.host}:{cfg.port}")
    logger.info(f"  Device: {cfg.device}")
    logger.info(f"  Cache:  {cfg.cache_dir}")
    logger.info(f"  Models: {list(asr.MODEL_CONFIGS.keys())}")
    if cfg.translate_upstream:
        logger.info(
            f"  Translation upstream: {cfg.translate_upstream} (auth={bool(cfg.translate_api_key)})"
        )
    elif cfg.translate_free != "none":
        logger.info(f"  Translation: free sources [{cfg.translate_free}] (no --translate-upstream)")
    else:
        logger.info("  Translation gateway: disabled (no --translate-upstream)")
    if cfg.libretranslate_upstream:
        logger.info(
            f"  LibreTranslate upstream: {cfg.libretranslate_upstream} (auth={bool(cfg.libretranslate_api_key)})"
        )
    elif cfg.translate_free != "none":
        logger.info(f"  LibreTranslate: free sources [{cfg.translate_free}] (no --libretranslate-upstream)")
    else:
        logger.info("  LibreTranslate gateway: disabled (no --libretranslate-upstream)")
    logger.info(f"  Docs:   http://{cfg.host}:{cfg.port}/docs")

    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
