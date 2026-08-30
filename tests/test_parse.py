from perchance_key.parse import extract_ad_access_code, extract_user_key


KEY = "fe14a03dabcc4c3013f7f56db38bc7cda58b0da258fcef96b1c10afe08d5aa7e"
AD = "bdb81e510ad8273f27163560c1280622ecd6df7eabe87a7a8d4a94411812a843"


def test_user_key_from_json_object():
    assert extract_user_key({"status": "success", "userKey": KEY}) == KEY


def test_user_key_from_already_verified():
    assert extract_user_key({"status": "already_verified", "userKey": KEY}) == KEY


def test_user_key_from_plain_hex():
    assert extract_user_key(KEY) == KEY


def test_user_key_from_querystring():
    url = (
        "https://image-generation.perchance.org/api/generate"
        f"?userKey={KEY}&requestId=0.1&adAccessCode={AD}"
    )
    assert extract_user_key("", url) == KEY
    assert extract_ad_access_code("", url) == AD


def test_ad_code_plain():
    assert extract_ad_access_code(AD) == AD


def test_nested_data():
    assert extract_user_key({"data": {"userKey": KEY}}) == KEY
