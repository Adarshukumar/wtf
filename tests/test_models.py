from perchance_key.models import ProxyEndpoint, pick_protocol


def test_from_api_http():
    ep = ProxyEndpoint.from_api(
        {
            "proxy": "1.2.3.4:8080",
            "ip": "1.2.3.4",
            "port": 8080,
            "protocols": ["HTTP", "HTTPS"],
            "country_code": "US",
        }
    )
    assert ep.address == "1.2.3.4:8080"
    assert ep.protocol == "http"
    assert ep.url == "http://1.2.3.4:8080"
    assert ep.chromium_proxy == "http://1.2.3.4:8080"
    assert ep.curl_proxies() == {"http": ep.url, "https": ep.url}


def test_from_api_socks5_preferred_when_only_socks():
    ep = ProxyEndpoint.from_api({"proxy": "9.9.9.9:1080", "protocols": ["SOCKS5"]})
    assert ep.protocol == "socks5"
    assert ep.chromium_proxy == "socks5://9.9.9.9:1080"


def test_http_wins_over_socks_when_both():
    # Chromium is more reliable on HTTP CONNECT than mixed SOCKS lists.
    assert pick_protocol(["SOCKS5", "HTTP"]) == "http"


def test_bundle_record_roundtrip():
    from perchance_key.models import KeyBundle

    ep = ProxyEndpoint(host="8.8.8.8", port=80, protocol="http", country_code="US")
    b = KeyBundle(
        user_key="a" * 64,
        ad_access_code="b" * 64,
        proxy=ep,
        source="verifyUser",
        user_agent="ua",
    )
    rec = b.to_record()
    assert rec["proxy"] == "8.8.8.8:80"
    assert rec["proxy_url"] == "http://8.8.8.8:80"
    assert rec["user_key"] == "a" * 64
