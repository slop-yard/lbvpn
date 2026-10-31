"""lbvpn mxrelay - fetch-as-a-service, backed by mitmproxy.

    GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS  {BASE}/?url=<target>[&k=v ...]

- `<target>` is fetched with the incoming method, headers and body
- extra query parameters are merged into the target's query (last wins)
- the response is returned with the target's status, headers and body
- every fetch egresses through the upstream proxy
  (mitmproxy -> LB pool -> Proton exit) and is visible in mitmweb

Only hop-by-hop and transport-managed headers are stripped; everything else
passes through untouched, so to the target this is a plain client fetch.
"""
import hmac
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("MXRELAY_PORT", "9001"))
PROXY = os.environ.get("MXRELAY_PROXY", "http://mitm:8080")
TIMEOUT = int(os.environ.get("MXRELAY_TIMEOUT", "180"))
AUTH = os.environ.get("MXRELAY_AUTH_TOKEN", "")

# hop-by-hop + transport-managed headers are never forwarded
STRIP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "proxy-connection",
    "host", "content-length",
}

# through mitmproxy the TLS towards us is intercepted (its CA, not the
# target's) - mitm still validates the target's certificate on its side, so
# the only relaxed check is towards our own mitm.
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}),
    urllib.request.HTTPSHandler(context=SSL_CTX),
)


def fetch(method, url, headers, body):
    request = urllib.request.Request(url, data=body, method=method)
    for name, value in headers:
        request.add_header(name, value)
    try:
        with OPENER.open(request, timeout=TIMEOUT) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        # 4xx/5xx are valid answers: relay them instead of raising
        try:
            content = error.read()
        except Exception:  # noqa: BLE001
            content = b""
        return error.code, error.headers, content


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"[mxrelay] {self.command} {self.path}", flush=True)

    def send_response(self, code, message=None):
        # pass the target's headers through: no synthetic Server/Date
        self.log_request(code)
        self.send_response_only(code, message)

    def do_GET(self): self._relay()
    do_POST = do_GET
    do_PUT = do_GET
    do_PATCH = do_GET
    do_DELETE = do_GET
    do_OPTIONS = do_GET
    do_HEAD = do_GET

    def _authorized(self):
        # header name is case-insensitive; value is the raw token or "Bearer <token>"
        provided = self.headers.get("X-MX-Relay-Auth") or ""
        if provided.startswith("Bearer "):
            provided = provided[len("Bearer "):]
        # bytes compare never raises on non-ascii header values (a weird header
        # gets a clean 401, not a TypeError/500)
        return bool(AUTH) and hmac.compare_digest(
            provided.encode("utf-8"), AUTH.encode("utf-8"))

    def _relay(self):
        if not self._authorized():
            # drain the body (if any) so keep-alive framing stays intact
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            return self._raw(401, "Unauthorized: missing or invalid X-MX-RELAY-AUTH header")

        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
        target = (query.get("url") or [""])[0].strip()
        if not target:
            return self._raw(400, "Target unspecified: missing ?url= parameter")

        # merge extra query parameters into the target's query (last wins)
        parts = urllib.parse.urlsplit(target)
        merged = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        for key, values in query.items():
            if key != "url":
                merged[key] = values[-1]
        target = urllib.parse.urlunsplit((
            parts.scheme, parts.netloc, parts.path,
            urllib.parse.urlencode(merged), parts.fragment))

        headers = [(k, v) for k, v in self.headers.items()
                   if k.lower() not in STRIP]
        body = None
        if self.command not in ("GET", "HEAD"):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None

        print(f"[mxrelay] -> {self.command} {target}", flush=True)
        try:
            status, resp_headers, content = fetch(self.command, target, headers, body)
        except Exception as exc:  # noqa: BLE001 - surface any upstream failure
            return self._raw(500, f"Proxy error: {exc}")
        return self._respond(status, resp_headers, content)

    def _respond(self, status, headers, content):
        self.send_response(status)
        for name, value in headers.items():
            if name.lower() in STRIP or name.lower() == "content-length":
                continue
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def _raw(self, status, text):
        body = text.encode() + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


if __name__ == "__main__":
    print(f"[mxrelay] fetch-as-a-service on :{PORT} via {PROXY}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
