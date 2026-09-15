"""Structural-family analysis: what a single mental board would have to contain.

Scope: one pawn-structure family (all positions sharing an exact pawn placement),
weighted by *family-conditioned game flow*, not raw database counts.

Weighting rules (the part that must not be got wrong):

  enter_mass(P)  probability that a family game first enters this family at P
                 (persisted in position_flow; sums to the family's entering mass).
                 Used for "what the family looks like when it forms" and for the
                 game-level dwell/retention simulation, so each game counts once.
  reach_flow(P)  probability a family game passes through P at all. Used as the
                 "presence" weighting: the share of family board-visits on which a
                 placement holds, i.e. what the board typically looks like while
                 the family lasts.

Occupancy sums over squares equal the average number of that piece per board, so
a piece that is always present spreads a full 1.0 across its squares.

All outputs are empirical: counts, shares, transitions, retention, compression.
No natural-language chess advice is produced here.
"""
import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import chess

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import attractors, db as studydb  # noqa: E402
from study.metrics import ep_from_score  # noqa: E402
from study.structures import render_pawns  # noqa: E402

PIECES = ('wp', 'wn', 'wb', 'wr', 'wq', 'wk', 'bp', 'bn', 'bb', 'br', 'bq', 'bk')
PIECE_NAMES = {'wp': 'White pawn', 'wn': 'White knight', 'wb': 'White bishop',
               'wr': 'White rook', 'wq': 'White queen', 'wk': 'White king',
               'bp': 'Black pawn', 'bn': 'Black knight', 'bb': 'Black bishop',
               'br': 'Black rook', 'bq': 'Black queen', 'bk': 'Black king'}
SQUARE_NAME = [chess.square_name(s) for s in range(64)]
HOME_SQUARES = {
    'wn': {'b1', 'g1'}, 'wb': {'c1', 'f1'}, 'wr': {'a1', 'h1'}, 'wq': {'d1'}, 'wk': {'e1'},
    'bn': {'b8', 'g8'}, 'bb': {'c8', 'f8'}, 'br': {'a8', 'h8'}, 'bq': {'d8'}, 'bk': {'e8'},
}


# --------------------------------------------------------------------- loading
def load_family(db, structure_id, source='local2200'):
    positions = {}
    for row in db.execute('''SELECT p.position_key, p.fen, p.ply, p.role, p.pieces_json,
                                    p.dev_status, p.dev_white, p.dev_black, p.white_castled,
                                    p.black_castled, p.castling_rights,
                                    f.reach_flow, f.enter_mass, f.path_count, f.n_parents_total,
                                    f.count_ratio, f.flags
                             FROM position p
                             LEFT JOIN position_flow f ON f.position_key = p.position_key
                                  AND f.source = ?
                             WHERE p.structure_id = ?''', (source, structure_id)):
        pieces = {name: int(json.loads(row['pieces_json'])[name]) for name in PIECES}
        positions[row['position_key']] = {
            'fen': row['fen'], 'ply': row['ply'], 'role': row['role'], 'pieces': pieces,
            'dev_status': row['dev_status'], 'dev_white': row['dev_white'],
            'dev_black': row['dev_black'], 'white_castled': row['white_castled'],
            'black_castled': row['black_castled'],
            'reach': row['reach_flow'] or 0.0, 'enter': row['enter_mass'] or 0.0,
            'paths': row['path_count'] or 0.0, 'parents': row['n_parents_total'] or 0,
            'count_ratio': row['count_ratio'], 'flags': row['flags'] or '',
        }
    moves = defaultdict(list)
    for row in db.execute('SELECT parent_key, child_key, uci, games FROM provenance WHERE source=?',
                          (source,)):
        if row['parent_key'] in positions or row['child_key'] in positions:
            moves[row['parent_key']].append((row['child_key'], row['uci'], row['games']))
    sans = {}
    for row in db.execute('SELECT position_key, uci, san FROM move_source WHERE source=?', (source,)):
        sans[(row['position_key'], row['uci'])] = row['san']
    structure = db.execute('SELECT * FROM structure WHERE structure_id=?', (structure_id,)).fetchone()
    return {'structure_id': structure_id, 'structure': dict(structure) if structure else None,
            'positions': positions, 'moves': dict(moves), 'sans': sans, 'source': source,
            'reach_sum': sum(m['reach'] for m in positions.values())}


def weights(family, mode='enter_mass'):
    key = 'enter' if mode == 'enter_mass' else 'reach'
    return {k: meta[key] for k, meta in family['positions'].items()}


# ------------------------------------------------------------------ occupancy
def piece_occupancy(family, mode='enter_mass'):
    w = weights(family, mode)
    out = {name: defaultdict(float) for name in PIECES}
    for key, meta in family['positions'].items():
        weight = w.get(key, 0.0)
        if weight <= 0:
            continue
        for name in PIECES:
            board = meta['pieces'][name]
            while board:
                square = (board & -board).bit_length() - 1
                out[name][square] += weight
                board &= board - 1
    return out, w


