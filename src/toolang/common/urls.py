"""URL transformations shared by host-service clients."""

from urllib.parse import urlsplit, urlunsplit


def replace_guest_loopback(value: str, gateway: str | None) -> str:
    """Rewrite a local endpoint through an explicitly supplied host gateway."""

    if not gateway:
        return value
    try:
        parsed = urlsplit(value)
        if parsed.hostname not in {
            "0.0.0.0",
            "127.0.0.1",
            "localhost",
            "::",
            "::1",
        }:
            return value
        port = parsed.port
    except ValueError:
        return value
    gateway_host = f"[{gateway}]" if ":" in gateway else gateway
    netloc = f"{gateway_host}:{port}" if port is not None else gateway_host
    userinfo, separator, _ = parsed.netloc.rpartition("@")
    if separator:
        netloc = f"{userinfo}@{netloc}"
    return urlunsplit(parsed._replace(netloc=netloc))
