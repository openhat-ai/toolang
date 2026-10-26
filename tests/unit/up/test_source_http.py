"""Remote agent sources retain TLS verification without a Python CA installation."""

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
import ssl
from threading import Thread
from urllib.error import URLError
from urllib.request import urlopen

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
import httpx
import pytest

from toolang.common.github import GitHubRef
from toolang.up import source

_PUBLIC_CA_BUNDLE = certifi.where()


@pytest.fixture
def https_source(tmp_path, monkeypatch):
    ca_key = ec.generate_private_key(ec.SECP256R1())
    server_key = ec.generate_private_key(ec.SECP256R1())
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Toolang test CA")])
    certificates = []
    for serial, key, is_ca in ((1, ca_key, True), (2, server_key, False)):
        subject = (
            issuer
            if is_ca
            else x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        )
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(serial)
            .not_valid_before(datetime(2020, 1, 1, tzinfo=timezone.utc))
            .not_valid_after(datetime(2100, 1, 1, tzinfo=timezone.utc))
            .add_extension(x509.BasicConstraints(ca=is_ca, path_length=None), True)
            .add_extension(
                x509.KeyUsage(
                    True, False, False, False, False, is_ca, is_ca, False, False
                ),
                True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                False,
            )
        )
        if not is_ca:
            builder = builder.add_extension(
                x509.SubjectAlternativeName(
                    [x509.DNSName("localhost"), x509.IPAddress(ip_address("127.0.0.1"))]
                ),
                False,
            ).add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False
            )
        certificates.append(builder.sign(ca_key, hashes.SHA256()))
    ca_file = tmp_path / "ca.pem"
    ca_file.write_bytes(certificates[0].public_bytes(serialization.Encoding.PEM))
    certificate = tmp_path / "server.pem"
    certificate.write_bytes(certificates[1].public_bytes(serialization.Encoding.PEM))
    private_key = tmp_path / "server.key"
    private_key.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, private_key)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/redirect.too":
                self.send_response(302)
                self.send_header("Location", "/lex.too")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            content = "agic:\n  Practice English.\n".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", lambda *_args: None)
    monkeypatch.setattr(certifi, "where", lambda: str(ca_file))
    try:
        yield f"https://127.0.0.1:{server.server_port}/lex.too"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize("redirect", [False, True])
def test_agent_fetch_works_without_python_default_certificates(https_source, redirect):
    with pytest.raises(URLError, match="CERTIFICATE_VERIFY_FAILED"):
        urlopen(https_source, timeout=5)
    url = https_source.replace("lex.too", "redirect.too") if redirect else https_source
    assert source._fetch_http_text(url) == "agic:\n  Practice English.\n"


def test_agent_fetch_still_rejects_untrusted_certificates(https_source, monkeypatch):
    monkeypatch.setattr(certifi, "where", lambda: _PUBLIC_CA_BUNDLE)
    with pytest.raises(ValueError, match="TLS certificate verification failed"):
        source._fetch_http_text(https_source)


@pytest.mark.parametrize("status", [200, 404, 403, 429, 503])
def test_github_probe_distinguishes_missing_sources_from_request_failures(
    monkeypatch, status
):
    ref = GitHubRef(owner="octo", repo="agents", path="lex.too", rev="main")

    def head(url, **_kwargs):
        return httpx.Response(status, request=httpx.Request("HEAD", url))

    monkeypatch.setattr(httpx, "head", head)
    if status in {200, 404}:
        assert source._github_agent_ref_exists(ref) is (status == 200)
    else:
        with pytest.raises(ValueError, match=f"HTTP {status}"):
            source._github_agent_ref_exists(ref)


@pytest.mark.parametrize("failure", ["timeout", "network", "TLS"])
def test_github_shorthand_preserves_network_failure_reason(monkeypatch, failure):
    messages = {
        "timeout": "request timed out",
        "network": "network request failed",
        "TLS": "TLS certificate verification failed",
    }

    def unavailable(*_args, **_kwargs):
        if failure == "timeout":
            raise httpx.ConnectTimeout("https://secret.example/?token=secret-value")
        if failure == "TLS":
            try:
                raise ssl.SSLCertVerificationError("secret-value")
            except ssl.SSLCertVerificationError as exc:
                raise httpx.ConnectError("secret-value") from exc
        raise httpx.ConnectError("secret-value")

    monkeypatch.setattr(httpx, "get", unavailable)
    monkeypatch.setattr(httpx, "head", unavailable)
    events = []
    with pytest.raises(ValueError, match=messages[failure]) as error:
        source.resolve_agent_source(
            source.parse_agent_target("gh:octo/lex"), progress=events.append
        )
    assert "could not resolve agent shorthand" not in str(error.value)
    assert "secret" not in str(error.value)
    assert all("secret" not in str(event) for event in events)


def test_github_default_branch_reads_json(monkeypatch):
    def get(url, **_kwargs):
        return httpx.Response(
            200, json={"default_branch": "trunk"}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx, "get", get)
    assert source._github_repo_default_branch("octo", "agents") == "trunk"


@pytest.mark.parametrize("status", [404, 429])
def test_agent_fetch_reports_http_status_without_url_secrets(monkeypatch, status):
    def get(url, **_kwargs):
        return httpx.Response(status, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    events = []
    with pytest.raises(ValueError, match=f"HTTP {status}") as error:
        source.fetch_agent_ref(
            source.HttpRef(
                "https://secret-user:secret-pass@example.com/lex.too?token=secret-query"
            ),
            progress=events.append,
        )
    assert "secret" not in str(error.value)
    assert all("secret" not in str(event) for event in events)
