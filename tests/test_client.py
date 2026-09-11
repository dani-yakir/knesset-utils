import httpx
import pytest

from knesset_utils.odata import client as client_mod
from knesset_utils.odata.client import ODataClient


def _client_with(monkeypatch, statuses):
    """ODataClient whose transport answers with `statuses` in order (200 -> empty page)."""
    calls = []
    sleeps = []
    monkeypatch.setattr(client_mod.time, "sleep", sleeps.append)

    def handler(request):
        status = statuses[len(calls)]
        calls.append(request)
        return httpx.Response(status, json={"value": []}) if status == 200 else httpx.Response(status, text="nope")

    c = ODataClient(retries=5, backoff=1.0, throttle_backoff=5.0)
    c._client = httpx.Client(transport=httpx.MockTransport(handler))
    return c, calls, sleeps


@pytest.mark.parametrize("status", [429, 481])
def test_throttle_status_is_retried_with_exponential_backoff(monkeypatch, status):
    c, calls, sleeps = _client_with(monkeypatch, [status, status, 200])

    assert c.get_entities("KNS_Foo") == {"value": []}
    assert len(calls) == 3
    assert sleeps == [5.0, 10.0]


def test_persistent_throttle_raises_after_all_attempts(monkeypatch):
    c, calls, sleeps = _client_with(monkeypatch, [481] * 5)

    with pytest.raises(httpx.HTTPStatusError) as exc:
        c.get_entities("KNS_Foo")
    assert exc.value.response.status_code == 481
    assert len(calls) == 5
    assert sleeps == [5.0, 10.0, 20.0, 40.0]


def test_other_4xx_is_not_retried(monkeypatch):
    c, calls, sleeps = _client_with(monkeypatch, [404])

    with pytest.raises(httpx.HTTPStatusError):
        c.get_entities("KNS_Foo")
    assert len(calls) == 1
    assert sleeps == []


def test_5xx_keeps_its_linear_backoff(monkeypatch):
    c, calls, sleeps = _client_with(monkeypatch, [500, 503, 200])

    assert c.get_entities("KNS_Foo") == {"value": []}
    assert sleeps == [1.0, 2.0]


def test_throttle_callback_fires_per_throttled_response(monkeypatch):
    c, calls, sleeps = _client_with(monkeypatch, [481, 429, 200])
    seen = []
    c.on_throttle = lambda: seen.append(1)

    c.get_entities("KNS_Foo")
    assert len(seen) == 2
