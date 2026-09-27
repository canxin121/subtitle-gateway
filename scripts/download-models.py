#!/usr/bin/env python3
"""One-key model download: fetch every model the gateway needs into the cache.

Reads the model id set from the repo-root model manifest (gateway.manifest),
so the download list can never drift from what the gateway actually loads.

Usage:
  scripts/download-models.py                      # repo-root models_cache/
  scripts/download-models.py --cache-dir /data/models_cache
  scripts/download-models.py --model sensevoice   # only these (repeatable)
  scripts/download-models.py --list               # show ids, download nothing
"""

import argparse
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def main() -> int:
    from gateway.config import resolve_cache_dir
    from gateway.manifest import MANIFEST_FILE, MODEL_CONFIGS, REQUIRED_MODELS

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--cache-dir",
        default="",
        help="model cache dir (MODELSCOPE_CACHE / HF_HOME); default: repo-root/models_cache",
    )
    ap.add_argument(
        "--model",
        action="append",
        default=[],
        metavar="MANIFEST_ID",
        help="download only this manifest id (repeatable); default: every model in models.json",
    )
    ap.add_argument("--list", action="store_true", help="list hub ids and exit")
    args = ap.parse_args()

    if args.list:
        print(f"manifest: {MANIFEST_FILE}")
        for hub_id, hub in REQUIRED_MODELS:
            print(f"  {hub:3}  {hub_id}")
        return 0

    # --cache-dir > SUBTITLE_GATEWAY_CACHE_DIR > repo-root models_cache, the
    # same priority the gateway itself uses.
    cache_dir = resolve_cache_dir(args.cache_dir or None)
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Same hard set as gateway.config.apply_cache_env, so downloads land where
    # the gateway will look for them.
    os.environ["MODELSCOPE_CACHE"] = str(cache_dir)
    os.environ["HF_HOME"] = str(cache_dir)

    try:
        from funasr.download.download_model_from_hub import download_model
    except ImportError as e:
        print(
            f"error: missing dependency ({e}). Run ./setup.sh in this repo first,\n"
            "       then: .venv/bin/python scripts/download-models.py",
            file=sys.stderr,
        )
        return 1

    wanted_names = args.model or list(MODEL_CONFIGS)
    unknown = [m for m in wanted_names if m not in MODEL_CONFIGS]
    if unknown:
        print(
            f"error: not in {MANIFEST_FILE.name}: {unknown}\n"
            f"       known ids: {list(MODEL_CONFIGS)}",
            file=sys.stderr,
        )
        return 1

    # Every model id, plus each entry's VAD model under its parent's hub —
    # exactly the pairs AutoModel resolves at load time.
    wanted = set()
    for name in wanted_names:
        entry = MODEL_CONFIGS[name]
        hub = entry.get("hub", "ms")
        wanted.add((entry["model"], hub))
        if entry.get("vad_model"):
            wanted.add((entry["vad_model"], hub))
    wanted = sorted(wanted)

    print(f"cache:    {cache_dir}")
    print(f"manifest: {MANIFEST_FILE}")
    failed: list[str] = []
    for hub_id, hub in wanted:
        t0 = time.perf_counter()
        try:
            # Through FunASR's own resolver, so the files land in the same
            # layout AutoModel later looks for (and hub aliases like "fsmn-vad"
            # resolve exactly as they do at inference time).
            kwargs = download_model(model=hub_id, hub=hub, disable_update=True)
            print(
                f"  ok     {hub:3} {hub_id}  ({time.perf_counter() - t0:.1f}s)\n"
                f"         {kwargs.get('model_path', '?')}"
            )
        except Exception as e:  # one bad model must not abort the rest
            failed.append(hub_id)
            print(f"  FAIL   {hub_id}: {type(e).__name__}: {e}", file=sys.stderr)

    if failed:
        print(f"\nerror: {len(failed)} model(s) failed: {failed}", file=sys.stderr)
        return 1
    print(f"\ndone. {len(wanted)} model(s) ready. start: ./run.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
