from perchance_key.models import KeyBundle, ProxyEndpoint
from perchance_key.store import KeyStore


def test_save_and_latest(tmp_path):
    store = KeyStore(path=tmp_path / "keys.sqlite")
    ep = ProxyEndpoint(host="1.1.1.1", port=8080, protocol="http", country_code="DE")
    bundle = KeyBundle(
        user_key="c" * 64,
        ad_access_code="d" * 64,
        proxy=ep,
        source="verifyUser",
        user_agent="ua",
        cookies={"cf": "1"},
    )
    store.save(bundle)
    got = store.latest()
    assert got is not None
    assert got.user_key == "c" * 64
    assert got.proxy.address == "1.1.1.1:8080"
    assert "1.1.1.1:8080" in store.used_proxies()
    store.mark_dead("2.2.2.2:80", "probe")
    assert "2.2.2.2:80" in store.dead()
