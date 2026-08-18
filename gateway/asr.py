"""ASR models + transcription pipeline, ported verbatim from serve_dual.py.

Covers: model registry / lazy loading, FunASR transcription, SRT rendering,
Opus decoding (ctypes binding to libopus), WAV validation, and the ferrum
response helpers.
"""

import copy
import ctypes
import ctypes.util
import gc
import logging
import os
import re
import struct
import threading
import time
from collections import OrderedDict

from .config import get_cfg

logger = logging.getLogger(__name__)

MODEL_REGISTRY = OrderedDict()
_MODEL_LOCK = threading.RLock()
_OPUS_DECODER = None

MODEL_CONFIGS = {
    "sensevoice": {
        "model": "iic/SenseVoiceSmall",
        "vad_model": "fsmn-vad",
        "vad_kwargs": {"max_single_segment_time": 30000},
        # Advisory: which language codes the model accepts as a hint (zh/en/ja/
        # ko/yue...). Not enforced — unknown values fall back to auto-detect.
        "languages": ["zh", "en", "ja", "ko", "yue", "auto"],
    },
    "fun-asr-mlt-nano": {
        "model": "FunAudioLLM/Fun-ASR-MLT-Nano-2512",
        "hub": "hf",
        "trust_remote_code": True,
        # FunASRNano stores BF16 under llm_conf, while its inference path reads
        # a flat runtime key. Keep the override device-specific because full
        # BF16 was only verified for the LLM on macOS MPS.
        "llm_dtype_by_device": {"mps": "bf16"},
        "vad_model": "fsmn-vad",
        "vad_kwargs": {"max_single_segment_time": 30000},
        "languages": ["zh", "en", "ja", "ko", "yue", "auto"],
    },
}


def _runtime_llm_dtype(model_name: str) -> str | None:
    """Return the verified LLM dtype override for the active device."""
    mapping = MODEL_CONFIGS[model_name].get("llm_dtype_by_device", {})
    device_type = str(get_cfg().device).split(":", 1)[0]
    return mapping.get(device_type)


def _release_mps_cache(*, force: bool = False) -> None:
    """Return currently unused PyTorch MPS allocator blocks to macOS."""
    cfg = get_cfg()
    if not str(cfg.device).startswith("mps"):
        return
    if not force and not cfg.mps_empty_cache:
        return

    try:
        import torch

        if torch.backends.mps.is_available():
            # generate() is synchronous here, but synchronization also makes
            # cleanup reliable after an inference exception.
            torch.mps.synchronize()
            torch.mps.empty_cache()
    except Exception as exc:  # cleanup must never turn a valid response into 500
        logger.debug("Unable to release MPS cache: %s", exc)


def accelerator_memory_stats() -> dict:
    """Return lightweight accelerator counters for the health endpoint."""
    if not str(get_cfg().device).startswith("mps"):
        return {}

    try:
        import torch

        mib = 1024 * 1024
        current = torch.mps.current_allocated_memory()
        driver = torch.mps.driver_allocated_memory()
        return {
            "backend": "mps",
            "current_allocated_mib": round(current / mib, 1),
            "driver_allocated_mib": round(driver / mib, 1),
            "driver_overhead_mib": round(max(driver - current, 0) / mib, 1),
            "recommended_max_mib": round(torch.mps.recommended_max_memory() / mib, 1),
        }
    except Exception as exc:
        logger.debug("Unable to read MPS memory counters: %s", exc)
        return {"backend": "mps"}


def _make_room_for_model() -> None:
    """Evict least-recently-used models before loading a new one."""
    limit = get_cfg().max_loaded_models
    if limit <= 0:
        return

    evicted = []
    while len(MODEL_REGISTRY) >= limit:
        name, model = MODEL_REGISTRY.popitem(last=False)
        evicted.append(name)
        del model

    if evicted:
        gc.collect()
        # Model eviction is explicitly intended to reclaim memory, regardless
        # of the normal per-request cache policy.
        _release_mps_cache(force=True)
        logger.info("Evicted resident model(s): %s", evicted)


