"""Candidate mature templates, their future transformations, and the compression frontier.

Tasks C, D and E.

C  descriptors for candidate mature families: flow-weighted occupancy, fixed and
   moving pawns, castling and development distributions, flexible piece slots,
   coverage, move-order diversity, dwell.
D  objective transformations over the next 6-12 plies of strong-play flow:
   pawn advances/breaks, piece relocations, exchanges, castling, files opening,
   recurring destination squares. Nothing is named or interpreted.
E  the compression frontier: greedy max-coverage selection over mature candidates,
   with near-duplicate families suppressed so that five similar boards cannot
   pretend to deliver five times the knowledge.
"""
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

import chess

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import db as studydb, family, structure_flow  # noqa: E402

PIECE_LETTER = {chess.PAWN: 'P', chess.KNIGHT: 'N', chess.BISHOP: 'B', chess.ROOK: 'R',
                chess.QUEEN: 'Q', chess.KING: 'K'}


def candidate_pool(db, source='local2200', min_mass=0.02, min_development=0.35,
                   min_dwell=2.0, min_boards=15):
    rows = db.execute('''SELECT * FROM structure_maturity
                         WHERE source=? AND entry_mass >= ? AND development_level >= ?
                           AND dwell_mean >= ? AND COALESCE(boards,0) >= ?
                         ORDER BY entry_mass DESC''',
                      (source, min_mass, min_development, min_dwell, min_boards)).fetchall()
    return [dict(row) for row in rows]


def pawn_distance(db, first, second):
    """Number of differing pawn squares between two families (0 = identical pawns)."""
    rows = {row['structure_id']: row for row in db.execute(
        'SELECT structure_id, white_pawns, black_pawns FROM structure '
        'WHERE structure_id IN (?,?)', (first, second))}
    if len(rows) < 2:
        return None
    a, b = rows[first], rows[second]
    return (bin(int(a['white_pawns'], 16) ^ int(b['white_pawns'], 16)).count('1')
            + bin(int(a['black_pawns'], 16) ^ int(b['black_pawns'], 16)).count('1'))


def descriptors(db, structure_id, source='local2200'):
    """Task C descriptor block for one family."""
    fam = family.load_family(db, structure_id, source)
    positions = fam['positions']
    if not positions:
        return None
    occupancy, _w = family.piece_occupancy(fam, 'reach_flow')
    occupancy_form, _w2 = family.piece_occupancy(fam, 'enter_mass')
    dwell = family.retention(fam)
    pawns = db.execute('SELECT white_pawns, black_pawns, material FROM structure '
                       'WHERE structure_id=?', (structure_id,)).fetchone()
    weights = {k: meta['reach'] for k, meta in positions.items()}
    castling, white_castling, development = Counter(), Counter(), Counter()
    for key, meta in positions.items():
        weight = weights[key]
        castling[meta['black_castled']] += weight
        white_castling[meta['white_castled']] += weight
        development[meta['dev_status']] += weight
    total = sum(weights.values()) or 1.0
    structure_row = db.execute('''SELECT n_positions, n_edges_in, n_parents, coverage_lower,
                                         coverage_upper, persistence_mean_plies, attractor_score
                                  FROM structure_stat WHERE structure_id=? AND source=?''',
                               (structure_id, source)).fetchone()
    return {
        'structure_id': structure_id, 'boards': len(positions),
        'entry_mass': dwell['entry_mass'], 'reach_sum': total,
        'mean_dwell': dwell['mean_dwell'], 'median_dwell': dwell['median_dwell'],
        'retention_3': dwell['retention_3'],
        'white_pawns': pawns['white_pawns'] if pawns else None,
        'black_pawns': pawns['black_pawns'] if pawns else None,
        'material': pawns['material'] if pawns else None,
        'occupancy': {name: family.occupancy_table(occupancy[name])[:4]
                      for name in family.PIECES if occupancy[name]},
        'occupancy_formation': {name: family.occupancy_table(occupancy_form[name])[:4]
                                for name in family.PIECES if occupancy_form[name]},
        'flexible_slots': family.flexible_slots(fam),
        'castling_black': {k: v / total for k, v in castling.items()},
        'castling_white': {k: v / total for k, v in white_castling.items()},
        'development': {k: v / total for k, v in development.items()},
        'stats': dict(structure_row) if structure_row else None,
        'pawn_diagram': family.render_pawns(int(pawns['white_pawns'], 16),
                                            int(pawns['black_pawns'], 16)) if pawns else '',
    }


