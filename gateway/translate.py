"""Translation gateway logic (DeepL-compatible + LibreTranslate), ported from
serve_dual.py. Each function decouples the endpoint body (auth / parse /
forward / error mapping / metric headers) from the HTTP envelope so it can be
unit-tested offline. Returns (status_code, json_body, extra_headers).

When no upstream is configured, the endpoints fall back to a list of free
translation sources (--translate-free, default "google,edge") tried in order —
big-company unofficial endpoints only, pure HTTP, no local models / inference:
  - google: deep-translator library against Google's free web endpoint
  - edge:   Microsoft Edge's undocumented /translate/translatetext endpoint
"""

import asyncio
import json
import logging
import time

import httpx

from .auth import deepl_auth_ok, libretranslate_key_ok
from .config import get_cfg

logger = logging.getLogger(__name__)

_TRANSLATE_CLIENT: httpx.AsyncClient | None = None


def translate_client() -> httpx.AsyncClient:
    """Lazily-built shared httpx.AsyncClient (connection pool reused across
    concurrent requests from the plugin's buffer_unordered pipeline)."""
    global _TRANSLATE_CLIENT
    if _TRANSLATE_CLIENT is None:
        _TRANSLATE_CLIENT = httpx.AsyncClient(timeout=30.0)
    return _TRANSLATE_CLIENT


def _metric_headers(bytes_in: int, bytes_out: int, elapsed_ms: int) -> dict:
    return {
        "x-metric-queue-ms": "0",
        "x-metric-infer-ms": str(elapsed_ms),
        "x-metric-worker-ms": str(elapsed_ms),
        "x-bytes-in": str(bytes_in),
        "x-bytes-out": str(bytes_out),
    }


def _google_lang(code: str | None) -> str:
    """Map DeepL/Libre language codes to Google's: lowercase, region variants
    stripped (en-GB->en, pt-BR->pt), Chinese -> zh-CN, empty/'auto' -> auto."""
    c = (code or "").strip().lower()
    if not c or c == "auto":
        return "auto"
    if c.startswith("zh"):
        return "zh-CN"
    return c.split("-")[0]


def _google_error_status(exc: Exception) -> int:
    try:
        from deep_translator import exceptions as dex
    except ImportError:
        return 502
    if isinstance(exc, (dex.LanguageNotSupportedException, dex.NotValidPayload)):
        return 400
    if isinstance(exc, dex.TooManyRequests):
        return 429
    return 502


async def _google_translate(
    texts: list[str], source: str, target: str, bytes_in: int
) -> tuple[int, list[str] | None, str | None, dict]:
    """Translate via deep-translator's GoogleTranslator (free web endpoint,
    pure HTTP, no local inference). deep-translator is sync (requests), so run
    in a thread. Returns (status, translated_list|None, error_msg|None, headers)."""
    try:
        from deep_translator import GoogleTranslator
    except ImportError:
        return (
            503,
            None,
            "google source needs deep-translator: pip install deep-translator",
            {},
        )
    src, tgt = _google_lang(source), _google_lang(target)
    t0 = time.time()

    def _run() -> list[str]:
        return GoogleTranslator(source=src, target=tgt).translate_batch(texts)

    try:
        translated = await asyncio.to_thread(_run)
    except Exception as e:
        logger.warning("Free google translation failed: %s", e)
        return _google_error_status(e), None, f"google translation failed: {e}", {}

    elapsed_ms = int((time.time() - t0) * 1000)
    out = json.dumps(translated, ensure_ascii=False).encode()
    return 200, translated, None, _metric_headers(bytes_in, len(out), elapsed_ms)


_EDGE_URL = "https://edge.microsoft.com/translate/translatetext"


def _edge_lang(code: str | None) -> str:
    """Map DeepL/Libre codes to Edge's BCP-47: lowercase, region stripped,
    Chinese -> zh-Hans (simplified). Empty/'auto' -> "" (Edge auto-detects
    when `from` is empty)."""
    c = (code or "").strip().lower()
    if not c or c == "auto":
        return ""
    if c.startswith("zh"):
        return "zh-Hans"
    return c.split("-")[0]


