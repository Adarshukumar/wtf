"""
probe_env.py — Detects your local curl_cffi capabilities.
Run: python probe_env.py
"""
import sys
import platform
import importlib.metadata

print("=" * 70)
print("ENVIRONMENT PROBE")
print("=" * 70)
print(f"Python:        {sys.version}")
print(f"Platform:      {platform.platform()}")
print()

# curl_cffi version
try:
    cv = importlib.metadata.version("curl_cffi")
    print(f"curl_cffi:     {cv}")
except Exception as e:
    print(f"curl_cffi:     NOT INSTALLED ({e})")
    sys.exit(1)

# List available impersonation targets
print()
print("Available impersonation targets (from your curl_cffi build):")
try:
    from curl_cffi import requests
    members = list(requests.BrowserType.__members__.keys())
    for i in range(0, len(members), 6):
        print("  " + "  ".join(f"{m:24s}" for m in members[i:i+6]))
except Exception as e:
    print(f"  could not enumerate: {e}")

# certifi
print()
try:
    import certifi
    print(f"certifi:       {certifi.__version__}")
    print(f"  CA bundle:   {certifi.where()}")
    import os
    if os.path.exists(certifi.where()):
        sz = os.path.getsize(certifi.where())
        print(f"  size:        {sz:,} bytes")
except Exception as e:
    print(f"certifi:       NOT INSTALLED ({e})")

# Quick test of one known-good fingerprint
print()
print("Quick TLS handshake test (chrome131, the one that worked for you):")
try:
    from curl_cffi import requests
    r = requests.get(
        "https://perchance.org/imageapi?prompt=a%20cute%20booy",
        impersonate="chrome131",
        timeout=20,
    )
    print(f"  status:        {r.status_code}")
    print(f"  body length:   {len(r.text)}")
    print(f"  has 'verifyUser' in HTML: {'verifyUser' in r.text}")
    print(f"  has 'image-generation' in HTML: {'image-generation' in r.text}")
    # Extract the first <script> src if any
    import re
    scripts = re.findall(r'<script[^>]*src=["\']([^"\']+)["\']', r.text)
    print(f"  <script src=> count: {len(scripts)}")
    for s in scripts[:5]:
        print(f"    - {s}")
except Exception as e:
    print(f"  ERROR: {type(e).__name__}: {str(e)[:200]}")
