"""Local HTTP server for reviewing enrichment output on any device."""

from __future__ import annotations

import json
import logging
import socket
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

logger = logging.getLogger(__name__)


class ReviewHandler(SimpleHTTPRequestHandler):
    """Serves the review HTML and accepts disposition POSTs."""

    def __init__(self, *args, review_html: Path, dispositions_path: Path, **kwargs):
        self.review_html = review_html
        self.dispositions_path = dispositions_path
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            content = self.review_html.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/dispositions":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                dispositions = json.loads(body)
                self.dispositions_path.parent.mkdir(parents=True, exist_ok=True)
                self.dispositions_path.write_text(json.dumps(dispositions, indent=2))
                count = len(dispositions)
                logger.info("Saved %d dispositions to %s", count, self.dispositions_path)

                resp = json.dumps({"saved": count, "path": str(self.dispositions_path)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
            except (json.JSONDecodeError, Exception) as e:
                logger.error("Failed to save dispositions: %s", e)
                self.send_error(400, str(e))
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        logger.debug(format, *args)


def get_network_ips() -> dict[str, str]:
    """Get available network IPs for display."""
    import subprocess

    ips = {}

    # LAN IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips["LAN"] = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    # Tailscale IP
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            ts_ip = result.stdout.strip().split("\n")[0]
            if ts_ip:
                ips["Tailscale"] = ts_ip
    except Exception:
        pass

    return ips or {"Local": "localhost"}


def serve_review(review_html: Path, dispositions_path: Path, port: int = 8787) -> None:
    """Start a local HTTP server for the review report.

    Serves the HTML on all interfaces so it's accessible from phones
    on the same network or over Tailscale. Dispositions are saved via POST.
    """
    handler = partial(
        ReviewHandler,
        review_html=review_html,
        dispositions_path=dispositions_path,
    )
    server = HTTPServer(("0.0.0.0", port), handler)
    ips = get_network_ips()

    logger.info("Review server started")
    print(f"\n  Local:     http://localhost:{port}")
    for label, ip in ips.items():
        print(f"  {label + ':':10s} http://{ip}:{port}")
    print(f"\n  Open on your phone or any device on the same network.")
    print(f"  Dispositions will save to: {dispositions_path}")
    print(f"  Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()
