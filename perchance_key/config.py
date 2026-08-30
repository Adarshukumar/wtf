"""Runtime configuration. Env vars win over defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("PERCHANCE_DATA_DIR", ROOT / "data"))
OUTPUT_DIR = Path(os.environ.get("PERCHANCE_OUTPUT_DIR", ROOT / "output"))

DEFAULT_PROXY_API = "https://adarshu07-no-plz.hf.space"

# Observed in perchance.org.json (Chrome 152 HAR, 2026-08-25).
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

IMAGE_GEN_ORIGIN = "https://image-generation.perchance.org"
PERCHANCE_ORIGIN = "https://perchance.org"
EMBED_URL = f"{IMAGE_GEN_ORIGIN}/embed"
IMAGEAPI_URL = f"{PERCHANCE_ORIGIN}/imageapi"

VERIFY_PATH = "/api/verifyUser"
GENERATE_PATH = "/api/generate"
QUEUE_PATH = "/api/getUserQueuePosition"
AWAIT_PATH = "/api/awaitExistingGenerationRequest"
DOWNLOAD_PROXY_PATH = "/api/downloadTemporaryImageViaProxy"
DOWNLOAD_PATH = "/api/downloadTemporaryImage"
AD_ACCESS_PATH = "/api/getAccessCodeForAdPoweredStuff"

DEFAULT_CHANNEL = "imageapi"
DEFAULT_SUBCHANNEL = "public"
DEFAULT_RESOLUTION = "512x768"
DEFAULT_GUIDANCE = 7


@dataclass
class Settings:
    proxy_api: str = field(
        default_factory=lambda: os.environ.get("PERCHANCE_PROXY_API", DEFAULT_PROXY_API).rstrip("/")
    )
    data_dir: Path = field(default_factory=lambda: DATA_DIR)
    output_dir: Path = field(default_factory=lambda: OUTPUT_DIR)
    user_agent: str = field(default_factory=lambda: os.environ.get("PERCHANCE_UA", DEFAULT_UA))
    headless: bool = field(
        default_factory=lambda: os.environ.get("PERCHANCE_HEADLESS", "1") not in ("0", "false", "False")
    )
    impersonate: str = field(
        default_factory=lambda: os.environ.get("PERCHANCE_IMPERSONATE", "chrome131")
    )
    browser_timeout: float = field(
        default_factory=lambda: float(os.environ.get("PERCHANCE_BROWSER_TIMEOUT", "75"))
    )
    proxy_probe_timeout: float = field(
        default_factory=lambda: float(os.environ.get("PERCHANCE_PROXY_PROBE_TIMEOUT", "12"))
    )
    prefer_protocols: tuple[str, ...] = ("HTTP", "HTTPS", "SOCKS5")
    channel: str = DEFAULT_CHANNEL
    subchannel: str = DEFAULT_SUBCHANNEL

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / ".chrome").mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
