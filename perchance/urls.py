"""Origins and paths taken from generator.har + prompt.har."""

IMAGE_GEN = "https://image-generation.perchance.org"
PERCHANCE = "https://perchance.org"

EMBED = f"{IMAGE_GEN}/embed"
IMAGEAPI = f"{PERCHANCE}/imageapi"
DEFAULT_PROMPT = "a cute booy"


def imageapi_url(prompt: str = DEFAULT_PROMPT) -> str:
    from urllib.parse import quote

    return f"{IMAGEAPI}?prompt={quote(prompt)}"

VERIFY_USER = f"{IMAGE_GEN}/api/verifyUser"
CHECK_VERIFIED = f"{IMAGE_GEN}/api/checkUserVerificationStatus"
GENERATE = f"{IMAGE_GEN}/api/generate"
QUEUE = f"{IMAGE_GEN}/api/getUserQueuePosition"
AWAIT = f"{IMAGE_GEN}/api/awaitExistingGenerationRequest"
DOWNLOAD_PROXY = f"{IMAGE_GEN}/api/downloadTemporaryImageViaProxy"
DOWNLOAD = f"{IMAGE_GEN}/api/downloadTemporaryImage"
AD_ACCESS = f"{PERCHANCE}/api/getAccessCodeForAdPoweredStuff"

# Cloudflare Turnstile (loaded by embed JS when token_required)
TURNSTILE_SITEKEY = "0x4AAAAAAAA8g8NphwaSOT59"

DEFAULT_PROXY_API = "https://adarshu07-no-plz.hf.space"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
