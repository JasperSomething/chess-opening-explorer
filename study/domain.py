"""Build the Scandinavian (1.e4 d5) domain subgraph inside the analysis database.

Reads the ingestion databases read-only. This is the "derived reverse-edge /
provenance index" the research plan requires: instead of modifying the complete
2200+ importer (which has no incoming-edge lookup), every (position, move) edge
we read is replayed with python-chess and the resulting child key is written to
`provenance` as (child, parent, move, source, games). That is exact, needs no
rebuild of ingestion data, and costs one board push per edge.

Cost model for a later full-scale version: one push_uci plus one key computation
per edge over the whole edge table. For the complete 2200+ index that is ~10^7
edges, i.e. minutes of CPU and a table of roughly 40 bytes per edge. It is a
real batch job, not an interactive one. For a bounded domain like this one it is
seconds, which is why the proof of concept builds the reverse index directly.
"""
import time
from collections import defaultdict, deque

import chess

import complete_2200
from study import db as studydb
from study import structures
from study.evals import canonical_fen

ENTRY_MOVES = ('e2e4', 'd7d5')

EXPANDING = ('local2200', 'local2200all', 'lichess', 'masters')
ENRICHING = ('local2200complete', 'allgames')

DEFAULT_CONFIG = {
    'domain': 'scandinavian-1e4-d5',
    'max_ply': 34,
    'max_positions': 20000,
    'min_games': {'local2200': 10, 'local2200all': 10, 'lichess': 50, 'masters': 10},
    'expanding': EXPANDING,
    'enriching': ENRICHING,
}


def entry_board():
    board = chess.Board()
    for uci in ENTRY_MOVES:
        board.push_uci(uci)
    return board


def child_board(board, uci):
    child = board.copy(stack=False)
    child.push_uci(uci)
    return child


class Ingest:
    """Read-only access to the ingestion databases."""

    def __init__(self):
        self.handles = {}
        self.mismatches = 0
        self.checked_targets = 0

    def _db(self, name):
        if name not in self.handles:
            self.handles[name] = studydb.readonly(studydb.INGEST[name])
        return self.handles[name]

    def close(self):
        for handle in self.handles.values():
            handle.close()
        self.handles.clear()

    def position(self, board, source):
        """(games, white, draws, black) or None when the source cannot know this position."""
        fen = canonical_fen(board)
        if source in ('local2200', 'local2200all'):
            row = self._db('local2200').execute(
                "SELECT white,draws,black,white2200,draws2200,black2200 FROM counts "
                "WHERE position=? AND uci=''", (fen,)).fetchone()
            if not row:
                return None
            w, d, b = (row[3], row[4], row[5]) if source == 'local2200' else (row[0], row[1], row[2])
            return (w + d + b, w, d, b)
        if source in ('lichess', 'masters'):
            row = self._db('lichess').execute(
                'SELECT white,draws,black FROM snapshots WHERE position=? AND source=?',
                (fen, source)).fetchone()
            return None if not row else (row[0] + row[1] + row[2], row[0], row[1], row[2])
        if source == 'local2200complete':
            row = self._db('local2200complete').execute(
                'SELECT SUM(white+draws+black), SUM(white), SUM(draws), SUM(black) FROM edges '
                'WHERE position=?', (complete_2200.compact_key(board),)).fetchone()
            if not row or row[0] is None:
                return None
            return (row[0], row[1] or 0, row[2] or 0, row[3] or 0)
        if source == 'allgames':
            row = self._db('allgames').execute(
                "SELECT white,draws,black FROM counts WHERE position=? AND uci=''",
                (fen,)).fetchone()
            return None if not row else (row[0] + row[1] + row[2], row[0], row[1], row[2])
        raise KeyError(source)

    def moves(self, board, source):
        """[(uci, san, games, white, draws, black, target_fen)] at this position."""
        fen = canonical_fen(board)
        rows = []
        if source in ('local2200', 'local2200all'):
            columns = 'white2200,draws2200,black2200' if source == 'local2200' else 'white,draws,black'
            for uci, w, d, b in self._db('local2200').execute(
                    f'SELECT uci,{columns} FROM counts WHERE position=? AND uci!=""', (fen,)):
                rows.append((uci, None, w + d + b, w, d, b, None))
        elif source in ('lichess', 'masters'):
            for uci, san, w, d, b, target in self._db('lichess').execute(
                    'SELECT uci,san,white,draws,black,target FROM moves WHERE position=? AND source=?',
                    (fen, source)):
                rows.append((uci, san, w + d + b, w, d, b, target))
        elif source == 'local2200complete':
            for move, w, d, b in self._db('local2200complete').execute(
                    'SELECT move,white,draws,black FROM edges WHERE position=?',
                    (complete_2200.compact_key(board),)):
                if not move:
                    continue
                rows.append((complete_2200.decode_move(move).uci(), None, w + d + b, w, d, b, None))
        elif source == 'allgames':
            for uci, w, d, b in self._db('allgames').execute(
                    'SELECT uci,white,draws,black FROM counts WHERE position=? AND uci!=""', (fen,)):
                rows.append((uci, None, w + d + b, w, d, b, None))
        else:
            raise KeyError(source)
        return [row for row in rows if row[2] > 0]

    def replay_target(self, board, uci, target_fen):
        """Replay one move; verify against a stored child key when the source provides one."""
        computed = canonical_fen(child_board(board, uci))
        if target_fen is not None:
            self.checked_targets += 1
            if computed != target_fen:
                self.mismatches += 1
        return computed