def occupancy_table(bits):
    total = sum(bits.values())
    if not total:
        return []
    return sorted(((SQUARE_NAME[sq], value / total, value) for sq, value in bits.items()),
                  key=lambda t: -t[1])


def flexible_slots(family, mode='reach_flow', top_consensus=0.85, minor=0.10):
    """Placements that are genuinely split: the template's open slots.

    Two cases are excluded because they are not flexibility:
      * pawns, whose squares are the family definition itself (see section 3);
      * a piece that merely has two copies on its two home squares (both rooks on
        a1/h1, both bishops on c1/f1), which is multiplicity, not choice. A slot is
        reported only if at least one of its top squares is not a home square.
    """
    occupancy, _w = piece_occupancy(family, mode)
    slots = []
    for name in PIECES:
        if name.endswith('p'):
            continue
        table = occupancy_table(occupancy[name])
        if not table or table[0][1] >= top_consensus:
            continue
        spread = [(sq, p) for sq, p, _m in table if p >= minor]
        home = HOME_SQUARES.get(name, set())
        if len(spread) >= 2 and any(sq not in home for sq, _p in spread[:2]):
            slots.append({'piece': name, 'squares': spread, 'dominant': table[0][1]})
    slots.sort(key=lambda s: -s['dominant'])
    return slots


# ------------------------------------------------------- dwell / retention
def retention(family, max_steps=20):
    """Game-level dwell inside the family (shared definition in study.attractors)."""
    return attractors.exit_time_stats(set(family['positions']), family['moves'],
                                      weights(family, 'enter_mass'), max_steps=max_steps)


def exit_moves(family, top=10):
    w = weights(family, 'enter_mass')
    out = defaultdict(float)
    for key, meta in family['positions'].items():
        mass = w.get(key, 0.0)
        if mass <= 0:
            continue
        edges = family['moves'].get(key, [])
        total = sum(g for _c, _u, g in edges)
        if not total:
            continue
        for child, uci, games in edges:
            if child in family['positions']:
                continue
            out[family['sans'].get((key, uci), uci)] += mass * (games / total)
    total_mass = sum(out.values()) or 1.0
    rows = sorted(((san, mass, mass / total_mass) for san, mass in out.items()), key=lambda t: -t[1])
    return rows[:top], total_mass


def exit_destinations(db, family, source, top=6):
    """Structures that the family's leaving moves lead into, with their own mass."""
    w = weights(family, 'enter_mass')
    dest = defaultdict(float)
    for key in family['positions']:
        mass = w.get(key, 0.0)
        if mass <= 0:
            continue
        edges = family['moves'].get(key, [])
        total = sum(g for _c, _u, g in edges)
        if not total:
            continue
        for child, _uci, games in edges:
            if child in family['positions']:
                continue
            row = db.execute('SELECT structure_id FROM position WHERE position_key=?',
                             (child,)).fetchone()
            if row:
                dest[row['structure_id']] += mass * (games / total)
    rows = []
    for structure_id, mass in sorted(dest.items(), key=lambda kv: -kv[1])[:top]:
        stats = db.execute('''SELECT n_positions, coverage_lower, persistence_mean_plies,
                                     attractor_score FROM structure_stat
                              WHERE structure_id=? AND source=? AND run_id=6''',
                           (structure_id, source)).fetchone()
        rows.append({'structure_id': structure_id, 'mass': mass, 'stats': dict(stats) if stats else None})
    return rows


# --------------------------------------------------------------- move orders
def viterbi_lines(db, source, entry_key, keys_of_interest):
    import math
    moves = defaultdict(list)
    for row in db.execute('SELECT parent_key, child_key, uci, games FROM provenance WHERE source=?',
                          (source,)):
        moves[row['parent_key']].append((row['child_key'], row['uci'], row['games']))
    ply = {row['position_key']: row['ply']
           for row in db.execute('SELECT position_key, ply FROM position')}
    best = {entry_key: (0.0, None, None)}
    by_ply = defaultdict(list)
    for key, value in ply.items():
        by_ply[value].append(key)
    for level in sorted(by_ply):
        for parent in by_ply[level]:
            if parent not in best:
                continue
            edges = moves.get(parent, [])
            total = sum(g for _c, _u, g in edges)
            if not total:
                continue
            base = best[parent][0]
            for child, uci, games in edges:
                if child not in ply or ply[child] <= level:
                    continue
                score = base + math.log(games / total)
                if child not in best or score > best[child][0]:
                    best[child] = (score, parent, uci)
    lines = {}
    for key in keys_of_interest:
        seq, node = [], key
        while node in best and best[node][1] is not None:
            _score, parent, uci = best[node]
            seq.append(uci)
            node = parent
        lines[key] = list(reversed(seq))
    return lines


