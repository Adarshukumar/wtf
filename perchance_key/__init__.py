"""Perchance image-generation userKey pipeline.

Every Chromium instance and every curl_cffi session is bound to exactly
one proxy. The userKey Perchance issues is IP-sticky — reuse the same
proxy for verify, generate, queue, and download.
"""

from .models import KeyBundle, ProxyEndpoint
from .store import KeyStore

__version__ = "0.1.0"
__all__ = ["KeyBundle", "ProxyEndpoint", "KeyStore", "__version__"]
