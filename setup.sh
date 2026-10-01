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

# PyPI 直连在部分网络下极慢 (torch 单个包 ~2GB)。用官方源: PIP_INDEX_URL= ./setup.sh
PIP_INDEX_URL="${PIP_INDEX_URL-https://mirrors.aliyun.com/pypi/simple/}"
export PIP_INDEX_URL
export UV_DEFAULT_INDEX="${UV_DEFAULT_INDEX:-$PIP_INDEX_URL}"

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

echo "==> index: $PIP_INDEX_URL"

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
#    但 ASR 运行必需; 放最后装, 让这个 pin 覆盖 funasr 拉进来的版本
echo "==> installing torch/torchaudio (ASR 必需, funasr 未声明)"
"${PIP[@]}" "torch==2.13.0" "torchaudio==2.11.0"

# 5) Qwen3-ASR 运行时 (Qwen3-ASR-1.7B / 0.6B 两个模型)。
#    qwen-asr 硬性要求 transformers==4.57.6, 所以这里连同 tokenizers /
#    huggingface_hub 一起降到该版本区间 —— 这是 qwen-asr 与 transformers 5.x
#    的已知冲突, 必须在 funasr 之后、按此顺序装, 否则依赖解析会把 5.x 拉回来。
echo "==> installing Qwen3-ASR runtime (qwen-asr; pins transformers==4.57.6)"
"${PIP[@]}" "qwen-asr==0.0.6" "transformers==4.57.6" "tokenizers==0.22.2" "huggingface_hub==0.36.2"

# 6) 预下载模型 (清单 = 仓库根 models.json)。ASR 必需; 装完即用, 无网络时跳过。
if [ "${SKIP_MODEL_DOWNLOAD:-}" = "1" ]; then
  echo "==> SKIP_MODEL_DOWNLOAD=1, 跳过模型下载 (之后可跑: scripts/run-download.sh)"
else
  echo "==> downloading ASR models (models.json; 四个模型首次约 10G, 复用已有缓存)"
  if ! "$VENV/bin/python" scripts/download-models.py; then
    echo "==> WARN: 模型下载失败(网络?), 安装继续。稍后可重试: scripts/run-download.sh" >&2
  fi
fi

echo "==> done. 启动: ./run.sh"
echo "==> 本地测速/质量: scripts/run-bench.sh (语料已入库, 无需生成)"
