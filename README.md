# subtitle-gateway

统一的 **ASR + 翻译网关**(从 FunASR 仓库的 `serve_dual.py` 独立出来):
- **ASR**:FunASR 双模型(SenseVoiceSmall + Fun-ASR-MLT-Nano)
  - OpenAI 兼容 `POST /v1/audio/transcriptions`(multipart)
  - ferrum 协议 `POST /transcribe`(raw body + Opus/AES-GCM/鉴权,供 [mpv-stt-plugin](https://github.com/canxin121/mpv_stt_plugin_crates) 使用)
- **翻译**:两个协议网关,转发到各自上游
  - DeepL 兼容 `POST /v1/translate`
  - LibreTranslate `POST /translate`

## 快速开始

```bash
# 1. 一键装环境 (funasr 从 PyPI 装)
./setup.sh

# 想要精确复现本地 FunASR 开发版行为, 用 editable:
FUNASR_PATH=/path/to/FunASR ./setup.sh

# 2. 一键启动 (device 默认 auto: 有 MPS 用 MPS, 否则 CPU; 端口 8000, 预载两个模型)
./run.sh

# 或指定端口/设备/复用旧模型缓存 (--cpu 等价 --device cpu)
./run.sh --port 9000 --cpu --cache-dir /path/to/old/models_cache
./run.sh --device cuda   # 有 NVIDIA GPU 的服务器
```

## 端点

| 端点 | 协议 | 说明 |
|---|---|---|
| `POST /v1/audio/transcriptions` | OpenAI | `-F file=@audio.wav -F model=sensevoice`(或 `fun-asr-mlt-nano`) |
| `POST /transcribe` | ferrum | raw body,头 `x-model`/`x-compression`(pcm\|wav\|opus)/`x-encrypted`/`x-auth-token` |
| `POST /v1/translate` | DeepL | header `Authorization: DeepL-Auth-Key {key}` |
| `POST /translate` | LibreTranslate | body `api_key` 字段 |
| `GET /v1/models` | OpenAI | 可用模型列表 |
| `GET /health` | - | 健康检查 |

## CLI 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` / `--port` | `0.0.0.0` / `8000` | 监听地址 |
| `--device` | `auto` | `auto`(首个可用 mps/cuda/cpu)\| `cpu` \| `mps` \| `cuda`;显式设备不可用时**自动回退 `cpu`**,纯 CPU 服务器开箱即用 |
| `--preload` | `fun-asr-mlt-nano sensevoice` | 启动预载模型(裸 `--preload` = 不加载) |
| `--cache-dir` | 仓库根 `models_cache/` | 模型缓存目录(设 MODELSCOPE_CACHE + HF_HOME) |
| `--auth-secret` | `""` | ferrum 鉴权(客户端发 `x-auth-token = sha256(secret)`) |
| `--encryption-key` | `""` | ferrum AES-256-GCM 口令 |
| `--translate-upstream` / `-key` / `-api-key` | `""` | DeepL 网关:上游基址 / 发给上游的 key / 网关鉴权 key |
| `--libretranslate-upstream` / `-key` / `-api-key` | `""` | LibreTranslate 网关:同上 |

## 翻译网关示例

```bash
./run.sh --port 8000 \
  --translate-upstream https://api-free.deepl.com \
  --translate-upstream-key <deepl上游key> \
  --translate-api-key <deepl网关key> \
  --libretranslate-upstream http://127.0.0.1:5000 \
  --libretranslate-upstream-key <libre上游key> \
  --libretranslate-api-key <libre网关key>
```

## 模型缓存

默认缓存目录 = 仓库根 `models_cache/`(相对路径,不硬编码)。三个途径覆盖:
1. `--cache-dir <dir>` CLI 参数(优先级最高)
2. `SUBTITLE_GATEWAY_CACHE_DIR` 环境变量
3. 仓库根默认

**从旧 serve_dual 迁移**:旧缓存(7G)直接复用,无需重新下载——
```bash
ln -s /path/to/old/models_cache models_cache
# 或每次启动带参数:
./run.sh --cache-dir /path/to/old/models_cache
```

## 服务安装

### systemd(Linux)

```bash
# 前置: 已运行 ./setup.sh
sudo ./install-systemd.sh                          # 系统级, 开机自启 (device 默认 cpu)
sudo ./install-systemd.sh --device cuda            # 有 NVIDIA GPU 的服务器
sudo ./install-systemd.sh --cache-dir /path/to/models_cache
./install-systemd.sh --user                        # 用户级
```

需要 `libopus0`(Opus 解码):`sudo apt install libopus0`。若 venv 用 `FUNASR_PATH` editable 构建,请确保该路径在服务运行时仍可访问。

### macOS

**仓库在系统卷**(如 `~/Projects/...`)→ 用 launchd LaunchAgent:

```bash
./launchd/install-macos.sh                        # 安装为 LaunchAgent (device 默认 auto)
./launchd/install-macos.sh --device mps --cache-dir /path/to/models_cache
./launchd/install-macos.sh --uninstall           # 卸载
```

- plist:`~/Library/LaunchAgents/com.canxin.subtitle-gateway.plist`;日志:`~/Library/Logs/subtitle-gateway/`
- 查看状态:`launchctl print gui/$(id -u)/com.canxin.subtitle-gateway`
- 安装脚本会先检查端口占用——`KeepAlive` 会对启动失败的实例反复重试,请先释放端口再安装。

**仓库在外部卷**(如 `/Volumes/...`)→ macOS 的 launchd 服务进程**无法访问外部卷**(TCC 限制:加载外部卷二进制会卡死在 dyld、也无法写外部卷日志),请用用户上下文守护脚本:

```bash
./run-as-service.sh start                        # 后台运行 + 看门狗(崩溃 5s 自动重启)
./run-as-service.sh status                       # 状态 + /health
./run-as-service.sh stop
./run-as-service.sh start --port 8000 --device auto   # 可传 gateway 参数
# 开机自启: 系统设置 > 通用 > 登录项 > "+" 添加 /path/to/subtitle-gateway/run-as-service.sh
#   (选"打开时启动"; 登录项以用户会话启动, 可正常访问外部卷)
```

- 日志:仓库内 `logs/gateway.log` / `logs/gateway-error.log`(外部卷,用户上下文可写)

## 无 GPU / 纯 CPU 服务器

- `--device` 默认 `auto`:优先 `mps`(Apple Silicon)→ `cuda` → `cpu`;显式 `--device mps`/`cuda` 在不可用机器上**自动回退 `cpu`** 并打 warning,无需改配置。
- Linux systemd 默认 `--device cpu`;macOS launchd 默认 auto。
- 纯 CPU 上推理较慢属预期(SenseVoice 每句数秒),模型加载同样从缓存命中。

## funasr 安装策略

- `setup.sh`(无 `FUNASR_PATH`):从 **PyPI** 装 `funasr`(首次拉入 torch 等,~GB 级)。
- `FUNASR_PATH=/path/to/FunASR ./setup.sh`:以 **editable** 方式安装本地 FunASR 开发版——PyPI 发布版可能滞后本地版,要精确复现本地行为请用这个。

## 许可

MIT(派生自 FunASR 的 serve_dual.py)。ASR 模型权重各自许可,运行时从 ModelScope / Hugging Face 下载,不在本仓库内。
