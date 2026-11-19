import httpx
import pytest
import respx

from app.keitaro import KeitaroClient, KeitaroError


@respx.mock
def test_sends_api_key_and_uses_admin_api_path():
    route = respx.get("https://k.test/admin_api/v1/campaigns/5/streams").mock(
        return_value=httpx.Response(200, json=[{"id": 1}])
    )
    kt = KeitaroClient("https://k.test/", "secret")
    assert kt.streams(5) == [{"id": 1}]
    assert route.calls[0].request.headers["Api-Key"] == "secret"


@respx.mock
def test_error_is_raised_with_message():
    respx.post("https://k.test/admin_api/v1/campaigns").mock(
        return_value=httpx.Response(422, json={"error": "alias is taken"})
    )
    with pytest.raises(KeitaroError) as e:
        KeitaroClient("https://k.test", "k").create_campaign({})
    assert e.value.status == 422 and "alias" in e.value.message


@respx.mock
def test_network_errors_become_keitaro_errors():
    respx.get("https://k.test/admin_api/v1/offers").mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(KeitaroError) as e:
        KeitaroClient("https://k.test", "k").offers()
    assert e.value.status == 504


def test_keitaro_url_must_be_https():
    from app.config import Settings

    with pytest.raises(ValueError):
        Settings(_env_file=None, keitaro_url="http://keitaro.example")
    assert Settings(_env_file=None, keitaro_url="https://k.example/").keitaro_url == "https://k.example"
    assert Settings(_env_file=None, keitaro_url="http://localhost:8080").keitaro_url
