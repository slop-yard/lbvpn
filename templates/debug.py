"""lbvpn debug consumer - answers "is the chain actually leaving through the VPN?"

GET /            every target
GET /<name>      one target

Each target is reached through a proxy, so the answer is what the exit looks
like from the outside:

  pool  -> mitm:8080 -> lb pool -> a Proton exit       (full chain)
  <reg> -> lb:3131   -> vpn-<reg> -> that Proton exit  (exact region)

TLS is not verified: through mitmproxy the request is intercepted anyway, and
this is a diagnostic, so a private address or a repeated address is the
interesting signal - and it is reported explicitly.
"""
import ipaddress
import json
import os
import ssl
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGETS = json.loads(os.environ["DEBUG_TARGETS"])
ECHO = os.environ.get("DEBUG_ECHO", "https://api.ipify.org/?format=json")
PORT = int(os.environ.get("DEBUG_PORT", "9000"))

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


def classify(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return "not-an-ip"
    if address.is_private or address.is_loopback or address.is_link_local:
        return "private"
    return "public"


def egress(proxy):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": f"http://{proxy}", "https": f"http://{proxy}"}),
        urllib.request.HTTPSHandler(context=SSL_CTX),
    )
    try:
        with opener.open(ECHO, timeout=25) as response:
            raw = response.read().decode().strip()
    except Exception as exc:  # noqa: BLE001 - report whatever went wrong
        return {"error": f"{type(exc).__name__}: {exc}"}
    try:
        ip = json.loads(raw).get("ip", raw)
    except ValueError:
        ip = raw
    return {"ip": ip, "kind": classify(ip)}


def probe(names):
    exits = {}
    for name in names:
        proxy = TARGETS.get(name)
        exits[name] = (
            {"error": f"unknown target (have: {', '.join(TARGETS)})"}
            if proxy is None
            else {"via": proxy, **egress(proxy)}
        )

    problems = [f"{name}: {info['error']}" for name, info in exits.items() if "error" in info]
    ips = [info["ip"] for info in exits.values() if "ip" in info]
    for name, info in exits.items():
        if info.get("kind") == "private":
            problems.append(f"{name}: {info['ip']} is a private address - traffic is not leaving the network")
        elif info.get("kind") == "not-an-ip":
            problems.append(f"{name}: got {info['ip']!r} instead of an address")
    if len(ips) > 1 and len(set(ips)) == 1:
        problems.append(
            f"every exit reports the same address ({ips[0]}) - "
            "either the exits are not distinct or nothing is being tunneled"
        )

    return {
        "ok": not problems,
        "distinct": len(set(ips)),
        "exits": exits,
        "problems": problems,
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        print(f"[debug] {self.command} {self.path}", flush=True)

    def do_GET(self):
        wanted = self.path.split("?", 1)[0].strip("/")
        if wanted and wanted not in TARGETS:
            names = [wanted]
        else:
            names = [wanted] if wanted else list(TARGETS)
        body = (json.dumps(probe(names), indent=2) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    print(f"[debug] listening on :{PORT}, targets: {', '.join(TARGETS)}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
