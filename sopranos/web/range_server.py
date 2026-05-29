"""A static file server that honors HTTP Range requests.

The stdlib ``http.server`` ignores ``Range`` and always returns the full file,
but ``sql.js-httpvfs`` fetches the SQLite DB with byte ranges and needs 206
partial-content responses (real static hosts like Cloudflare Pages / R2 do this
natively). This handler adds that, so the built ``dist/`` can be previewed
exactly as it will behave when deployed.
"""
from __future__ import annotations

import functools
import http.server
import os
import re
import socketserver
from pathlib import Path

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)\s*$")


class RangeHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive: many small range requests per page

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def do_GET(self):  # noqa: N802
        rng = self.headers.get("Range")
        path = self.translate_path(self.path)
        if not rng or not os.path.isfile(path):
            return super().do_GET()
        m = _RANGE_RE.match(rng.strip())
        if not m or (m.group(1) == "" and m.group(2) == ""):
            return super().do_GET()

        size = os.path.getsize(path)
        if m.group(1) == "":  # suffix range: last N bytes
            start, end = max(0, size - int(m.group(2))), size - 1
        else:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
        end = min(end, size - 1)
        if start >= size or start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return

        length = end - start + 1
        with open(path, "rb") as f:
            f.seek(start)
            self.send_response(206)
            self.send_header("Content-Type", self.guess_type(path))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


class ThreadingHTTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def make_server(directory: str | Path, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    handler = functools.partial(RangeHTTPRequestHandler, directory=str(Path(directory).resolve()))
    return ThreadingHTTPServer((host, port), handler)
