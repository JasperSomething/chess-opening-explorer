"""Maturity measure: separating "strategically developed" from "merely deep".

Research task B. Each component is computed separately and stored separately; the
composite is reported next to its parts, never instead of them.

Components (all family-conditioned, weighted by family board-visits unless stated):

  minors_developed      knights and bishops off their home squares, per side, /4
  castled               castled (1), king off home without castling (0.5), else 0
  centre_pawns_left     of the d- and e-pawn, how many have left home, per side /2
  centre_files_touched  of the c,d,e,f pawns, how many are no longer on their home
                        square or have been traded, per side /4
  dwell_mean/median     plies a game spends in the family (game-level simulator)
  retention_3           share of entering mass still in the family after 3 plies
  reconvergence         distinct parent boards per board of the family: how many
                        different move orders funnel into the same structure
  effective_successors  exp(entropy) of the outgoing transition mass: how far the
                        family fragments instead of continuing down one channel
  ply_mean              mean ply of the family's boards

Depth adjustment: every component is also reported as a z-residual inside its ply
bucket, because development grows with depth by construction. The composite
`maturity_index` uses only the depth-adjusted residuals of the strategic
components (minors, castling, centre), so "deeper" alone cannot buy maturity.
"""
import json
import math
from collections import defaultdict
from pathlib import Path
import sys

import chess

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import db as studydb  # noqa: E402

HOME = {
    'wn': {'b1', 'g1'}, 'wb': {'c1', 'f1'},
    'bn': {'b8', 'g8'}, 'bb': {'c8', 'f8'},
}
COMPONENTS = ('minors_white', 'minors_black', 'castled_white', 'castled_black',
              'centre_left_white', 'centre_left_black', 'centre_files_white',
              'centre_files_black', 'dwell_mean', 'retention_3', 'reconvergence',
              'effective_successors', 'ply_mean')


def _squares(bitboard):
    out = []
    while bitboard:
        square = (bitboard & -bitboard).bit_length() - 1
        out.append(chess.square_name(square))
        bitboard &= bitboard - 1
    return out


def position_components(pieces, castling):
    """The per-side components for a single board."""
    def minors(colour):
        return sum(1 for name in (f'{colour}n', f'{colour}b')
                   for square in _squares(pieces[name]) if square not in HOME[name]) / 4.0

    def centre_left(colour):
        home_pawns = ('d2', 'e2') if colour == 'w' else ('d7', 'e7')
        pawns = _squares(pieces[f'{colour}p'])
        return sum(1 for square in home_pawns if square not in pawns) / 2.0

    def centre_files(colour):
        files = ('c', 'd', 'e', 'f')
        home_rank = '2' if colour == 'w' else '7'
        pawns = {s for s in _squares(pieces[f'{colour}p'])}
        touched = 0
        for file in files:
            if f'{file}{home_rank}' not in pawns:
                touched += 1
        return touched / 4.0

    def castled(colour):
        state = castling[f'{colour}_castled']
        if state.startswith('castled'):
            return 1.0
        if state == 'king_moved':
            return 0.5
        return 0.0

    return {'minors_white': minors('w'), 'minors_black': minors('b'),
            'castled_white': castled('w'), 'castled_black': castled('b'),
            'centre_left_white': centre_left('w'), 'centre_left_black': centre_left('b'),
            'centre_files_white': centre_files('w'), 'centre_files_black': centre_files('b')}


