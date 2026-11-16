"""Local web page + JSON API for the tiny shop (synthetic demo; listens on 127.0.0.1 only)."""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from shop import cart_total

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Tiny Shop</title></head>
<body>
  <h1>Tiny Shop</h1>
  <label>Mugs: <input id="qty" type="number" min="1" value="1"></label>
  <button id="calc">Calculate</button>
  <p id="total">Total: -</p>
  <script>
    document.getElementById('calc').addEventListener('click', async () => {
      const qty = document.getElementById('qty').value;
      const res = await fetch('/api/total?sku=mug&qty=' + encodeURIComponent(qty));
      const data = await res.json();
      const out = document.getElementById('total');
      if (!res.ok) { out.textContent = 'Error: ' + data.error; return; }
      out.textContent = 'Total: $' + (data.total / 100).toFixed(2) + (data.label ? ' (' + data.label + ')' : '');
    });
  </script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body, ctype="application/json"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            return self._send(200, PAGE, "text/html")
        if url.path == "/health":
            return self._send(200, json.dumps({"ok": True}))
        if url.path == "/api/total":
            q = parse_qs(url.query)
            try:
                qty = int(q.get("qty", ["1"])[0])
                result = cart_total([(q.get("sku", ["mug"])[0], qty)])
            except (ValueError, KeyError) as e:
                return self._send(400, json.dumps({"error": f"bad request: {type(e).__name__}"}))
            return self._send(200, json.dumps(result))
        return self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, fmt, *args):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
