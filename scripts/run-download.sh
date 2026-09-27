#!/bin/bash
# 一键下载所有 ASR 模型到缓存目录 (模型清单 = 仓库根 models.json)。
# 用法:
#   scripts/run-download.sh                     # 仓库根 models_cache/
#   scripts/run-download.sh --cache-dir /data/models_cache
#   scripts/run-download.sh --model sensevoice  # 只下指定模型 (可重复)
#   scripts/run-download.sh --list              # 只列清单, 不下载
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  echo "错误: 未找到 .venv/bin/python, 请先运行 ./setup.sh" >&2
  exit 1
fi

exec .venv/bin/python scripts/download-models.py "$@"