def build(db, run_id, config=None, verbose=True):
    config = {**DEFAULT_CONFIG, **(config or {})}
    ingest = Ingest()
    domain = config['domain']
    expanding = list(config['expanding'])
    sources = list(dict.fromkeys(expanding + list(config['enriching'])))

    entry = entry_board()
    entry_key = complete_2200.compact_key(entry)

    entry_games = {}
    for source in sources:
        total = ingest.position(entry, source)
        entry_games[source] = total[0] if total else None

    # ---- graph walk (BFS by ply: the first visit to a position is its minimum ply)
    queue = deque([(entry, len(ENTRY_MOVES))])
    discovered = {entry_key: (canonical_fen(entry), len(ENTRY_MOVES), entry.fen())}
    while queue and len(discovered) < config['max_positions']:
        board, ply = queue.popleft()
        for source in expanding:
            total = ingest.position(board, source)
            if not total or total[0] < config['min_games'].get(source, 10):
                continue
            for uci, _san, _games, _w, _d, _b, _target in ingest.moves(board, source):
                child = child_board(board, uci)
                child_key = complete_2200.compact_key(child)
                if child_key in discovered:
                    continue
                if ply + 1 > config['max_ply']:
                    continue
                if len(discovered) >= config['max_positions']:
                    break
                discovered[child_key] = (canonical_fen(child), ply + 1, child.fen())
                queue.append((child, ply + 1))

    if verbose:
        print(f'walked {len(discovered)} positions')

    now = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    edge_count = 0
    source_counts = defaultdict(int)

    for key, (fen, ply, full_fen) in discovered.items():
        board = chess.Board(fen + ' 0 1')
        fp = structures.fingerprint(board)
        db.execute('''INSERT OR REPLACE INTO position(position_key, fen, full_fen, domain, ply, min_ply,
                        role, is_entry, structure_id, white_pawns, black_pawns, pieces_json,
                        castling_rights, white_castled, black_castled, dev_white, dev_black,
                        dev_status, run_id)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (key, fen, full_fen, domain, ply, ply,
                    'white_to_move' if board.turn == chess.WHITE else 'black_to_move',
                    1 if key == entry_key else 0, fp['structure_id'],
                    f"{fp['white_pawns']:016x}", f"{fp['black_pawns']:016x}",
                    fp['pieces_json'], f"{fp['castling_rights']:016x}",
                    fp['white_castled'], fp['black_castled'], fp['dev_white'], fp['dev_black'],
                    fp['dev_status'], run_id))
        db.execute('''INSERT OR REPLACE INTO structure(structure_id, white_pawns, black_pawns,
                        centre_config, open_files, semi_open_white, semi_open_black, islands_white,
                        islands_black, isolated_white, isolated_black, doubled_white, doubled_black,
                        backward_white, backward_black, backward_approximate, passed_white,
                        passed_black, material)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (fp['structure_id'], f"{fp['white_pawns']:016x}", f"{fp['black_pawns']:016x}",
                    fp['centre_config'],
                    fp['open_files'], fp['semi_open_white'], fp['semi_open_black'],
                    fp['islands_white'], fp['islands_black'], fp['isolated_white'],
                    fp['isolated_black'], fp['doubled_white'], fp['doubled_black'],
                    fp['backward_white'], fp['backward_black'], 1, fp['passed_white'],
                    fp['passed_black'], fp['material']))

        for source in sources:
            policy = studydb.SOURCE_POLICY.get(source, {'coverage': 'partial'})
            total = ingest.position(board, source)
            if total is None:
                state = 'zero' if policy['coverage'] == 'complete' else 'absent'
                db.execute('''INSERT OR REPLACE INTO position_source(position_key, source, games,
                                white, draws, black, entry_games, reach_prob, coverage_state, retrieved)
                              VALUES(?,?,?,?,?,?,?,?,?,?)''',
                           (key, source, 0, 0, 0, 0, entry_games.get(source), None, state, now))
            else:
                games, w, d, b = total
                denom = entry_games.get(source)
                db.execute('''INSERT OR REPLACE INTO position_source(position_key, source, games,
                                white, draws, black, entry_games, reach_prob, coverage_state, retrieved)
                              VALUES(?,?,?,?,?,?,?,?,?,?)''',
                           (key, source, games, w, d, b, denom,
                            (games / denom) if denom else None, policy['coverage'], now))
            if not total or total[0] < config['min_games'].get(source, 0):
                continue
            moves = ingest.moves(board, source)
            games_total = sum(row[2] for row in moves)
            source_counts[source] += len(moves)
            for uci, san, games, w, d, b, target in moves:
                move = chess.Move.from_uci(uci)
                san = san or board.san(move)
                child_fen = ingest.replay_target(board, uci, target)
                child_key = complete_2200.compact_key(child_board(board, uci))
                db.execute('''INSERT OR REPLACE INTO move_source(position_key, source, uci, san,
                                games, white, draws, black, share)
                              VALUES(?,?,?,?,?,?,?,?,?)''',
                           (key, source, uci, san, games, w, d, b,
                            games / games_total if games_total else 0.0))
                db.execute('''INSERT OR REPLACE INTO provenance(child_key, parent_key, uci, source, games)
                              VALUES(?,?,?,?,?)''',
                           (child_key, key, uci, source, games))
                edge_count += 1

    db.commit()
    summary = {'positions': len(discovered), 'move_rows': edge_count,
               'entry_fen': canonical_fen(entry), 'entry_games': entry_games,
               'targets_checked': ingest.checked_targets, 'target_mismatches': ingest.mismatches,
               'moves_per_source': dict(source_counts),
               'truncated_at_cap': len(discovered) >= config['max_positions'],
               'max_ply': config['max_ply'], 'min_games': config['min_games']}
    ingest.close()
    return summary
