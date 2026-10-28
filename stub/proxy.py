"""Tiny stand-in for a gluetun HTTP proxy, for testing the LB without a VPN.

Listens on :8888 like gluetun's built-in proxy.
- plain HTTP requests  -> answers with "STUB <NAME>" so you can see routing
- HTTPS CONNECT        -> tunnels transparently (we can't fake inside TLS)
"""
import os
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler

NAME = os.environ.get("STUB_NAME", "stub")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        print(f"[{NAME}] {self.command} {self.path}", flush=True)

    def _reply(self):
        body = f"STUB {NAME}\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _reply
    do_POST = _reply

    def do_CONNECT(self):
        host, _, port = self.path.partition(":")
        try:
            upstream = socket.create_connection((host, int(port or 443)), timeout=10)
        except OSError:
            self.send_error(502, "Bad Gateway")
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        self.connection.settimeout(None)
        self._tunnel(self.connection, upstream)

    @staticmethod
    def _pump(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def _tunnel(self, client, upstream):
        threading.Thread(target=self._pump, args=(client, upstream), daemon=True).start()
        self._pump(upstream, client)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print(f"stub proxy '{NAME}' listening on :8888", flush=True)
    Server(("0.0.0.0", 8888), Handler).serve_forever()
