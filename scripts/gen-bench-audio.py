#!/usr/bin/env python3
"""Generate benchmark TTS audio for the ASR bench (macOS only).

The corpus is committed under bench/audio/{ja,zh}/, so benching needs nothing
from this script — it is only how that corpus was produced and how to
regenerate it (or add a language). Regenerating changes the clips, which makes
results incomparable with earlier runs, so prefer adding over rewriting.

Uses the built-in `say` voices and `afconvert`, so no extra dependency is
needed. Voice quality is synthetic, but it is captured in the repo and thus
identical for everyone — good enough for speed (RTF) and for a rough quality
signal. Use --audio to point scripts/bench.py at real audio when you have it.

Usage:
  scripts/gen-bench-audio.py                 # ja -> bench/audio/ja
  scripts/gen-bench-audio.py --lang zh       # zh -> bench/audio/zh
  scripts/gen-bench-audio.py --voice Kyoko   # force a Ja-JP voice
  scripts/gen-bench-audio.py --dir /tmp/x    # somewhere else
"""

# Stdlib-only, so it runs on the system python3 (older than the venv's) too.
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent.parent / "bench" / "audio"

# (filename, label, speech rate wpm, text)
SETS: dict[str, list[tuple[str, str, int, str]]] = {
    "ja": [
        (
            "short.wav",
            "短句",
            180,
            "こんにちは。今日は天気がいいですね。",
        ),
        (
            "long.wav",
            "长句(天气预报)",
            175,
            "今日は全国的に高気圧に覆われて、広い範囲で晴れるでしょう。"
            "気温は平年並みかやや高く、日中は三月下旬から四月並みの暖かさとなりそうです。"
            "ただ、内陸部では朝晩は冷え込みますので、服装でうまく調節してください。"
            "午後は山沿いを中心に、にわか雨や雷雨のおそれがあります。空模様の変化にご注意ください。",
        ),
        (
            "h_1_tongue.wav",
            "绕口令",
            165,
            "生麦生米生卵。生麦生米生卵。庭には二羽、鶏がいる。"
            "隣の客はよく柿食う客だ。",
        ),
        (
            "h_2_haiku.wav",
            "俳句(古语)",
            130,
            "古池や蛙飛びこむ水の音。柿くへば鐘が鳴るなり法隆寺。",
        ),
        (
            "h_3_terms.wav",
            "专业术语",
            170,
            "本日は人工知能と機械学習についてお話しします。"
            "心筋梗塞のリスク要因として高血圧、糖尿病、脂質異常症が挙げられます。"
            "また、クラウドコンピューティングとエッジコンピューティングの違いを整理しましょう。",
        ),
        (
            "h_4_fast.wav",
            "快速语速",
            260,
            "政府は今日の閣議で新しい経済対策を決定しました。"
            "物価上昇に対応するため、所得税の減税と給付金の支給を柱とする内容です。",
        ),
        (
            "h_5_mixed.wav",
            "长句+数字+外来语",
            175,
            "打ち合わせは八がつの十三日の午後二時半から、会議室で行います。"
            "最高気温は三十三度、湿度は六十五パーセントの予報です。"
            "このソフトウェアのアップデートは、ダウンロードしてからインストールしてください。"
            "なお、にわか雨に備えて傘を持って行ったほうがいいでしょう。",
        ),
    ],
    "zh": [
        ("short.wav", "短句", 180, "你好，今天天气不错。"),
        (
            "long.wav",
            "长句(天气预报)",
            175,
            "今天全国大部分地区受高气压控制，天气以晴为主。气温接近常年水平略偏高，"
            "白天感觉温暖。不过内陆地区早晚仍然偏凉，请注意增减衣物。"
            "午后山区附近可能出现阵雨或雷阵雨，请留意天气变化。",
        ),
    ],
}

# Preferred voices, tried in order (macOS `say -v ?` names).
VOICE_PREFS = {
    "ja": ["Kyoko", "Otoya", "O-Ren", "Hattori"],
    "zh": ["Tingting", "Meijia", "Li-mu", "Yu-shu"],
}

# macOS also tags voices with a localized language name; match it case-insensitively.
VOICE_LANG_HINTS = {"ja": ["ja_jp", "日语"], "zh": ["zh_cn", "zh_tw", "中文", "普通话"]}


def list_voices() -> str:
    try:
        return subprocess.run(
            ["say", "-v", "?"], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return ""


def find_voice(voices: str, lang: str) -> str | None:
    lines = voices.splitlines()
    for name in VOICE_PREFS.get(lang, []):
        for line in lines:
            if line.startswith(name) and any(
                h in line.lower() for h in VOICE_LANG_HINTS[lang]
            ):
                return name
    for line in lines:
        if any(h in line.lower() for h in VOICE_LANG_HINTS[lang]):
            return line.split()[0]
    return None


def synth(text: str, out: Path, voice: str, rate: int) -> None:
    """say -> AIFF -> 16 kHz mono 16-bit WAV (what the gateway accepts)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as tmp:
        aiff = Path(tmp.name)
    try:
        subprocess.run(
            ["say", "-v", voice, "-r", str(rate), "-o", str(aiff), text],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "afconvert", str(aiff), str(out),
                "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
            ],
            check=True,
            capture_output=True,
        )
    finally:
        aiff.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--voice", default=None, help="force a `say` voice for every clip")
    ap.add_argument("--lang", default="ja", choices=sorted(SETS), help="which set to synthesize")
    ap.add_argument(
        "--dir",
        default="",
        help=f"output directory (default: {BENCH_DIR}/<lang>)",
    )
    ap.add_argument(
        "--models-json",
        default="",
        help="corpus metadata path (default: <output dir>/models.json)",
    )
    ap.add_argument("--force", action="store_true", help="overwrite existing files")
    args = ap.parse_args()

    if not shutil.which("say") or not shutil.which("afconvert"):
        print("error: needs macOS `say` and `afconvert`", file=sys.stderr)
        return 1

    # One corpus per language, each a self-contained directory that
    # scripts/bench.py can be pointed at with --audio.
    out_dir = Path(args.dir) if args.dir else BENCH_DIR / args.lang
    models_json = Path(args.models_json) if args.models_json else out_dir / "models.json"
    voices = list_voices()
    voice = args.voice or find_voice(voices, args.lang)
    if not voice:
        print(
            f"error: no {args.lang} voice found — install one in "
            "System Settings > Accessibility > Spoken Content, or pass --voice",
            file=sys.stderr,
        )
        return 1

    print(f"output: {out_dir}")
    print(f"voice:  {voice} ({args.lang})")
    labels: dict[str, str] = {}
    references: dict[str, str] = {}
    for name, label, rate, text in SETS[args.lang]:
        labels[name] = label
        references[name] = text
        out = out_dir / name
        if out.exists() and not args.force:
            print(f"  skip   {name:16} {label} (exists)")
            continue
        synth(text, out, voice, rate)
        print(f"  write  {name:16} {label}  ({out.stat().st_size / 1024:.0f} KiB)")

    # The reference text is exactly what was synthesized — recording it here is
    # what lets bench.py report CER, and keeps the two from drifting.
    models_json.parent.mkdir(parents=True, exist_ok=True)
    models_json.write_text(
        json.dumps(
            {
                "voice": f"{voice} ({args.lang})",
                "generated_by": f"scripts/gen-bench-audio.py --lang {args.lang}",
                "labels": labels,
                "reference": references,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"  write  {models_json.name} (labels + reference)")

    print(f"done. bench: scripts/run-bench.sh --audio {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
