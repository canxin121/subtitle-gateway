"""ASR model manifest (repo-root `models.json`) — the single source of truth.

Both the gateway and the helper scripts read the same file:
  - gateway.asr uses `MODEL_CONFIGS` for loading / generation kwargs
  - gateway.config uses `DEFAULT_PRELOAD` as the --preload default
  - scripts/download-models.py uses `REQUIRED_MODELS` to download everything

Entry keys the gateway itself consumes and must strip before forwarding to
FunASR: "languages" (advisory, surfaced by /v1/models), the per-device dtype
overrides "llm_dtype_by_device" (FunASR's `llm_dtype` decoder dtype, used by the
LLM-decoder models) and "dtype_by_device" (the model class's own `dtype` kwarg,
used by Qwen3-ASR), plus "note" (human-facing doc).
"""

import json
from collections import OrderedDict
from pathlib import Path

MANIFEST_FILE = Path(__file__).resolve().parent.parent / "models.json"

# Entry keys owned by the gateway, never passed through to FunASR.
GATEWAY_META_KEYS = (
    "languages",
    "llm_dtype_by_device",
    "dtype_by_device",
    "note",
)


def _load() -> dict:
    if not MANIFEST_FILE.is_file():
        raise RuntimeError(
            f"model manifest not found: {MANIFEST_FILE} — "
            "expected models.json at the repository root"
        )
    with MANIFEST_FILE.open(encoding="utf-8") as f:
        return json.load(f)


_MANIFEST = _load()

MODEL_CONFIGS: OrderedDict = OrderedDict(_MANIFEST["models"])
if not MODEL_CONFIGS:
    raise RuntimeError(f"{MANIFEST_FILE} declares no models")

# Models preloaded at startup when --preload is not given.
DEFAULT_PRELOAD: list[str] = list(_MANIFEST.get("preload", []))

# Every model the gateway needs on disk, as (hub_id, hub) — including each
# entry's VAD model, which is fetched through the same hub as its parent (that
# is what AutoModel does at load time, and the two hubs stash files in
# different cache layouts, so the pair has to match).
REQUIRED_MODELS: list[tuple[str, str]] = sorted(
    {
        (entry[field], entry.get("hub", "ms"))
        for entry in MODEL_CONFIGS.values()
        for field in ("model", "vad_model")
        if entry.get(field)
    }
)
