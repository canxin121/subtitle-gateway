#!/bin/bash
# 一键启动 subtitle-gateway (FunASR ASR + 翻译网关, auto/MPS/CPU/CUDA)
# 用法: ./run.sh [--port 8000] [--device auto] [--cpu] [--cache-dir <dir>] [其他 gateway 参数...]
# device 默认 auto: 优先 mps(Apple Silicon), 其次 cuda, 否则 cpu;纯 CPU 服务器开箱即用
set -euo pipefail
cd "$(dirname "$0")"

DEVICE=auto
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
echo "==> 预载模型: fun-asr-mlt-nano (按需切换; 默认最多常驻 1 个)"

exec .venv/bin/python -m gateway \
  --device "$DEVICE" \
  --port "$PORT" \
  --preload fun-asr-mlt-nano \
  ${EXTRA[@]+"${EXTRA[@]}"}