def load_model(model_name: str):
    """Lazily load a model and keep it in the configured LRU registry."""
    with _MODEL_LOCK:
        if model_name in MODEL_REGISTRY:
            MODEL_REGISTRY.move_to_end(model_name)
            return MODEL_REGISTRY[model_name]

        if model_name not in MODEL_CONFIGS:
            available = list(MODEL_CONFIGS.keys())
            raise ValueError(f"Unknown model '{model_name}'. Available: {available}")

        from funasr import AutoModel

        cfg = copy.deepcopy(MODEL_CONFIGS[model_name])
        # Strip gateway-only metadata before splatting into AutoModel.
        for meta in ("languages", "llm_dtype_by_device"):
            cfg.pop(meta, None)
        cfg["device"] = get_cfg().device
        cfg["disable_update"] = True
        llm_dtype = _runtime_llm_dtype(model_name)
        if llm_dtype:
            cfg["llm_dtype"] = llm_dtype

        # With a one-model limit, unloading first prevents a model switch from
        # temporarily requiring memory for both models at once.
        _make_room_for_model()

        logger.info(f"Loading model '{model_name}' on {cfg['device']}...")
        t0 = time.time()
        model = AutoModel(**cfg)
        elapsed = time.time() - t0
        logger.info(f"Model '{model_name}' loaded in {elapsed:.1f}s")

        MODEL_REGISTRY[model_name] = model
        MODEL_REGISTRY.move_to_end(model_name)
        return model


def clean_text(text: str) -> str:
    """Remove SenseVoice special tags from output."""
    return re.sub(r"<\|[^|]*\|>", "", text).strip()


def run_transcription(
    model_name: str,
    audio_path: str,
    language: str | None,
    sentence_timestamp: bool,
):
    """Run one FunASR transcription. Returns (text, segments, elapsed_seconds)."""
    # Serial inference bounds peak accelerator memory even if the server is
    # later moved to threaded handlers. The current async routes are already
    # effectively serial because FunASR inference is synchronous.
    with _MODEL_LOCK:
        asr_model = load_model(model_name)
        t0 = time.time()

        generate_kwargs = {"input": audio_path, "batch_size": 1}
        if language:
            generate_kwargs["language"] = language
        if sentence_timestamp:
            generate_kwargs["sentence_timestamp"] = True
        llm_dtype = _runtime_llm_dtype(model_name)
        if llm_dtype:
            # FunASR resets request kwargs to a base snapshot on every call;
            # keep this explicit so the decoder can never fall back to FP32.
            generate_kwargs["llm_dtype"] = llm_dtype

        result = None
        try:
            import torch

            # AutoModel uses no_grad internally; inference_mode also disables
            # view/version bookkeeping for this inference-only service.
            with torch.inference_mode():
                result = asr_model.generate(**generate_kwargs)
            elapsed = time.time() - t0

            text = clean_text(result[0]["text"])
            segments = []
            if "sentence_info" in result[0]:
                for seg in result[0]["sentence_info"]:
                    segments.append(
                        {
                            "start": seg.get("start", 0) / 1000.0,
                            "end": seg.get("end", 0) / 1000.0,
                            "text": clean_text(seg.get("text", "")),
                            "speaker": seg.get("spk", None),
                        }
                    )
            return text, segments, elapsed
        finally:
            # Some FunASR result variants carry accelerator tensors. Drop the
            # raw result before releasing unoccupied allocator blocks.
            result = None
            _release_mps_cache()


