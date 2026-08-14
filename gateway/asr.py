"""ASR models + transcription pipeline, ported verbatim from serve_dual.py.

Covers: model registry / lazy loading, FunASR transcription, SRT rendering,
Opus decoding (ctypes binding to libopus), WAV validation, and the ferrum
response helpers.
"""

import ctypes
import ctypes.util
import logging
import os
import re
import struct
import time

from .config import get_cfg

logger = logging.getLogger(__name__)

MODEL_REGISTRY = {}
_OPUS_DECODER = None

MODEL_CONFIGS = {
    "sensevoice": {
        "model": "iic/SenseVoiceSmall",
        "vad_model": "fsmn-vad",
        "vad_kwargs": {"max_single_segment_time": 30000},
    },
    "fun-asr-mlt-nano": {
        "model": "FunAudioLLM/Fun-ASR-MLT-Nano-2512",
        "hub": "hf",
        "trust_remote_code": True,
        "vad_model": "fsmn-vad",
        "vad_kwargs": {"max_single_segment_time": 30000},
    },
}


def load_model(model_name: str):
    """Lazily load a model and cache it in the registry (no-op if loaded)."""
    if model_name in MODEL_REGISTRY:
        return MODEL_REGISTRY[model_name]

    if model_name not in MODEL_CONFIGS:
        available = list(MODEL_CONFIGS.keys())
        raise ValueError(f"Unknown model '{model_name}'. Available: {available}")

    from funasr import AutoModel

    cfg = MODEL_CONFIGS[model_name].copy()
    cfg["device"] = get_cfg().device
    cfg["disable_update"] = True

    logger.info(f"Loading model '{model_name}' on {cfg['device']}...")
    t0 = time.time()
    model = AutoModel(**cfg)
    elapsed = time.time() - t0
    logger.info(f"Model '{model_name}' loaded in {elapsed:.1f}s")

    MODEL_REGISTRY[model_name] = model
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
    asr_model = load_model(model_name)
    t0 = time.time()

    generate_kwargs = {"input": audio_path, "batch_size": 1}
    if language:
        generate_kwargs["language"] = language
    if sentence_timestamp:
        generate_kwargs["sentence_timestamp"] = True

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
