"""Read-only server for the generated Scandinavian course.

Serves the generated course file and, on demand, the evidence behind any item
(Masters, Lichess and engine), read straight from the analysis database. It writes
nothing, and it never computes a new evaluation or fetches a new distribution.
"""
import argparse
import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COURSE = ROOT / 'analysis' / 'course.json'
UI = ROOT / 'course_ui'
DEFAULT_DB = ROOT / 'data' / 'atlas-analysis.sqlite'


class Handler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB

    def log_message(self, *args):        # keep the console readable
        pass

    def _send(self, payload, content_type='application/json', status=200):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ('/', '/index.html'):
            return self._send((UI / 'index.html').read_bytes(), 'text/html; charset=utf-8')
        if parsed.path == '/course.json':
            if not COURSE.exists():
                return self._send({'error': 'course.json not generated yet'}, status=404)
            return self._send(COURSE.read_bytes())
        if parsed.path == '/evidence':
            query = parse_qs(parsed.query)
            key = (query.get('position_key') or [''])[0]
            return self._send(evidence(key, self.db_path))
        return self._send({'error': 'not found'}, status=404)


def evidence(position_key_hex, db_path=DEFAULT_DB):
    """The raw evidence behind one board: Masters, Lichess, engine, and the position row."""
    if not position_key_hex:
        return {'error': 'position_key required'}
    try:
        key = bytes.fromhex(position_key_hex)
    except ValueError:
        return {'error': 'position_key must be hex'}
    connection = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    out = {'position_key': position_key_hex}
    row = connection.execute('SELECT fen, full_fen, ply, role, structure_id, domain '
                             'FROM position WHERE position_key=?', (key,)).fetchone()
    if row:
        out['position'] = dict(row)
    out['distributions'] = {}
    for source in ('local2200', 'existing_local_lichess_cache', 'lichess'):
        rows = connection.execute(
            'SELECT uci, san, games, white, draws, black FROM move_source '
            'WHERE position_key=? AND source=? ORDER BY games DESC LIMIT 12',
            (key, source)).fetchall()
        if rows:
            out['distributions'][source] = [dict(entry) for entry in rows]
    if row:
        evals = connection.execute(
            'SELECT source, depth, engine, nodes, multipv, pvs_json FROM eval '
            'WHERE fen=? ORDER BY depth DESC LIMIT 3', (row['fen'],)).fetchall()
        out['engine'] = [dict(entry) for entry in evals]
    connection.close()
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8790)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    options = parser.parse_args()
    Handler.db_path = options.db
    server = ThreadingHTTPServer(('127.0.0.1', options.port), Handler)
    print(f'course UI on http://127.0.0.1:{options.port}/  (read-only; course file '
          f'{"present" if COURSE.exists() else "MISSING"})', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