def _fmt_ts(ms: int) -> str:
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def segments_to_srt(segments: list) -> str:
    """Render segments (seconds) as SRT text, the ferrum protocol response body."""
    if not segments:
        return ""
    lines = []
    for i, seg in enumerate(segments, 1):
        start_ms = max(0, int(round(seg["start"] * 1000)))
        end_ms = max(start_ms + 1, int(round(seg["end"] * 1000)))
        lines.append(f"{i}\n{_fmt_ts(start_ms)} --> {_fmt_ts(end_ms)}\n{seg['text']}")
    return "\n\n".join(lines) + "\n"


class OpusDecoder:
    """ctypes binding to libopus for the ferrum frame stream `[u32_le_len][packet]...`
    (16 kHz mono, 20 ms frames — matches the plugin's SimpleOpusEncoder).
    Self-contained: probes common libopus locations, no Python package needed."""

    def __init__(self):
        candidates = []
        if os.environ.get("OPUS_LIB_DIR"):
            candidates.append(os.path.join(os.environ["OPUS_LIB_DIR"], "libopus.dylib"))
        candidates += ["/opt/homebrew/lib/libopus.dylib", "/usr/local/lib/libopus.dylib"]
        found = ctypes.util.find_library("opus")
        if found:
            candidates.append(found)

        lib = None
        for path in candidates:
            try:
                lib = ctypes.CDLL(path)
                break
            except OSError:
                continue
        if lib is None:
            raise RuntimeError("libopus not found (brew install opus, or set OPUS_LIB_DIR)")

        self._lib = lib
        lib.opus_decoder_create.restype = ctypes.c_void_p
        lib.opus_decoder_create.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        lib.opus_decode.restype = ctypes.c_int
        lib.opus_decode.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_int,
            ctypes.c_int,
        ]
        lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]

    def decode_to_wav(self, payload: bytes) -> bytes:
        rate, channels, frame_size = 16000, 1, 320
        err = ctypes.c_int(0)
        dec = self._lib.opus_decoder_create(rate, channels, ctypes.byref(err))
        if not dec or err.value != 0:
            raise ValueError(f"opus_decoder_create failed: {err.value}")
        try:
            pcm = bytearray()
            out = (ctypes.c_int16 * frame_size)()
            pos = 0
            while pos + 4 <= len(payload):
                (frame_len,) = struct.unpack_from("<I", payload, pos)
                pos += 4
                if pos + frame_len > len(payload):
                    raise ValueError("invalid opus frame length")
                frame = payload[pos : pos + frame_len]
                pos += frame_len
                n = self._lib.opus_decode(dec, frame, frame_len, out, frame_size, 0)
                if n < 0:
                    raise ValueError(f"opus_decode failed: {n}")
                # out[:n] yields signed int16s (negative values break bytes());
                # grab raw little-endian PCM instead.
                pcm.extend(ctypes.string_at(out, n * ctypes.sizeof(ctypes.c_int16)))
            return pcm16_to_wav(bytes(pcm), rate, channels)
        finally:
            self._lib.opus_decoder_destroy(dec)


def get_opus_decoder() -> OpusDecoder:
    """Lazily build the shared OpusDecoder (mirrors the old module-level global)."""
    global _OPUS_DECODER
    if _OPUS_DECODER is None:
        _OPUS_DECODER = OpusDecoder()
    return _OPUS_DECODER


def pcm16_to_wav(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    data_size = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        sample_rate,
        sample_rate * channels * 2,
        channels * 2,
        16,
        b"data",
        data_size,
    )
    return header + pcm


def validate_wav(data: bytes) -> None:
    """Ensure 16 kHz mono 16-bit PCM (what the ferrum client always sends)."""
    import io
    import wave

    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            if (
                w.getnchannels() != 1
                or w.getframerate() != 16000
                or w.getsampwidth() != 2
            ):
                raise ValueError(
                    f"unsupported wav: {w.getnchannels()}ch "
                    f"{w.getframerate()}Hz {w.getsampwidth() * 8}bit"
                )
            if w.getnframes() == 0:
                raise ValueError("wav contains no samples")
    except (wave.Error, EOFError) as e:
        raise ValueError(f"invalid wav: {e}")
