#!/usr/bin/env python3
"""ASR bench: RTF + output text for every model, on a directory of WAVs.

Reports both load time and steady-state transcription time:

  RTF = median(transcribe) / audio_duration

so a smaller number is faster and RTF < 1 means faster than real time. It is
also the right metric for a two-phase pipeline (VAD slices audio, then the ASR
runs on the slices) — the fixed per-request overhead is charged once per file
here, and the rest is amortized over the audio length.

Writes results to a table (markdown) and, with --json-out, raw numbers for
tracking speed/quality across models and revisions.

Usage:
  scripts/bench.py --model all
  scripts/bench.py --model sensevoice --device cpu --repeat 3
  scripts/bench.py --audio /path/to/real_audio
  scripts/run-bench.sh --model all --device cpu    # wrapper (uses the repo venv)
"""

import argparse
import json
import statistics
import sys
import time
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# A corpus ships a models.json next to the WAVs, binding files to clips and to
# an optional reference transcript for CER/WER:
#   {"labels": {"a.wav": "绕口令"}, "reference": {"a.wav": "…"}}
BENCH_DIR = REPO / "bench" / "audio"
REFERENCE_FIELD = "reference"


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def cer(reference: str, hypothesis: str) -> float:
    """Character error rate: edit distance / reference length.

    Punctuation, whitespace and case are stripped first, because the models
    differ in whether they emit 「。」/「、」/spaces at all — scoring those would
    compare formatting, not recognition. For languages written without spaces
    this is the meaningful metric (and it degrades to WER-like behaviour for
    space-separated text).
    """
    strip = str.maketrans("", "", " 　\t\n。、，,.!?！？…「」『』()（）:;：；“”\"'")
    ref = reference.lower().translate(strip)
    hyp = hypothesis.lower().translate(strip)
    if not ref:
        return 0.0
    return _edit_distance(ref, hyp) / len(ref)


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance over two rows (linear memory)."""
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(
                min(
                    previous[j] + 1,  # deletion
                    current[j - 1] + 1,  # insertion
                    previous[j - 1] + (ca != cb),  # substitution
                )
            )
        previous = current
    return previous[-1]


def load_audio(args) -> list[tuple[str, Path, float, str]]:
    """Return [(label, path, duration_seconds, reference_text)] for every WAV."""
    audio_dir = Path(args.audio)
    if not audio_dir.is_dir():
        print(f"error: no audio dir at {audio_dir}", file=sys.stderr)
        return []

    labels: dict[str, str] = {}
    references: dict[str, str] = {}
    labels_file = Path(args.models_file) if args.models_file else audio_dir / "models.json"
    if labels_file.is_file():
        data = json.loads(labels_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            labels = data.get("labels", {})
            references = data.get(REFERENCE_FIELD, {})

    items = []
    for path in sorted(audio_dir.glob("*.wav")):
        try:
            items.append(
                (
                    labels.get(path.name, path.name),
                    path,
                    wav_duration(path),
                    references.get(path.name, ""),
                )
            )
        except (wave.Error, EOFError) as e:
            print(f"  skip   {path.name}: not a readable wav ({e})", file=sys.stderr)
    return items


def bench_model(model_name: str, audio: list, args) -> dict:
    import torch

    if args.threads:
        torch.set_num_threads(args.threads)

    import gateway.config as _config
    from gateway import asr
    from gateway.config import RuntimeConfig, resolve_device

    # FunASR draws a tqdm bar per generate() call, which drowns the bench's own
    # output. Bench-only: the gateway keeps its progress bars.
    for entry in asr.MODEL_CONFIGS.values():
        entry["disable_pbar"] = True

    device = resolve_device(args.device)
    # max_loaded_models=0 keeps every bench'd model resident instead of evicting
    # the previous one, so timing one model never pays for reloading another.
    _config.CURRENT = RuntimeConfig(
        device=device, max_loaded_models=0, cache_dir=args.cache_dir
    )

    # Reuse the gateway's own load + generate path, so the numbers reflect
    # production behaviour (dtype overrides, LRU registry, inference_mode).
    t0 = time.perf_counter()
    asr.load_model(model_name)
    load_s = time.perf_counter() - t0

    rows = []
    for label, path, dur, reference in audio:
        times, text = [], ""
        for _ in range(args.repeat):
            t0 = time.perf_counter()
            text, _segments, _elapsed = asr.run_transcription(
                model_name, str(path), args.language, sentence_timestamp=True
            )
            times.append(time.perf_counter() - t0)
        median = statistics.median(times)
        row = {
            "label": label,
            "file": path.name,
            "duration": round(dur, 3),
            "times": [round(t, 3) for t in times],
            "median": round(median, 3),
            "rtf": round(median / dur, 3) if dur else None,
            "text": text,
        }
        if reference:
            row["reference"] = reference
            row["cer"] = round(cer(reference, text), 4)
        rows.append(row)
        speed = f"{1 / (median / dur):.1f}x realtime" if dur and median else "?"
        quality = f"  CER={row['cer']:.3f}" if "cer" in row else ""
        print(f"  {label:26} {dur:6.2f}s  median={median:6.2f}s  RTF={median / dur:5.2f}  ({speed}){quality}")
        print(f"      {text[:300]}")

    return {
        "model": model_name,
        "device": device,
        "torch_threads": torch.get_num_threads(),
        "load_seconds": round(load_s, 2),
        "results": rows,
    }


def write_markdown(payload: dict, path: Path) -> None:
    """One comparison table across every model bench'd, regenerated from JSON."""
    models = payload["models"]
    lines = [
        "# subtitle-gateway ASR bench",
        "",
        f"- device: `{models[0]['device']}`",
        f"- torch threads: {models[0]['torch_threads']}",
        f"- repeats per clip: {payload['repeat']}",
        f"- audio dir: `{payload['audio_dir']}`",
        "",
        "RTF = median(transcribe) / audio duration — lower is faster, < 1 is faster than realtime.",
        "Load time is a one-off cost and is excluded from RTF. CER is character error rate",
        "(punctuation/whitespace stripped) against the reference in the corpus models.json.",
        "",
        "| file | audio (s) | " + " | ".join(m["model"] for m in models) + " |",
        "|---|---|" + "---|" * len(models),
    ]
    files = [r["file"] for r in models[0]["results"]]
    for i, name in enumerate(files):
        dur = models[0]["results"][i]["duration"]
        cells = []
        for m in models:
            row = next((r for r in m["results"] if r["file"] == name), None)
            cell = f"{row['median']:.2f}s (RTF {row['rtf']:.2f})" if row else "-"
            if row and "cer" in row:
                cell += f" CER {row['cer']:.2f}"
            cells.append(cell)
        lines.append(f"| {name} | {dur} | " + " | ".join(cells) + " |")

    # Only meaningful when the corpus carries references.
    scored = [m for m in models if any("cer" in r for r in m["results"])]
    if scored:
        lines += [
            "",
            "### Mean CER (lower is better)",
            "",
            "| model | mean CER |",
            "|---|---|",
        ]
        for m in scored:
            values = [r["cer"] for r in m["results"] if "cer" in r]
            lines.append(f"| {m['model']} | {sum(values) / len(values):.3f} |")

    lines += ["", "## Load time", "", "| model | load (s) |", "|---|---|"]
    lines += [f"| {m['model']} | {m['load_seconds']} |" for m in models]

    for m in models:
        lines += ["", f"## Output: `{m['model']}`", ""]
        for r in m["results"]:
            header = f"**{r['label']}** ({r['file']}, {r['duration']}s)"
            if "cer" in r:
                header += f" — CER {r['cer']:.3f}"
            lines += [header, "", r["text"] or "(empty)", ""]
            if "reference" in r:
                lines += [f"参考: {r['reference']}", ""]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    from gateway.manifest import MODEL_CONFIGS

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--model",
        action="append",
        default=[],
        help=f"model id (repeatable) or 'all'; available: {', '.join(MODEL_CONFIGS)}",
    )
    ap.add_argument("--device", default="auto", help="auto|cpu|mps|cuda (default: auto)")
    ap.add_argument(
        "--audio",
        default=str(BENCH_DIR / "ja"),
        help=f"directory of WAVs (default: {BENCH_DIR}/ja; zh corpus: {BENCH_DIR}/zh)",
    )
    ap.add_argument("--models-file", default=None, help="JSON of {file: label} (default: <audio>/models.json)")
    ap.add_argument("--repeat", type=int, default=3, help="runs per file, take the median (default: 3)")
    ap.add_argument("--language", default=None, help="language hint passed to the model")
    ap.add_argument(
        "--threads",
        type=int,
        default=0,
        help=(
            "torch CPU threads (default: FunASR picks its own — 4 on CPU unless "
            "config.yaml overrides ncpu; set this to your core count for a fairer CPU number)"
        ),
    )
    ap.add_argument(
        "--cache-dir",
        default="",
        help="model cache dir; default: repo-root/models_cache (ignored if already set by the wrapper)",
    )
    ap.add_argument(
        "--out",
        default="",
        help="markdown output path (default: <audio dir>/RESULTS.md)",
    )
    ap.add_argument(
        "--json-out",
        default="",
        help="raw results path (default: <audio dir>/results.json)",
    )
    args = ap.parse_args()

    # Results land next to the corpus, so benching the shipped Japanese set and
    # the Chinese set does not have one overwrite the other. Both are gitignored
    # (the numbers are machine-specific).
    audio_dir = Path(args.audio)
    if not args.out:
        args.out = str(audio_dir / "RESULTS.md")
    if not args.json_out:
        args.json_out = str(audio_dir / "results.json")

    from gateway.config import apply_cache_env, resolve_cache_dir

    # Same priority the gateway uses: --cache-dir > SUBTITLE_GATEWAY_CACHE_DIR >
    # repo-root models_cache.
    cache_dir = resolve_cache_dir(args.cache_dir or None)
    apply_cache_env(cache_dir)
    args.cache_dir = cache_dir

    names = args.model or ["all"]
    if "all" in names:
        names = list(MODEL_CONFIGS)
    unknown = [n for n in names if n not in MODEL_CONFIGS]
    if unknown:
        print(f"error: unknown model {unknown}; available: {list(MODEL_CONFIGS)}", file=sys.stderr)
        return 1

    audio = load_audio(args)
    if not audio:
        print(
            f"hint: generate a synthetic corpus with scripts/gen-bench-audio.py, "
            f"or pass --audio <dir>",
            file=sys.stderr,
        )
        return 1

    print(f"cache:  {cache_dir}")
    payload = {"repeat": args.repeat, "audio_dir": str(Path(args.audio)), "models": []}
    for name in names:
        print(f"\n=== {name} ({len(audio)} files x {args.repeat}) ===")
        try:
            payload["models"].append(bench_model(name, audio, args))
        except Exception as e:
            print(f"  FAIL {name}: {type(e).__name__}: {e}", file=sys.stderr)

    if not payload["models"]:
        return 1

    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_markdown(payload, Path(args.out))
    print(f"\nwrote {args.out}\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