async def _edge_translate(
    texts: list[str], source: str, target: str, bytes_in: int
) -> tuple[int, list[str] | None, str | None, dict]:
    """Translate via Microsoft Edge's undocumented /translate/translatetext
    endpoint (free, no key, pure HTTP). Body is a bare JSON array; response is
    an array with one {translations:[{text}]} per input, same order."""
    src, tgt = _edge_lang(source), _edge_lang(target)
    params = {"from": src, "to": tgt, "isEnterpriseClient": "false"}
    t0 = time.time()
    try:
        resp = await translate_client().post(
            _EDGE_URL,
            params=params,
            json=texts,
            headers={"Content-Type": "application/json"},
        )
    except httpx.HTTPError as e:
        logger.warning("Edge translation request failed: %s", e)
        return 502, None, f"edge translation request failed: {e}", {}
    elapsed_ms = int((time.time() - t0) * 1000)

    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return 502, None, "invalid edge response", {}
    if resp.status_code >= 400:
        return resp.status_code, None, f"edge error: HTTP {resp.status_code}", {}
    if not isinstance(data, list):
        return 502, None, "invalid edge response: not a list", {}
    try:
        translated = [item["translations"][0]["text"] for item in data]
    except (KeyError, IndexError, TypeError):
        return 502, None, "invalid edge response shape", {}
    if len(translated) != len(texts):
        return 502, None, "edge response count mismatch", {}

    out = json.dumps(translated, ensure_ascii=False).encode()
    return 200, translated, None, _metric_headers(bytes_in, len(out), elapsed_ms)


async def _free_translate(
    texts: list[str], source: str, target: str, bytes_in: int
) -> tuple[int, list[str] | None, str | None, dict]:
    """Try each configured free source (--translate-free, comma-separated) in
    order; the first 200 wins. If one fails (rate-limited / down / unsupported
    pair) it falls through to the next. Returns
    (status, translated_list|None, error_msg|None, headers)."""
    sources = [s.strip() for s in get_cfg().translate_free.split(",") if s.strip()]
    if not sources:
        return 503, None, "no free translation source configured", {}
    last: tuple[int, str] | None = None
    for name in sources:
        if name == "google":
            status, translated, err, headers = await _google_translate(
                texts, source, target, bytes_in
            )
        elif name == "edge":
            status, translated, err, headers = await _edge_translate(
                texts, source, target, bytes_in
            )
        else:
            logger.warning("Unknown free translation source '%s'", name)
            continue
        if status == 200:
            return 200, translated, None, headers
        last = (status, err or "unknown error")
        logger.warning("Free source '%s' failed (%s): %s", name, status, err)
    st, er = last or (502, "all free sources failed")
    return st, None, er, {}


