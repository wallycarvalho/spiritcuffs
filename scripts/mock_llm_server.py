"""Throwaway mock OpenAI-compatible server for dogfooding `spc learn` offline.

Serves one endpoint (POST /v1/chat/completions) returning a canned profile.
Not part of the package — demo scaffolding only.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

PROFILE = (
    "# Repo profile (mock synthesis)\n\n"
    "This repository is a small pure-Python CLI package (`spc/`) with a flat test suite "
    "in `tests/`. Git history is young (1 commit), so mining found no hotspots or bug "
    "classes yet. Expect this profile to sharpen as history accumulates.\n"
)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 — http.server API
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps(
            {
                "choices": [
                    {"message": {"content": PROFILE}},
                ]
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass  # keep demo output clean


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8931), Handler).serve_forever()
