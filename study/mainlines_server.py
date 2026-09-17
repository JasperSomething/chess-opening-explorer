"""Read-only server for the opening mainlines dataset.

Serves the UI, the generated mainlines file, and nothing else. No writes, no
evaluation, no network: everything comes from analysis/mainlines.json, which was
derived offline from data/lumbra-2200.sqlite.
"""
import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / 'mainlines_ui'
DATA = ROOT / 'analysis' / 'mainlines.json'

KINDS = {'.css': 'text/css; charset=utf-8', '.js': 'application/javascript; charset=utf-8',
         '.svg': 'image/svg+xml', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
         '.png': 'image/png', '.webp': 'image/webp', '.woff2': 'font/woff2'}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):        # keep the console readable
        pass

    def _send(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store, must-revalidate')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/index.html', '/ui', '/ui/'):
            return self._send((UI / 'index.html').read_bytes(), 'text/html; charset=utf-8')
        if path == '/mainlines.json':
            if not DATA.exists():
                return self._send(json.dumps({'error': 'mainlines.json not generated'}).encode(),
                                  'application/json', 404)
            return self._send(DATA.read_bytes(), 'application/json')
        if path.startswith('/ui/'):
            target = UI / Path(path).name
            if not target.exists() or target.is_dir():
                return self._send(b'{"error":"not found"}', 'application/json', 404)
            return self._send(target.read_bytes(),
                              KINDS.get(target.suffix, 'application/octet-stream'))
        return self._send(b'{"error":"not found"}', 'application/json', 404)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8790)
    options = parser.parse_args()
    if not DATA.exists():
        sys.exit(f'missing {DATA}; run the generator first')
    ThreadingHTTPServer.request_queue_size = 64
    ThreadingHTTPServer.daemon_threads = True
    server = ThreadingHTTPServer(('127.0.0.1', options.port), Handler)
    count = len(json.loads(DATA.read_bytes())['openings'])
    print(f'opening mainlines on http://127.0.0.1:{options.port}/  '
          f'({count} openings, read-only)', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
