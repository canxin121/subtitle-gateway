#!/bin/bash
# 一键环境准备: 建 venv + 装依赖 + funasr(可编辑本地版或 PyPI)
# 用法:
#   ./setup.sh                          # funasr 从 PyPI 安装
#   FUNASR_PATH=/path/to/FunASR ./setup.sh   # funasr 以 editable 方式装本地开发版
set -euo pipefail
cd "$(dirname "$0")"

PY_VERSION="${PYTHON_VERSION:-3.12}"
VENV=".venv"
UV="$(command -v uv || true)"

echo "==> subtitle-gateway setup"

# 1) 建 venv: uv 优先, 无 uv fallback 到 python3 -m venv
if [ -n "$UV" ]; then
  echo "==> creating venv with uv (python $PY_VERSION)"
  uv venv --python "$PY_VERSION" "$VENV"
  PIP=("uv" "pip" "install" "--python" "$VENV/bin/python")
else
  echo "==> WARN: uv 未安装, 用 system python 建 venv (建议 brew install uv)"
  python3 -m venv "$VENV"
  PIP=("$VENV/bin/python" "-m" "pip")
fi

# 2) 服务器小依赖
echo "==> installing server dependencies from requirements.txt"
"${PIP[@]}" -r requirements.txt

# 3) funasr: (a) FUNASR_PATH 存在 -> editable 本地开发版; (b) 否则 PyPI 开箱即用
if [ -n "${FUNASR_PATH:-}" ]; then
  echo "==> installing funasr editable from \$FUNASR_PATH = $FUNASR_PATH"
  "${PIP[@]}" -e "$FUNASR_PATH"
else
  echo "==> installing funasr from PyPI (首次会拉入 torch 等大依赖, ~GB 级)"
  "${PIP[@]}" funasr
fi

# 4) torch / torchaudio: FunASR 的 setup.py 未声明它们,
#    但 ASR 运行必需, 这里显式装
echo "==> installing torch/torchaudio (ASR 必需, funasr 未声明)"
"${PIP[@]}" "torch==2.13.0" "torchaudio==2.11.0"

echo "==> done. 启动: ./run.sh"