def family_components(db, source='local2200'):
    """Flow-weighted components per structure, plus dwell and convergence."""
    structures = defaultdict(lambda: defaultdict(float))
    weights = defaultdict(float)
    for row in db.execute('''SELECT p.position_key, p.structure_id, p.pieces_json, p.ply,
                                    p.white_castled, p.black_castled, f.reach_flow
                             FROM position p
                             JOIN position_flow f ON f.position_key = p.position_key
                                  AND f.source = ?''', (source,)):
        weight = row['reach_flow'] or 0.0
        if weight <= 0:
            continue
        pieces = {name: int(value) for name, value in json.loads(row['pieces_json']).items()}
        parts = position_components(pieces, {'w_castled': row['white_castled'],
                                             'b_castled': row['black_castled']})
        bucket = structures[row['structure_id']]
        for name, value in parts.items():
            bucket[name] += weight * value
        bucket['ply_mean'] += weight * row['ply']
        weights[row['structure_id']] += weight

    for structure, bucket in structures.items():
        total = weights[structure] or 1.0
        for name in ('minors_white', 'minors_black', 'castled_white', 'castled_black',
                     'centre_left_white', 'centre_left_black', 'centre_files_white',
                     'centre_files_black', 'ply_mean'):
            bucket[name] /= total

    for row in db.execute('''SELECT structure_id, persistence_mean_plies, n_positions,
                                    components_json FROM structure_stat WHERE source = ?''',
                          (source,)):
        components = json.loads(row['components_json'])
        bucket = structures[row['structure_id']]
        bucket['dwell_mean'] = row['persistence_mean_plies'] or 0.0
        bucket['retention_3'] = components.get('retention_3') or 0.0
        bucket['boards'] = row['n_positions']

    for row in db.execute('''SELECT destination_key, SUM(mass) AS mass,
                                    COUNT(DISTINCT source_key) AS sources
                             FROM structure_edge WHERE source=? GROUP BY destination_key''',
                          (source,)):
        bucket = structures[row['destination_key']]
        bucket['incoming_mass'] = row['mass'] or 0.0
        bucket['source_families'] = row['sources'] or 0

    outgoing = defaultdict(list)
    for row in db.execute('SELECT source_key, destination_key, mass FROM structure_edge '
                          'WHERE source=?', (source,)):
        outgoing[row['source_key']].append(row['mass'])
    for structure, masses in outgoing.items():
        total = sum(masses)
        if total <= 0:
            continue
        entropy = -sum((m / total) * math.log(m / total, 2) for m in masses if m > 0)
        structures[structure]['effective_successors'] = 2 ** entropy

    for row in db.execute('''SELECT p.structure_id, COUNT(DISTINCT pr.parent_key) AS parents,
                                    COUNT(*) AS boards
                             FROM position p
                             JOIN provenance pr ON pr.child_key = p.position_key
                             WHERE pr.source = ? GROUP BY p.structure_id''', (source,)):
        bucket = structures[row['structure_id']]
        bucket['reconvergence'] = (row['parents'] / row['boards']) if row['boards'] else 0.0

    for bucket in structures.values():
        bucket.setdefault('effective_successors', 0.0)
        bucket.setdefault('dwell_mean', 0.0)
        bucket.setdefault('reconvergence', 0.0)
        bucket.setdefault('retention_3', 0.0)
    return structures


def ply_adjust(structures, components=('minors_white', 'minors_black', 'castled_white',
                                       'castled_black', 'centre_left_white',
                                       'centre_left_black')):
    """Z-residual of each component inside its ply bucket, to discount depth."""
    buckets = defaultdict(list)
    for structure, values in structures.items():
        buckets[int(values.get('ply_mean', 0))].append(structure)
    residuals = defaultdict(dict)
    for _ply, members in buckets.items():
        if len(members) < 3:
            continue
        for name in components:
            series = [structures[s].get(name, 0.0) for s in members]
            mean = sum(series) / len(series)
            variance = sum((x - mean) ** 2 for x in series) / len(series)
            sd = math.sqrt(variance)
            for structure, value in zip(members, series):
                residuals[structure][name] = 0.0 if sd == 0 else (value - mean) / sd
    return residuals


STRATEGIC = ('minors_white', 'minors_black', 'castled_white', 'castled_black',
             'centre_left_white', 'centre_left_black')


def maturity_index(residuals, structure, components=STRATEGIC):
    """Depth-adjusted residual: "ahead of schedule for its ply", not "developed"."""
    values = [residuals.get(structure, {}).get(name, 0.0) for name in components]
    return sum(values) / len(values) if values else 0.0


def development_level(values, components=STRATEGIC):
    """Absolute development: what the board actually looks like, 0..1.

    This is the component that tells a corridor from a template. The residual
    above deliberately does not, because at low ply *nobody* is developed, so any
    development at all produces a large z-score — the queen-out corridor scored
    +0.83 on the residual while standing at 0.21/0.04 minors and no castling.
    """
    parts = [values.get(name, 0.0) for name in components]
    return sum(parts) / len(parts) if parts else 0.0


def write_maturity(db, structures, residuals, source='local2200'):
    written = 0
    for structure, values in structures.items():
        db.execute('''INSERT OR REPLACE INTO structure_maturity(
                        structure_id, source, boards, entry_mass, ply_mean, minors_white,
                        minors_black, castled_white, castled_black, centre_left_white,
                        centre_left_black, centre_files_white, centre_files_black, dwell_mean,
                        retention_3, reconvergence, effective_successors, source_families,
                        incoming_mass, residual_json, maturity_index, development_level)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (structure, source, values.get('boards'), values.get('entry_mass', 0.0),
                    values.get('ply_mean', 0.0), values.get('minors_white', 0.0),
                    values.get('minors_black', 0.0), values.get('castled_white', 0.0),
                    values.get('castled_black', 0.0), values.get('centre_left_white', 0.0),
                    values.get('centre_left_black', 0.0), values.get('centre_files_white', 0.0),
                    values.get('centre_files_black', 0.0), values.get('dwell_mean', 0.0),
                    values.get('retention_3', 0.0), values.get('reconvergence', 0.0),
                    values.get('effective_successors', 0.0), values.get('source_families', 0),
                    values.get('incoming_mass', 0.0), json.dumps(residuals.get(structure, {})),
                    maturity_index(residuals, structure), development_level(values)))
        written += 1
    db.commit()
    return written