def entry_start_board():
    from study.domain import ENTRY_MOVES
    board = chess.Board()
    for uci in ENTRY_MOVES:
        board.push_uci(uci)
    return board


def san_line(uci_moves, start=None):
    board = (start or entry_start_board()).copy(stack=False)
    out = []
    for uci in uci_moves:
        move = chess.Move.from_uci(uci)
        out.append(board.san(move))
        board.push(move)
    return ' '.join(out)


def development_orders(family, lines):
    w = weights(family, 'enter_mass')
    counter, total = Counter(), 0.0
    for key in family['positions']:
        mass = w.get(key, 0.0)
        if mass <= 0:
            continue
        counter[san_line(lines.get(key, []))] += mass
        total += mass
    rows = sorted(((line, mass, mass / total if total else 0.0) for line, mass in counter.items()),
                  key=lambda t: -t[1])
    return rows, total


def precedence(family, lines, move_a, move_b):
    w = weights(family, 'enter_mass')
    before = after = 0.0
    for key in family['positions']:
        mass = w.get(key, 0.0)
        if mass <= 0:
            continue
        seq = lines.get(key, [])
        if move_a in seq and move_b in seq:
            if seq.index(move_a) < seq.index(move_b):
                before += mass
            else:
                after += mass
        elif move_a in seq:
            before += mass
        elif move_b in seq:
            after += mass
    return {'a_before_b': before, 'b_before_a': after}


# -------------------------------------------------------------------- features
def feature_holds(meta, feature):
    if feature['kind'] == 'piece_square':
        return bool(meta['pieces'][feature['piece']]
                    & chess.BB_SQUARES[chess.parse_square(feature['squares'][0])])
    if feature['kind'] == 'piece_square_union':
        return any(meta['pieces'][feature['piece']] & chess.BB_SQUARES[chess.parse_square(sq)]
                   for sq in feature['squares'])
    return meta[feature['piece']] == feature['squares'][0]


def candidate_features(family, mode='reach_flow', min_consensus=0.35, top_consensus=0.85):
    occupancy, w = piece_occupancy(family, mode)
    total = sum(w.values()) or 1.0
    features = []
    for name in PIECES:
        table = occupancy_table(occupancy[name])
        for square, probability, mass in table:
            if probability >= min_consensus:
                features.append({'kind': 'piece_square', 'piece': name, 'squares': [square],
                                 'consensus': probability, 'mass': mass, 'total_mass': total,
                                 'weighting': mode})
        if len(table) >= 2 and table[0][1] < top_consensus:
            pair = table[:2]
            combined = pair[0][1] + pair[1][1]
            if combined >= min_consensus and min(pair[0][1], pair[1][1]) >= 0.10:
                features.append({'kind': 'piece_square_union', 'piece': name,
                                 'squares': [pair[0][0], pair[1][0]], 'consensus': combined,
                                 'mass': pair[0][2] + pair[1][2], 'total_mass': total,
                                 'weighting': mode})
    for field, mode_key in (('white_castled', 'state'), ('black_castled', 'state')):
        counter = Counter()
        for key, meta in family['positions'].items():
            counter[meta[field]] += w.get(key, 0.0)
        for value, mass in counter.items():
            share = mass / total
            if share >= min_consensus:
                features.append({'kind': 'state', 'piece': field, 'squares': [value],
                                 'consensus': share, 'mass': mass, 'total_mass': total,
                                 'weighting': mode})
    return features


def feature_label(feature):
    if feature['kind'].startswith('piece_square'):
        return f'{PIECE_NAMES[feature["piece"]]} on ' + '/'.join(feature['squares'])
    return f'{feature["piece"]}: {feature["squares"][0]}'


def feature_stats(family, feature, top=5, boards='all'):
    """Boards where a feature holds, plus its share of family board-visits.

    boards='weighted' counts only boards that carry mass under the feature's own
    weighting (the family's entry boards, for formation features); boards='all'
    counts every board of the family (for lifetime features).
    """
    base = {k: meta['reach'] for k, meta in family['positions'].items()}
    total = sum(base.values()) or 1.0
    floor = 0.0 if boards == 'all' else 1e-12
    mine = {k: meta['enter'] for k, meta in family['positions'].items()}
    holds_boards = holds_mass = 0
    failing = []
    for key, meta in family['positions'].items():
        if boards == 'weighted' and mine.get(key, 0.0) <= floor:
            continue
        if feature_holds(meta, feature):
            holds_boards += 1
            holds_mass += base[key]
        else:
            failing.append((key, base[key], meta))
    failing.sort(key=lambda item: -item[1])
    return {'holds_boards': holds_boards, 'holds_share': holds_mass / total,
            'fails_boards': len(failing), 'fails_share': sum(b for _k, b, _m in failing) / total,
            'top_failing': failing[:top]}