async def deepl_translate(raw: bytes, authorization: str) -> tuple[int, dict, dict]:
    """DeepL-compatible endpoint body. Auth: `Authorization: DeepL-Auth-Key {key}`
    header (when --translate-api-key set). Response `{"translations": [...]}`."""
    cfg = get_cfg()

    if cfg.translate_api_key:
        if not deepl_auth_ok(authorization, cfg.translate_api_key):
            return 401, {"message": "unauthorized"}, {}

    bytes_in = len(raw)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 400, {"message": "invalid JSON body"}, {}

    text = data.get("text")
    if isinstance(text, str):
        texts = [text]
    elif isinstance(text, list) and len(text) > 0 and all(
        isinstance(t, str) for t in text
    ):
        texts = text
    else:
        return 400, {
            "message": "text must be a string or non-empty array of strings"
        }, {}

    target_lang = str(data.get("target_lang", "ZH"))
    source_lang = data.get("source_lang")

    if not cfg.translate_upstream:
        if cfg.translate_free == "none":
            return 503, {"message": "translation upstream not configured"}, {}
        # Free fallback (google): same DeepL response shape so clients are unchanged.
        status, translated, err, headers = await _free_translate(
            texts, str(source_lang) if source_lang else "auto", target_lang, bytes_in
        )
        if status != 200:
            return status, {"message": err}, headers
        detected = (source_lang or "AUTO").upper()
        body = {
            "translations": [
                {"detected_source_language": detected, "text": t} for t in translated
            ]
        }
        return 200, body, headers

    payload = {"text": texts, "target_lang": target_lang}
    if source_lang:
        payload["source_lang"] = str(source_lang)

    headers = {"Content-Type": "application/json"}
    if cfg.translate_upstream_key:
        headers["Authorization"] = f"DeepL-Auth-Key {cfg.translate_upstream_key}"

    upstream_url = cfg.translate_upstream.rstrip("/") + "/v1/translate"
    t0 = time.time()
    try:
        resp = await translate_client().post(upstream_url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        logger.warning("Translation upstream request failed: %s", e)
        return 502, {"message": f"upstream request failed: {e}"}, {}
    elapsed_ms = int((time.time() - t0) * 1000)

    try:
        resp_data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return 502, {"message": "invalid upstream response"}, {}

    if resp.status_code >= 400:
        message = resp_data.get("message", f"HTTP {resp.status_code}")
        logger.warning(
            "Translation upstream error: %s (%d)", message, resp.status_code
        )
        return resp.status_code, {"message": message}, {}

    out = json.dumps(resp_data, ensure_ascii=False).encode()
    return 200, resp_data, _metric_headers(bytes_in, len(out), elapsed_ms)


async def libretranslate_translate(raw: bytes) -> tuple[int, dict, dict]:
    """LibreTranslate-compatible endpoint body. Auth: `api_key` in the request
    BODY (no Authorization header). Response single: `{"translatedText"}`
    or array: `{"translations": [...]}`."""
    cfg = get_cfg()

    bytes_in = len(raw)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 400, {"error": "invalid JSON body"}, {}

    if cfg.libretranslate_api_key:
        provided = data.get("api_key", "")
        if not isinstance(provided, str) or not libretranslate_key_ok(
            provided, cfg.libretranslate_api_key
        ):
            return 401, {"error": "invalid api key"}, {}

    q = data.get("q")
    if isinstance(q, str):
        qs, single = [q], True
    elif isinstance(q, list) and len(q) > 0 and all(isinstance(t, str) for t in q):
        qs, single = q, False
    else:
        return 400, {
            "error": "q must be a string or non-empty array of strings"
        }, {}

    source = str(data.get("source", "auto"))
    target = str(data.get("target", "zh"))

    if not cfg.libretranslate_upstream:
        if cfg.translate_free == "none":
            return 503, {"error": "translation upstream not configured"}, {}
        # Free fallback (google): keep LibreTranslate response shapes.
        status, translated, err, headers = await _free_translate(qs, source, target, bytes_in)
        if status != 200:
            return status, {"error": err}, headers
        if single:
            return 200, {"translatedText": translated[0]}, headers
        return 200, {"translations": [{"translatedText": t} for t in translated]}, headers

    payload = {
        "q": qs[0] if single else qs,
        "source": source,
        "target": target,
        "format": str(data.get("format", "text")),
    }
    if cfg.libretranslate_upstream_key:
        payload["api_key"] = cfg.libretranslate_upstream_key

    upstream_url = cfg.libretranslate_upstream.rstrip("/") + "/translate"
    t0 = time.time()
    try:
        resp = await translate_client().post(upstream_url, json=payload)
    except httpx.HTTPError as e:
        logger.warning("LibreTranslate upstream request failed: %s", e)
        return 502, {"error": f"upstream request failed: {e}"}, {}
    elapsed_ms = int((time.time() - t0) * 1000)

    try:
        resp_data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return 502, {"error": "invalid upstream response"}, {}

    if resp.status_code >= 400:
        message = resp_data.get("error", f"HTTP {resp.status_code}")
        logger.warning(
            "LibreTranslate upstream error: %s (%d)", message, resp.status_code
        )
        return resp.status_code, {"error": message}, {}

    out = json.dumps(resp_data, ensure_ascii=False).encode()
    return 200, resp_data, _metric_headers(bytes_in, len(out), elapsed_ms)
