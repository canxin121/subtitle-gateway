#!/bin/bash
# 一键本地 bench: 用 gateway 自身的加载/推理路径量各模型 RTF + 输出文本。
# 用法:
#   scripts/run-bench.sh                             # 日语语料, 全部模型, device=auto, 3 次取中位
#   scripts/run-bench.sh --device cpu                # 纯 CPU 服务器 (默认线程数已较优, 用 --threads N 覆盖)
#   scripts/run-bench.sh --model sensevoice --repeat 5
#   scripts/run-bench.sh --audio bench/audio/zh      # 中文语料
#   scripts/run-bench.sh --audio /path/to/real_audio # 用真实音频目录替代自带语料
#
# 语料已入库 (bench/audio/ja, bench/audio/zh), 开箱即跑。
# 结果: <语料目录>/RESULTS.md (对比表) + results.json (原始数据), 均已 gitignore。
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  echo "错误: 未找到 .venv/bin/python, 请先运行 ./setup.sh" >&2
  exit 1
fi

exec .venv/bin/python scripts/bench.py "$@"
