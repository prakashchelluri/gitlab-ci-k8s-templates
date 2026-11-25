"""Tiny HTTP service used to exercise the pipeline templates.

GET /         -> {"service": "hello-app", "version": "<APP_VERSION>"}
GET /healthz  -> {"status": "ok"}   (readiness and liveness probe)
"""

import json
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = os.environ.get("APP_VERSION", "dev")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self._send(200, {"status": "ok"})
        elif self.path == "/":
            self._send(200, {"service": "hello-app", "version": VERSION})
        else:
            self._send(404, {"error": "not found"})

    def _send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(port):
    server = ThreadingHTTPServer(("", port), Handler)
    # Kubernetes sends SIGTERM on rollout; finish in-flight requests and exit cleanly.
    # shutdown() blocks until serve_forever() returns, so it must run on another thread.
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    print(f"listening on :{port}, version {VERSION}", flush=True)
    server.serve_forever()
    server.server_close()


if __name__ == "__main__":
    serve(int(os.environ.get("PORT", "8080")))