# --------------------------------------------------------- cached-eval pilot
def cached_pilot(db, family, predicate, label, min_moves=2):
    """Counterfactual effect computed from *already cached* multi-PV evaluations.

    For every board matching `predicate` with a cached evaluation, EP is read for
    each move from the same search (same depth, engine and POV), so the comparison
    is like-for-like. The population's own distribution over those moves weights
    the loss. This is a feasibility demonstration, not the scaled experiment.
    """
    rows = []
    for key, meta in family['positions'].items():
        if not predicate(meta):
            continue
        eval_row = db.execute('''SELECT source, depth, pvs_json FROM eval WHERE fen=?
                                 ORDER BY depth DESC LIMIT 1''', (meta['fen'],)).fetchone()
        if not eval_row:
            continue
        pvs = json.loads(eval_row['pvs_json'])
        ep = {}
        for pv in pvs:
            value = ep_from_score(cp=pv.get('cp'), mate=pv.get('mate'), wdl=pv.get('wdl'))
            if value is not None:
                ep[pv['uci']] = value
        if len(ep) < min_moves:
            continue
        shares = {row['uci']: row['share'] for row in db.execute(
            'SELECT uci, share FROM move_source WHERE position_key=? AND source=?',
            (key, family['source']))}
        best = max(ep.values())
        known = {uci: share for uci, share in shares.items() if uci in ep}
        weighted_loss = (sum(share * (best - ep[uci]) for uci, share in known.items())
                         / sum(known.values())) if known else None
        rows.append({'fen': meta['fen'], 'ply': meta['ply'], 'role': meta['role'],
                     'depth': eval_row['depth'], 'source': eval_row['source'],
                     'moves': {uci: round(value, 4) for uci, value in sorted(ep.items(), key=lambda kv: -kv[1])},
                     'population_moves': {uci: round(share, 3) for uci, share in sorted(known.items(), key=lambda kv: -kv[1])},
                     'population_ep_loss': weighted_loss,
                     'population_coverage': (sum(known.values()) / sum(shares.values())) if shares else None})
    losses = [r['population_ep_loss'] for r in rows if r['population_ep_loss'] is not None]
    summary = {'label': label, 'boards': len(rows),
               'mean_population_ep_loss': (sum(losses) / len(losses)) if losses else None,
               'min_loss': min(losses) if losses else None, 'max_loss': max(losses) if losses else None}
    return summary, rows


