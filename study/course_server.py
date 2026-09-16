"""Read-only server for the generated Scandinavian course.

Serves the frontend, the generated course file, and — on demand — the evidence behind
any item (Masters, Lichess and engine), the structural flow between the course's
structures for the opening map, and alternate representative move orders for a family.

It writes nothing, computes no new evaluation, fetches no new distribution and changes
no part of the frozen curriculum: everything it returns is read from the analysis
database or from the already-generated course file.
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
    # the page pulls ~32 piece images plus two large JSON payloads at once; the default
    # backlog of 5 dropped connections under that burst, which silently tripped the
    # piece-image fallback path
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):        # keep the console readable
        pass

    def _send(self, payload, content_type='application/json', status=200):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        # the UI is edited while it is being reviewed, so stale caches are worse than a
        # extra few kilobytes per load
        self.send_header('Cache-Control', 'no-store, must-revalidate')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path in ('/', '/index.html', '/ui', '/ui/'):
            return self._send((UI / 'index.html').read_bytes(), 'text/html; charset=utf-8')
        if parsed.path.startswith('/ui/'):
            return self._static(parsed.path)
        if parsed.path == '/course.json':
            if not COURSE.exists():
                return self._send({'error': 'course.json not generated yet'}, status=404)
            return self._send(COURSE.read_bytes())
        if parsed.path == '/evidence':
            key = (query.get('position_key') or [''])[0]
            return self._send(evidence(key, self.db_path))
        if parsed.path == '/flow':
            return self._send(flow(self.db_path))
        if parsed.path == '/orders':
            family = (query.get('family') or [''])[0]
            return self._send(orders(family, self.db_path))
        return self._send({'error': 'not found'}, status=404)

    def _static(self, path):
        name = Path(path).name
        target = UI / name
        if not target.exists() or target.is_dir():
            return self._send({'error': 'not found'}, status=404)
        kinds = {'.css': 'text/css; charset=utf-8', '.js': 'application/javascript; charset=utf-8',
                 '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.jpg': 'image/jpeg',
                 '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp'}
        return self._send(target.read_bytes(),
                          kinds.get(target.suffix, 'application/octet-stream'))


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


def flow(db_path=DEFAULT_DB):
    """Structural transitions among the course's structures, for the opening map.

    Read from the existing flow graph with the existing research helper; nothing is
    recomputed beyond aggregating edges the project already produces.
    """
    from study import structure_flow
    db = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    positions, moves = structure_flow.load_graph_inputs(db, 'local2200')
    edges, summary = structure_flow.transition_edges(positions, moves)
    structures = {}
    for row in db.execute('SELECT structure_id, entry_mass, boards, ply_mean, development_level '
                          'FROM structure_maturity WHERE source=?', ('local2200',)):
        structures[row['structure_id']] = dict(row)
    families = set(structures)
    out_edges = []
    # transition_edges returns {(source_structure, destination_structure, move): mass}
    for (source, target, uci), mass in edges.items():
        if source in families and target in families and mass >= 0.005:
            out_edges.append({'from': source, 'to': target, 'mass': mass, 'move': uci})
    board_of = {}
    for row in db.execute('SELECT p.structure_id, p.position_key, p.fen, p.ply, '
                          'f.reach_flow FROM position p JOIN position_flow f '
                          'ON f.position_key = p.position_key WHERE f.source = ?',
                          ('local2200',)):
        reach = row['reach_flow'] or 0.0
        current = board_of.get(row['structure_id'])
        if current is None or reach > current['reach']:
            board_of[row['structure_id']] = {'fen': row['fen'], 'ply': row['ply'],
                                            'reach': reach}
    db.close()
    return {'structures': [{'id': key, **{k: v for k, v in value.items()},
                            'board': board_of.get(key)} for key, value in structures.items()],
            'edges': out_edges}


def orders(family, db_path=DEFAULT_DB, plies=10):
    """Representative move orders for a structure: the main line, then alternatives.

    Each order follows the highest-mass edge at every step; the alternatives differ at
    the first ply where another move carries real mass and then continue the same way,
    so a learner can see the same plan arising through different sequences. Read-only.
    """
    if not family:
        return {'error': 'family required'}
    from study import structure_flow
    db = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    positions, moves = structure_flow.load_graph_inputs(db, 'local2200')
    start = None
    for row in db.execute('SELECT position_key FROM position WHERE structure_id=?',
                          (family,)):
        if row['position_key'] in moves and moves[row['position_key']]:
            start = row['position_key']
            break
    if start is None:
        db.close()
        return {'orders': []}
    fens = {row['position_key']: row['fen'] for row in
            db.execute('SELECT position_key, fen FROM position')}
    db.close()

    def walk(first_choice):
        line, key = [], start
        for ply in range(plies):
            outgoing = sorted(moves.get(key) or [], key=lambda edge: -edge[2])
            if not outgoing:
                break
            pick = min(first_choice, len(outgoing) - 1)
            child, uci, games = outgoing[pick]
            line.append({'fen': fens.get(key, ''), 'uci': uci, 'games': games,
                         'ply': ply + 1})
            first_choice = 0
            key = child
        return line

    first = walk(0)
    alternatives = []
    for index in (1, 2):
        line = walk(index)
        if line and line not in alternatives and line != first:
            alternatives.append(line)
    return {'family': family, 'orders': [{'label': 'main line', 'moves': first}] +
            [{'label': f'alternative {i + 1}', 'moves': line}
             for i, line in enumerate(alternatives)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8790)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    options = parser.parse_args()
    Handler.db_path = options.db
    ThreadingHTTPServer.request_queue_size = 128
    ThreadingHTTPServer.daemon_threads = True
    server = ThreadingHTTPServer(('127.0.0.1', options.port), Handler)
    print(f'course UI on http://127.0.0.1:{options.port}/  (read-only; course file '
          f'{"present" if COURSE.exists() else "MISSING"})', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
