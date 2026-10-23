"""Host gateway URL rewriting shared by local models and Hub discovery."""

import pytest

from toolang.common.urls import replace_guest_loopback


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "0.0.0.0", "[::1]", "[::]"])
@pytest.mark.parametrize(
    "gateway, authority",
    [("host.docker.internal", "host.docker.internal"), ("fd00::1", "[fd00::1]")],
)
def test_gateway_replacement_preserves_url_components(host, gateway, authority):
    assert (
        replace_guest_loopback(f"https://{host}:7001/api?view=current#latest", gateway)
        == f"https://{authority}:7001/api?view=current#latest"
    )
    assert replace_guest_loopback(f"http://{host}/api", gateway) == (
        f"http://{authority}/api"
    )


@pytest.mark.parametrize("gateway", [None, ""])
def test_missing_gateway_preserves_loopback(gateway):
    endpoint = "http://127.0.0.1:7001"
    assert replace_guest_loopback(endpoint, gateway) == endpoint


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://hub.example:7001/api",
        "http://192.0.2.1:7001",
        "http://localhost:invalid",
        "http://[::1",
    ],
)
def test_remote_or_invalid_endpoint_is_unchanged(endpoint):
    assert replace_guest_loopback(endpoint, "host.docker.internal") == endpoint