# ------------------------------------------------------------------ task D
def forward_transformations(db, seeds, source='local2200', max_plies=12, windows=((1, 6), (7, 12)),
                            positions=None, moves=None, fen_of=None):
    """Objective transformations after the seed boards, weighted by flow mass.

    seeds: {position_key: mass} — normally the family's entering-mass distribution.
    positions/moves/fen_of may be supplied for tests; otherwise they are read from
    the analysis database.
    """
    if positions is None or moves is None:
        positions, moves = structure_flow.load_graph_inputs(db)
    fen_of = fen_of or (lambda key: positions_fen(db, key))
    order = sorted(positions, key=lambda k: (positions[k]['ply'], k))
    mass_at = defaultdict(float)
    for key, mass in seeds.items():
        if key in positions:
            mass_at[key] += mass
    seen = set(seeds)
    counters = {w: Counter() for w in windows}
    totals = {w: 0.0 for w in windows}
    piece_dest = {w: Counter() for w in windows}
    pawn_depart = {w: Counter() for w in windows}
    castling_events = {w: Counter() for w in windows}
    depth_of = {key: 0 for key in seeds}
    boards = {}
    for key in order:
        mass = mass_at.get(key, 0.0)
        if mass <= 0:
            continue
        outgoing = moves.get(key, [])
        total_games = sum(g for _c, _u, g in outgoing)
        if not total_games:
            continue
        depth = depth_of.get(key, 0)
        board = boards.get(key)
        if board is None:
            board = chess.Board(fen_of(key) + ' 0 1')
            boards[key] = board
        for child, uci, games in outgoing:
            share = games / total_games
            flow = mass * share
            move = chess.Move.from_uci(uci)
            window = next((w for w in windows if w[0] <= depth + 1 <= w[1]), None)
            if window is not None:
                totals[window] += flow
                piece = board.piece_at(move.from_square)
                if piece is not None:
                    if board.is_castling(move):
                        side = 'kingside' if chess.square_file(move.to_square) > 4 else 'queenside'
                        counters[window][f'castle_{"white" if piece.color else "black"}_{side}'] += flow
                        castling_events[window][f'{"white" if piece.color else "black"}_{side}'] += flow
                    if piece.piece_type == chess.PAWN:
                        counters[window][f'pawn {uci}'] += flow
                        pawn_depart[window][chess.square_name(move.from_square)] += flow
                    else:
                        letter = PIECE_LETTER[piece.piece_type]
                        counters[window][f'piece {letter} {uci[:2]}->{uci[2:4]}'] += flow
                        piece_dest[window][f'{letter}->{uci[2:4]}'] += flow
                    if board.is_capture(move):
                        victim = board.piece_at(move.to_square) if not board.is_en_passant(move) else None
                        victim_letter = PIECE_LETTER[victim.piece_type] if victim else 'P'
                        counters[window][f'capture {PIECE_LETTER[piece.piece_type]}x{victim_letter}'] += flow
                    child_position = positions.get(child)
                    if child_position and child_position['structure'] != positions[key]['structure']:
                        counters[window]['gives up the pawn structure'] += flow
                    after = board.copy(stack=False)
                    after.push(move)
                    before_open = _open_files(board)
                    after_open = _open_files(after)
                    for file in after_open - before_open:
                        counters[window][f'file {file} opens'] += flow
            if child in positions and child not in seen:
                seen.add(child)
                depth_of[child] = depth + 1
                mass_at[child] += flow
    return {'windows': {w: {'total': totals[w], 'moves': counters[w], 'destinations': piece_dest[w],
                            'pawn_departures': pawn_depart[w], 'castling': castling_events[w]}
                        for w in windows}}


_FEN_CACHE = {}


def positions_fen(db, key):
    if not _FEN_CACHE:
        for row in db.execute('SELECT position_key, fen FROM position'):
            _FEN_CACHE[row['position_key']] = row['fen']
    return _FEN_CACHE[key]


def _open_files(board):
    pawns = board.pieces(chess.PAWN, chess.WHITE) | board.pieces(chess.PAWN, chess.BLACK)
    return {chr(ord('a') + file) for file in range(8) if not (pawns & chess.BB_FILES[file])}


# ------------------------------------------------------------------ task E
def frontier(db, source='local2200', k_max=10, min_pawn_distance=3, pool=None):
    """Greedy max-coverage selection over mature candidates, near-duplicates suppressed."""
    positions, moves = structure_flow.load_graph_inputs(db)
    edges, _summary = structure_flow.transition_edges(positions, moves)
    leak = {}
    for structure, meta in _summary.items():
        leak[structure] = meta['leak_mass']
    entry_structure = db.execute('SELECT structure_id FROM position WHERE is_entry=1').fetchone()[0]
    pool = pool if pool is not None else candidate_pool(db, source)
    remaining = [row['structure_id'] for row in pool]
    selected, rows = [], []
    covered = 0.0
    for step in range(k_max):
        best, best_cover = None, covered
        for structure in remaining:
            cover, _mass = structure_flow.coverage_of_set(edges, entry_structure,
                                                          selected + [structure], leak=leak)
            if cover > best_cover + 1e-9:
                best, best_cover = structure, cover
        if best is None:
            break
        selected.append(best)
        remaining.remove(best)
        rows.append({'k': len(selected), 'structure_id': best,
                     'coverage': best_cover, 'marginal': best_cover - covered,
                     'entry_mass': next(r['entry_mass'] for r in pool if r['structure_id'] == best),
                     'development_level': next(r['development_level'] for r in pool
                                               if r['structure_id'] == best),
                     'dwell': next(r['dwell_mean'] for r in pool if r['structure_id'] == best)})
        covered = best_cover
        # suppress near-duplicates of what we just took
        remaining = [s for s in remaining
                     if (pawn_distance(db, s, best) or 99) >= min_pawn_distance]
    return rows, selected, covered, edges, leak, entry_structure


def similarity_matrix(db, structures):
    out = {}
    for i, a in enumerate(structures):
        for b in structures[i + 1:]:
            out[(a, b)] = pawn_distance(db, a, b)
    return out
