"""Runtime configuration: CLI parsing, cache-dir resolution, env injection.

Replaces the module-level `global` state of the original serve_dual.py with a
single immutable `RuntimeConfig` dataclass read at request time via `get_cfg()`.
"""

import argparse
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class RuntimeConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    device: str = "mps"
    preload: list = field(default_factory=list)
    cache_dir: Path = field(default_factory=lambda: default_cache_dir())
    # ferrum protocol
    auth_secret: str = ""
    encryption_key: str = ""
    # DeepL-compatible translation gateway
    translate_upstream: str = ""
    translate_upstream_key: str = ""
    translate_api_key: str = ""
    # LibreTranslate-compatible translation gateway
    libretranslate_upstream: str = ""
    libretranslate_upstream_key: str = ""
    libretranslate_api_key: str = ""


# Singleton populated by main() before the server starts. Handlers must read it
# at request time (never snapshot at import time) so CLI options take effect.
CURRENT: RuntimeConfig | None = None


def get_cfg() -> RuntimeConfig:
    if CURRENT is None:
        # Only reachable in tests / before main() runs; fall back to defaults.
        return RuntimeConfig()
    return CURRENT


def default_cache_dir() -> Path:
    """Repo-root models_cache (relative to this package, no hardcoded path)."""
    return Path(__file__).resolve().parent.parent / "models_cache"


def resolve_cache_dir(cli_cache_dir: str | None) -> Path:
    """Priority: --cache-dir > SUBTITLE_GATEWAY_CACHE_DIR env > repo default."""
    if cli_cache_dir:
        return Path(cli_cache_dir)
    env_dir = os.environ.get("SUBTITLE_GATEWAY_CACHE_DIR")
    if env_dir:
        return Path(env_dir)
    return default_cache_dir()


def apply_cache_env(cache_dir: Path) -> None:
    """Point ModelScope / HuggingFace caches at cache_dir (hard set, not
    setdefault, so an explicit --cache-dir always wins over user env)."""
    os.environ["MODELSCOPE_CACHE"] = str(cache_dir)
    os.environ["HF_HOME"] = str(cache_dir)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )


def parse_args(argv: list[str] | None = None) -> RuntimeConfig:
    parser = argparse.ArgumentParser(
        description="subtitle-gateway: FunASR ASR (OpenAI + ferrum) and translation (DeepL + LibreTranslate) gateway"
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    parser.add_argument("--device", default="mps", help="Device: cuda, cpu, mps")
    parser.add_argument(
        "--preload",
        nargs="*",
        default=["fun-asr-mlt-nano", "sensevoice"],
        help="Startup pre-load model(s), e.g. --preload fun-asr-mlt-nano sensevoice",
    )
    parser.add_argument(
        "--cache-dir",
        default="",
        help="Model cache dir (MODELSCOPE_CACHE / HF_HOME); default: repo-root/models_cache",
    )
    parser.add_argument(
        "--auth-secret",
        default="",
        help="Ferrum protocol auth secret (client sends x-auth-token = sha256(secret)); empty disables",
    )
    parser.add_argument(
        "--encryption-key",
        default="",
        help="Ferrum protocol AES-256-GCM passphrase (key = sha256(passphrase)); empty disables",
    )
    parser.add_argument(
        "--translate-upstream",
        default="",
        help="Upstream DeepL-compatible translation base URL (e.g. https://api-free.deepl.com or a self-hosted service); empty disables the /v1/translate gateway",
    )
    parser.add_argument(
        "--translate-upstream-key",
        default="",
        help="Key sent to the upstream translation service (Authorization: DeepL-Auth-Key); empty omits it",
    )
    parser.add_argument(
        "--translate-api-key",
        default="",
        help="Gateway auth for /v1/translate: clients must send Authorization: DeepL-Auth-Key {key}; empty disables auth",
    )
    parser.add_argument(
        "--libretranslate-upstream",
        default="",
        help="Upstream LibreTranslate base URL (e.g. http://127.0.0.1:5000); empty disables the /translate gateway",
    )
    parser.add_argument(
        "--libretranslate-upstream-key",
        default="",
        help="Key sent to the upstream LibreTranslate service (body api_key); empty omits it",
    )
    parser.add_argument(
        "--libretranslate-api-key",
        default="",
        help="Gateway auth for /translate: clients must send this key in the body api_key field; empty disables auth",
    )
    args = parser.parse_args(argv)

    return RuntimeConfig(
        host=args.host,
        port=args.port,
        device=args.device,
        preload=args.preload,
        cache_dir=resolve_cache_dir(args.cache_dir),
        auth_secret=args.auth_secret,
        encryption_key=args.encryption_key,
        translate_upstream=args.translate_upstream,
        translate_upstream_key=args.translate_upstream_key,
        translate_api_key=args.translate_api_key,
        libretranslate_upstream=args.libretranslate_upstream,
        libretranslate_upstream_key=args.libretranslate_upstream_key,
        libretranslate_api_key=args.libretranslate_api_key,
    )
