"""Bootstrap не переносит локальный токен через redirect и подменённый порт."""

import importlib.util
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "mvp_bootstrap", Path(__file__).resolve().parents[1] / "scripts" / "mvp.py"
)
mvp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mvp)


@pytest.mark.parametrize("port", ["8065@evil.test", "8065/path", "-1", "0", "65536", "abc", "８０"])
def test_port_cannot_replace_host(port):
    with pytest.raises(ValueError):
        mvp.local_url({"PORT": port}, "PORT", 8065)


@pytest.mark.parametrize(
    "base",
    [
        "http://evil.test:8065",
        "https://localhost:8065",
        "http://localhost:8065@evil.test",
        "http://localhost:8065/path",
    ],
)
def test_bootstrap_rejects_nonlocal_base(base):
    with pytest.raises(ValueError):
        mvp.api(base, "GET", "/system/ping", token="secret-test-token")


def test_bearer_is_not_forwarded_by_redirect():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            if self.path.endswith("redirect"):
                self.send_response(302)
                self.send_header("Location", "/api/v4/leak")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            mvp.api(
                f"http://127.0.0.1:{server.server_port}", "GET", "/redirect", token="test-token"
            )
        assert error.value.code == 302
        assert requests == [("/api/v4/redirect", "Bearer test-token")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