# ------------------------------------------------------------------- reporting
def render(db, structure_id, source='local2200', outdir=None, run_id=6):
    family = load_family(db, structure_id, source)
    structure = family['structure']
    positions = family['positions']
    entry_occ, _w = piece_occupancy(family, 'enter_mass')
    presence_occ, _w2 = piece_occupancy(family, 'reach_flow')
    lines = viterbi_lines(db, source, db.execute(
        'SELECT position_key FROM position WHERE is_entry=1').fetchone()[0], list(positions))
    orders, order_total = development_orders(family, lines)
    exits, exit_total = exit_moves(family)
    dwell = retention(family)
    dests = exit_destinations(db, family, source)
    formation_features = candidate_features(family, mode='enter_mass')
    lifetime_features = candidate_features(family, mode='reach_flow')
    slots = flexible_slots(family)
    comparison = compare_sources(db, family, expert=source)

    def queen_on_d5_black_to_move(meta):
        return meta['role'] == 'black_to_move' and bool(
            meta['pieces']['bq'] & chess.BB_SQUARES[chess.D5])

    def white_to_move_in_family(meta):
        return meta['role'] == 'white_to_move'

    pilot_one, pilot_rows_one = cached_pilot(db, family, queen_on_d5_black_to_move,
                                             'Black to move with the queen on d5')
    pilot_two, pilot_rows_two = cached_pilot(db, family, white_to_move_in_family,
                                             'White to move anywhere in the family')

    out = []
    add = out.append
    add(f'# Structural family report — `{structure_id}` ({structure["material"]})\n')
    add('Empirical evidence only: occupancy, transition, dwell, compression. Weights are '
        'family-conditioned: **enter_mass** ("when the family forms") and **reach_flow** '
        '("family board-visits", i.e. what the board typically looks like while the family '
        'lasts). No chess advice is generated.\n')

    add('## 0. Where this output sits\n')
    add('The curriculum is deliberately kept as three separate outputs and never merged into a '
        'single ranking:\n')
    add('* **class 1 — decision knowledge**: the study-priority table in '
        '`analysis/study-report-run*.md`, backed by `metric` rows (per-position regret, gap, '
        'reach, coverage; components always stored separately).')
    add('* **class 2 — deviation knowledge**: the opponent-deviation table in the same report, '
        'backed by `deviation` rows (probability of deviation, objective concession, class, '
        'engine best response).')
    add('* **class 3 — structural / template knowledge**: this report, backed by `structure_stat` '
        'and `position_flow` rows (coverage, dwell, convergence, persistence, features).\n')

    add('## 1. The family in data terms\n')
    add(f'* boards **{len(positions)}**, plies {min(m["ply"] for m in positions.values())}–'
        f'{max(m["ply"] for m in positions.values())}, white-to-move '
        f'{sum(1 for m in positions.values() if m["role"] == "white_to_move")}')
    add(f'* entering mass **{dwell["entry_mass"]:.4f}** of the 1.e4 d5 population; '
        f'sum of board reaches {family["reach_sum"]:.4f}')
    add(f'* pawns: islands {structure["islands_white"]}/{structure["islands_black"]}, open files '
        f'{structure["open_files"]}, semi-open {structure["semi_open_white"]}/'
        f'{structure["semi_open_black"]}, passed pawns {structure["passed_white"]}/'
        f'{structure["passed_black"]}')
    add('')
    add('<pre>' + render_pawns(int(structure['white_pawns'], 16), int(structure['black_pawns'], 16))
        + '</pre>\n')

    add('## 2. Occupancy: formation versus presence\n')
    add('Formation = share of entering mass; presence = share of family board-visits. Squares '
        'below 1% are omitted.\n')
    add('| piece | formation | presence |')
    add('|---|---|---|')
    for name in PIECES:
        f_table = {sq: p for sq, p, _m in occupancy_table(entry_occ[name]) if p >= 0.01}
        p_table = {sq: p for sq, p, _m in occupancy_table(presence_occ[name]) if p >= 0.01}
        f_text = ', '.join(f'{sq} {p:.0%}' for sq, p in f_table.items()) or '—'
        p_text = ', '.join(f'{sq} {p:.0%}' for sq, p in p_table.items()) or '—'
        add(f'| {PIECE_NAMES[name]} | {f_text} | {p_text} |')

    add('\n## 3. Pawns inside the family, and what ends it\n')
    placements = {(m['pieces']['wp'], m['pieces']['bp']) for m in positions.values()}
    add(f'* distinct pawn placements across {len(positions)} boards: **{len(placements)}** — '
        f'the family is defined by its exact pawns, so pawn squares are fixed and carry no '
        f'variance; all variation inside the family is piece placement and move order')
    add(f'* mass that leaves the family on the next move: {exit_total:.4f}\n')
    add('| leaving move | share of exits | share of entering mass |')
    add('|---|---|---|')
    for san, mass, share in exits:
        add(f'| {san} | {share:.1%} | {mass:.4f} |')
    add('\nWhere that mass goes (destination structures, with their own statistics):\n')
    add('| destination structure | mass | boards | peak coverage | mean dwell | attractor score |')
    add('|---|---|---|---|---|---|')
    for row in dests:
        stats = row['stats'] or {}
        mean_dwell = stats.get('persistence_mean_plies')
        add(f"| `{row['structure_id']}` | {row['mass']:.4f} | {stats.get('n_positions', '—')} | "
            f"{(stats.get('coverage_lower') or 0):.3f} | "
            f"{(f'{mean_dwell:.2f}' if mean_dwell else '—')} | "
            f"{(stats.get('attractor_score') or 0):.2f} |")

    add('\n## 4. Move orders\n')
    add(f'* distinct most-likely orders (weighted): **{len(orders)}**, covering entry mass '
        f'{order_total:.4f}\n')
    add('| move order | mass | share |')
    add('|---|---|---|')
    for line, mass, share in orders[:12]:
        add(f'| `{line}` | {mass:.4f} | {share:.1%} |')
    add('\nPrecedence over the same paths (entry-mass weighted):\n')
    add('| move A | move B | A before B | B before A |')
    add('|---|---|---|---|')
    for a, b in (('b1c3', 'g1f3'), ('g1f3', 'f1c4'), ('b1c3', 'd2d4'), ('g8f6', 'b8c6')):
        stats = precedence(family, lines, a, b)
        add(f'| {a} | {b} | {stats["a_before_b"]:.4f} | {stats["b_before_a"]:.4f} |')

    add('\n## 5. Expert versus Lichess on the boards the cache covers\n')
    add(f'* comparable boards: {len(comparison)} of {len(positions)}; coverage is the API cache, '
        f'not the full Lichess corpus, so this section is evidence-of-difference, not a full '
        f'survey')
    if comparison:
        add('')
        add('| ply | board | entry mass | expert n | lichess n | total variation | expert top | '
            'Lichess top |')
        add('|---|---|---|---|---|---|---|---|')
        for row in comparison[:10]:
            add(f"| {row['ply']} | `{positions[row['position_key']]['fen']}` | "
                f"{row['enter_mass']:.4f} | {row['expert_games']} | {row['other_games']:,} | "
                f"{row['total_variation']:.3f} | {row['expert_top']} | {row['other_top']} |")

    add('\n## 6. Template features\n')
    add('### 6a. Formation features (entry-mass weighted)\n')
    entry_boards = sum(1 for meta in positions.values() if meta['enter'] > 0)
    add(f'All entering mass sits on {entry_boards} boards, so these are the facts true at the '
        f'moment the structure appears.\n')
    add('| feature | consensus (entering mass) | entry boards where it holds / fails |')
    add('|---|---|---|')
    for feature in sorted(formation_features, key=lambda f: -f['consensus'])[:12]:
        stats = feature_stats(family, feature, top=0, boards='weighted')
        add(f'| {feature_label(feature)} | {feature["consensus"]:.1%} | '
            f'{stats["holds_boards"]} / {stats["fails_boards"]} |')
    add('\n### 6b. Lifetime features (presence weighted over family board-visits)\n')
    invariants = [f for f in lifetime_features if f['consensus'] >= 0.995]
    varying = [f for f in lifetime_features if f['consensus'] < 0.995]
    add('**Invariants (>=99.5% of board-visits):** '
        + '; '.join(f'{feature_label(f)} ({f["consensus"]:.1%})'
                    for f in sorted(invariants, key=lambda f: -f['consensus'])) + '\n')
    add('**Features (35-99.5% of board-visits):**\n')
    add('| feature | share of board-visits | boards where it holds / fails | share where it fails |')
    add('|---|---|---|---|')
    for feature in sorted(varying, key=lambda f: -f['consensus'])[:16]:
        stats = feature_stats(family, feature, top=0)
        add(f'| {feature_label(feature)} | {feature["consensus"]:.1%} | '
            f'{stats["holds_boards"]} / {stats["fails_boards"]} | {stats["fails_share"]:.2%} |')
    add('\n### 6c. Flexible slots (placements that are genuinely split)\n')
    add('| piece | dominant square | split across |')
    add('|---|---|---|')
    for slot in slots[:10]:
        split = ', '.join(f'{sq} {p:.0%}' for sq, p in slot['squares'][:5])
        add(f'| {PIECE_NAMES[slot["piece"]]} | {slot["squares"][0][0]} {slot["dominant"]:.0%} | '
            f'{split} |')

    add('\n## 7. Exceptions for the strongest features\n')
    interesting = [f for f in lifetime_features
                   if feature_stats(family, f, top=0)['fails_boards'] > 0]
    for feature in sorted(interesting,
                          key=lambda f: -feature_stats(family, f, top=0)['fails_share'])[:5]:
        stats = feature_stats(family, feature, top=3)
        add(f'* **{feature_label(feature)}** — holds on {stats["holds_boards"]} boards '
            f'({feature["consensus"]:.1%} of board-visits); fails on {stats["fails_boards"]} '
            f'boards ({stats["fails_share"]:.2%} of board-visits). Largest counter-examples:')
        for key, _share, meta in stats['top_failing']:
            add(f'  * `{meta["fen"]}` (ply {meta["ply"]}, board-visit share {_share:.4f})')

    add('\n## 8. Transit states versus strategic attractors\n')
    add('Definition now shared with `structure_stat` (one estimator, game level): mass entering a '
        'structure is pushed along its own move shares; a move that changes the pawn structure '
        'exits. `exit@1ply` is the exiting share on the next move, `retention(3)` the share still '
        'inside after three plies. Convergence = how many distinct incoming edges and parent '
        'boards arrive.\n')
    add('| structure | boards | entry mass | mean dwell | median dwell | exit@1ply | exit@2ply | '
        'retention(3) | incoming edges | distinct parents | class |')
    add('|---|---|---|---|---|---|---|---|---|---|---|')
    top_structures = [row['structure_id'] for row in db.execute(
        '''SELECT p.structure_id, SUM(f.enter_mass) m FROM position p
           JOIN position_flow f ON f.position_key=p.position_key AND f.source=?
           GROUP BY p.structure_id ORDER BY m DESC LIMIT 8''', (source,))]
    for candidate in top_structures:
        stats = db.execute('''SELECT n_positions, coverage_lower, persistence_mean_plies,
                                     persistence_median_plies, n_edges_in, n_parents,
                                     components_json FROM structure_stat
                              WHERE structure_id=? AND source=? AND run_id=?''',
                           (candidate, source, run_id)).fetchone()
        if not stats:
            continue
        comp = json.loads(stats['components_json'])
        parents_family = comp.get('distinct_parents_family', stats['n_parents'])
        add(f"| `{candidate}` | {stats['n_positions']} | {comp['enter_mass']:.4f} | "
            f"{stats['persistence_mean_plies']:.2f} | {stats['persistence_median_plies']} | "
            f"{(comp.get('exit_share_first_ply') or 0):.1%} | "
            f"{(comp.get('exit_share_within_two') or 0):.1%} | "
            f"{(comp.get('retention_3') or 0):.1%} | {stats['n_edges_in']} | {parents_family} | "
            f"{classify(stats['persistence_mean_plies'], stats['persistence_median_plies'], comp, stats['n_edges_in'])} |")
    add('\nRule (thresholds are configuration, not truth): a structure is a **transit state** if '
        'at least half of its entering mass leaves on the next move; it is a **strategic '
        'attractor** if it retains ≥15% after three plies, has ≥5 distinct incoming edges and '
        'mean dwell ≥2 plies; otherwise intermediate.\n')

    add('## 9. Compression accounting\n')
    domain_positions = db.execute('SELECT COUNT(*) FROM position').fetchone()[0]
    add(f'* domain: {domain_positions:,} exact positions; this family: {len(positions)} boards '
        f'({len(positions) / domain_positions:.2%} of the domain) carrying '
        f'{dwell["entry_mass"]:.1%} of family games')
    add(f'* distinct most-likely move orders inside the family: **{len(orders)}** '
        f'({len(positions) / max(len(orders), 1):.0f} boards per order)')
    add(f'* pawns: 1 fixed placement; piece placement: {len(lifetime_features)} candidate features; '
        f'flexible slots: {len(slots)}')
    varying_features = [f for f in lifetime_features if f['consensus'] < 0.995]
    for feature in sorted(varying_features, key=lambda f: -f['consensus'])[:6]:
        stats = feature_stats(family, feature, top=0)
        add(f'  * {feature_label(feature)} — {stats["holds_boards"]} boards, '
            f'{feature["consensus"]:.1%} of board-visits')
    add(f'* mean plies a game spends inside the family: {dwell["mean_dwell"]:.2f} '
        f'(median {dwell["median_dwell"]}); board-visits per game on average: '
        f'{family["reach_sum"] / dwell["entry_mass"]:.2f}')

    add('\n## 10. Counterfactual experiments\n')
    add('### 10a. Feasibility check from cached evaluations (no new engine compute)\n')
    for pilot in (pilot_one, pilot_two):
        if not pilot['boards']:
            add(f"* {pilot['label']}: no cached evaluations available yet")
            continue
        material = sum(1 for r in (pilot_rows_one if pilot is pilot_one else pilot_rows_two)
                       if r['population_ep_loss'] is not None and r['population_ep_loss'] > 0.02)
        add(f"* **{pilot['label']}** — boards with cached multi-PV: {pilot['boards']} "
            f"({material} of them above 0.02 EP population loss); "
            f"population-weighted expected loss across those boards: "
            f"{pilot['mean_population_ep_loss']:.4f} EP (range "
            f"{pilot['min_loss']:.4f}–{pilot['max_loss']:.4f}); these are evaluations already in "
            f"the cache from the study run, so this costs no engine time")
    shown = 0
    for row in pilot_rows_one:
        if shown >= 4:
            break
        coverage_text = (f"{row['population_coverage']:.0%}"
                         if row['population_coverage'] is not None else "n/a")
        loss_text = (f"{row['population_ep_loss']:.4f}"
                     if row['population_ep_loss'] is not None else "n/a")
        add(f"  * `{row['fen']}` — PVs {row['moves']}; population {row['population_moves']} "
            f"(covers {coverage_text} of its moves, d{row['depth']}); population EP loss "
            f"{loss_text}")
        shown += 1

    add('\n### 10b. Protocol for the scaled version (designed, not executed)\n')
    add('1. Define the feature predicate over *related* pawn-structure families, not just this '
        'one; a feature that holds in one structure is not yet a reusable template.')
    add('2. For each parent board where the feature-establishing move `m_F` and at least one '
        'population alternative `m_A` are both available, run one multi-PV search and read both '
        'values from the same search (same depth, engine build, POV).')
    add('3. Effect = EP(m_F) − EP(m_A), aggregated with mass weights and a bootstrap interval, '
        'restricted to parents where both moves have ≥30 expert games.')
    add('4. Prescriptive only if the interval excludes 0 in more than one pawn-structure family; '
        'otherwise the feature stays descriptive.')
    add('5. Cost: ~30 parents × one depth-18 multi-PV search ≈ 90 s single-threaded; deepening '
        'the top 5 to depth 24 ≈ +60 s. No cloud requests.\n')

    add('## 11. Method notes and limits\n')
    add(f'* source `{source}`; entry denominators from the domain build; run {run_id}.')
    add('* occupancy sums equal the average piece count per board, so a permanently present piece '
        'spreads a full 1.0 over its squares.')
    add('* dwell/retention use the game-level simulator shared with `structure_stat`; the mass of '
        'a game is counted once, at family entry.')
    add('* the Lichess comparison is restricted to boards present in the API cache.')
    add('* backward-pawn and castling indicators remain approximate (see STUDY-DESIGN.md).')

    outdir = outdir or (ROOT / 'analysis')
    outdir.mkdir(parents=True, exist_ok=True)
    target = outdir / f'family-{structure_id}.md'
    target.write_text('\n'.join(out) + '\n')
    with (outdir / f'family-{structure_id}-features.csv').open('w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['weighting', 'kind', 'piece', 'squares', 'consensus', 'boards_hold',
                         'boards_fail', 'share_fail'])
        for feature in formation_features + lifetime_features:
            stats = feature_stats(family, feature, top=0)
            writer.writerow([feature['weighting'], feature['kind'], feature['piece'],
                             '/'.join(feature['squares']), round(feature['consensus'], 4),
                             stats['holds_boards'], stats['fails_boards'],
                             round(stats['fails_share'], 4)])
    with (outdir / f'family-{structure_id}-occupancy.csv').open('w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['piece', 'square', 'formation_share', 'presence_share'])
        for name in PIECES:
            formation = {sq: p for sq, p, _m in occupancy_table(entry_occ[name])}
            presence = {sq: p for sq, p, _m in occupancy_table(presence_occ[name])}
            for square in sorted(set(formation) | set(presence)):
                writer.writerow([name, square, round(formation.get(square, 0.0), 6),
                                 round(presence.get(square, 0.0), 6)])
    print(f'report written to {target}')
    return {'structure_id': structure_id, 'boards': len(positions),
            'formation_features': len(formation_features),
            'lifetime_features': len(lifetime_features), 'flexible_slots': len(slots),
            'move_orders': len(orders), 'exits': len(exits),
            'mean_dwell': dwell['mean_dwell'], 'median_dwell': dwell['median_dwell'],
            'entry_mass': dwell['entry_mass'], 'target': str(target)}


def retention_metrics(db, structure_id, source='local2200'):
    family = load_family(db, structure_id, source)
    if not family['positions']:
        return None
    stats = retention(family, max_steps=6)
    return {'entry_mass': stats['entry_mass'], 'mean_dwell': stats['mean_dwell'],
            'exit_first_ply': stats['exit_first_ply'], 'curve': stats['curve']}


def classify(mean_dwell, median_dwell, components, incoming_edges):
    if (components.get('exit_share_first_ply') or 0) >= 0.5:
        return 'transit'
    if ((components.get('retention_3') or 0) >= 0.15 and incoming_edges >= 5
            and (mean_dwell or 0) >= 2.0):
        return 'strategic_attractor'
    return 'intermediate'


def compare_sources(db, family, expert='local2200', other='lichess'):
    """Expert vs ordinary move distributions on the same boards.

    Audit note: `position_source.games` is used ONLY as an availability gate and as
    a display column (the source's published global population of the position). It
    is never treated as transition traffic: the comparison itself uses `move_source`
    shares, which are family-local. A position whose coverage_state is 'absent' or
    'zero' is skipped rather than counted as zero games.
    """
    rows = []
    for key, meta in family['positions'].items():
        coverage = db.execute('SELECT coverage_state, games FROM position_source '
                              'WHERE position_key=? AND source=?', (key, other)).fetchone()
        if not coverage or coverage['coverage_state'] in ('absent', 'zero'):
            continue
        expert_moves = {r['uci']: r for r in db.execute(
            'SELECT uci, san, share, games FROM move_source WHERE position_key=? AND source=?',
            (key, expert))}
        other_moves = {r['uci']: r for r in db.execute(
            'SELECT uci, san, share, games FROM move_source WHERE position_key=? AND source=?',
            (key, other))}
        if len(expert_moves) < 2 or len(other_moves) < 2:
            continue
        diff = sum(abs(expert_moves.get(u, {'share': 0})['share']
                       - other_moves.get(u, {'share': 0})['share'])
                   for u in set(expert_moves) | set(other_moves))
        rows.append({'position_key': key, 'ply': meta['ply'], 'enter_mass': meta['enter'],
                     'expert_games': sum(r['games'] for r in expert_moves.values()),
                     'other_games': coverage['games'],  # published global population
                     'other_population_kind': 'global_published', 'total_variation': diff / 2,
                     'expert_top': max(expert_moves.values(), key=lambda r: r['share'])['san'],
                     'other_top': max(other_moves.values(), key=lambda r: r['share'])['san']})
    rows.sort(key=lambda r: -r['enter_mass'])
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=studydb.DEFAULT_ANALYSIS_DB)
    parser.add_argument('--structure', default='e6808f7826825c0d')
    parser.add_argument('--source', default='local2200')
    parser.add_argument('--run-id', type=int, default=6)
    parser.add_argument('--outdir', type=Path)
    args = parser.parse_args()
    db = studydb.connect(args.db, create=False)
    print(json.dumps(render(db, args.structure, args.source, args.outdir, args.run_id), indent=1))
    db.close()


if __name__ == '__main__':
    main()
