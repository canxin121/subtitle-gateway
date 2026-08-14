#!/bin/bash
# 一键启动 subtitle-gateway (FunASR ASR + 翻译网关, MPS/CPU)
# 用法: ./run.sh [--port 8000] [--device mps] [--cpu] [--cache-dir <dir>] [其他 gateway 参数...]
set -euo pipefail
cd "$(dirname "$0")"

DEVICE=mps
PORT=8000
EXTRA=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --device) DEVICE="$2"; shift 2 ;;
    --cpu) DEVICE=cpu; shift ;;
    *) EXTRA+=("$1"); shift ;;        # 其余全部原样透传给 python -m gateway
  esac
done

echo "==> subtitle-gateway (device=$DEVICE port=$PORT)"
echo "==> 模型: fun-asr-mlt-nano, sensevoice"

exec .venv/bin/python -m gateway \
  --device "$DEVICE" \
  --port "$PORT" \
  --preload fun-asr-mlt-nano sensevoice \
  ${EXTRA[@]+"${EXTRA[@]}"}
