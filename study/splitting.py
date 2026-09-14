"""Template splitting: does a feature change future strong-play behaviour?

Implements section 3 of STUDY-CURRICULUM.md.

Within a mature pawn family S, test whether feature F predicts materially
different future strong-player behaviour Y:

    I(Y; F | S) = H(Y) - sum_g w_g * H(Y | F = g)        [bits]

Only split when the entropy reduction is large, survives a permutation test, and
every group carries real mass. Geometric difference without a behaviour
difference is never a split — the target of every test is behaviour, not the
position vector.

Two behaviour targets are measured separately:

* `near`  — the strong-play move distribution at the family's boards (next move);
* `far`   — the distribution over pawn structures reached within `horizon` plies
            (structural future), so a feature that only changes move order within
            the same structure is not mistaken for a strategic split.
"""
import json
import random
import sys
from collections import Counter, defaultdict
from math import log2
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import db as studydb  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS template_split (
    structure_id  TEXT NOT NULL,
    source        TEXT NOT NULL,
    feature       TEXT NOT NULL,
    target        TEXT NOT NULL,
    mi_bits       REAL NOT NULL,
    p_value       REAL NOT NULL,
    groups        INTEGER NOT NULL,
    min_group_share REAL NOT NULL,
    min_group_boards INTEGER NOT NULL,
    decision      TEXT NOT NULL,
    detail_json   TEXT NOT NULL,
    PRIMARY KEY (structure_id, source, feature, target)
);
"""


def ensure_schema(db):
    db.executescript(SCHEMA)
    db.commit()


# ------------------------------------------------------------------- entropy
def entropy(dist):
    total = sum(dist.values())
    if total <= 0:
        return 0.0
    return -sum((v / total) * log2(v / total) for v in dist.values() if v > 0)


def conditional_mi(samples, feature_fn, target_fn, min_boards=1):
    """samples: [(key, weight, meta)]. Returns (mi, group_report)."""
    pooled = defaultdict(float)
    groups = defaultdict(lambda: defaultdict(float))
    group_mass = defaultdict(float)
    group_boards = Counter()
    for key, weight, meta in samples:
        group = feature_fn(meta)
        if group is None:
            continue
        target = target_fn(key, meta)
        if target is None:
            continue
        # a target is either a discrete label or a distribution over labels
        # (e.g. the structural future), in which case mass is split by its shares
        items = target.items() if isinstance(target, dict) else [(target, 1.0)]
        for label, share in items:
            pooled[label] += weight * share
            groups[group][label] += weight * share
        group_mass[group] += weight
        group_boards[group] += 1
    usable = {g: dist for g, dist in groups.items() if group_boards[g] >= min_boards}
    total = sum(group_mass[g] for g in usable)
    if total <= 0 or len(usable) < 2:
        return 0.0, {'groups': {}, 'reason': 'fewer than two usable groups'}
    base = entropy(pooled)
    conditional = sum((group_mass[g] / total) * entropy(dist) for g, dist in usable.items())
    report = {'base_entropy_bits': base, 'conditional_entropy_bits': conditional,
              'groups': {g: {'share': group_mass[g] / total, 'boards': group_boards[g],
                             'distribution': dict(sorted(dist.items(), key=lambda kv: -kv[1])[:6])}
                         for g, dist in usable.items()}}
    return base - conditional, report


def permutation_p(samples, feature_fn, target_fn, observed, permutations=200, seed=1337,
                  min_boards=1):
    """Relabel the feature across boards, preserving group sizes.

    The null hypothesis is "the feature label carries no information about future
    behaviour": labels are permuted among the family's boards, keeping the number
    of boards per group, and the entropy reduction is recomputed. Target functions
    are looked up by position key (as in the real targets), so the permutation
    changes only the feature assignment.
    """
    rng = random.Random(seed)
    labels = [feature_fn(meta) for _key, _weight, meta in samples]
    keys = [key for key, _weight, _meta in samples]
    if len(set(labels)) < 2:
        return 1.0
    shuffled_labels = list(labels)
    count = 0
    for _ in range(permutations):
        rng.shuffle(shuffled_labels)
        mapping = dict(zip(keys, shuffled_labels))
        fake = [(key, weight, {**meta, '_label': mapping[key]})
                for key, weight, meta in samples]
        value, _report = conditional_mi(fake, lambda meta: meta.get('_label'), target_fn,
                                        min_boards=min_boards)
        if value >= observed:
            count += 1
    return (count + 1) / (permutations + 1)


# ------------------------------------------------------------------- features
def _square_of(pieces, name):
    bits = pieces.get(name, 0)
    if not bits or bits & (bits - 1):
        return None
    return bits.bit_length() - 1


def feature_functions(pieces_of):
    """Interpretable features; each returns a group label or None."""

    def development_total(meta):
        total = (meta.get('dev_white') or 0) + (meta.get('dev_black') or 0)
        return None if not meta else min(4, total // 2)

    def imbalance(meta):
        if not meta:
            return None
        return max(-2, min(2, (meta.get('dev_white') or 0) - (meta.get('dev_black') or 0)))

    def open_file_occupancy(colour):
        def inner(meta):
            key = meta.get('_key')
            pieces = pieces_of(key)
            if pieces is None:
                return None
            open_files = meta.get('_open_files')
            if open_files is None:
                return None
            rook = pieces.get('wr' if colour == 'w' else 'br', 0)
            present = any(rook & (1 << (8 * rank + file)) for file in open_files
                          for rank in range(8))
            return f'{colour}_rook_on_open_file={int(present)}'
        return inner

    def bishop_count(colour):
        def inner(meta):
            key = meta.get('_key')
            pieces = pieces_of(key)
            if pieces is None:
                return None
            home = {'w': (2, 5), 'b': (58, 61)}[colour]
            bits = pieces.get('wb' if colour == 'w' else 'bb', 0)
            off_home = sum(1 for square in range(64) if bits & (1 << square) and square not in home)
            return f'{colour}_bishops_developed={min(2, off_home)}'
        return inner

    def knight_count(colour):
        def inner(meta):
            key = meta.get('_key')
            pieces = pieces_of(key)
            if pieces is None:
                return None
            home = {'w': (1, 6), 'b': (57, 62)}[colour]
            bits = pieces.get('wn' if colour == 'w' else 'bn', 0)
            off_home = sum(1 for square in range(64) if bits & (1 << square) and square not in home)
            return f'{colour}_knights_developed={min(2, off_home)}'
        return inner

    def queen_bucket(colour):
        def inner(meta):
            key = meta.get('_key')
            pieces = pieces_of(key)
            if pieces is None:
                return None
            square = _square_of(pieces, 'wq' if colour == 'w' else 'bq')
            if square is None:
                return f'{colour}_queen=absent_or_multiple'
            return f'{colour}_queen={_square_name(square)}'
        return inner

    return {
        'castling_white': lambda meta: meta.get('white_castled'),
        'castling_black': lambda meta: meta.get('black_castled'),
        'queen_white': queen_bucket('w'),
        'queen_black': queen_bucket('b'),
        'bishops_white': bishop_count('w'),
        'bishops_black': bishop_count('b'),
        'knights_white': knight_count('w'),
        'knights_black': knight_count('b'),
        'development_total': development_total,
        'development_imbalance': imbalance,
        'open_file_rook_white': open_file_occupancy('w'),
        'open_file_rook_black': open_file_occupancy('b'),
    }


def _square_name(square):
    return chr(ord('a') + square % 8) + str(square // 8 + 1)


# --------------------------------------------------------------------- driver
def family_samples(db, structure_id, source='local2200', positions=None, moves=None):
    """[(position_key, weight, meta)] for a family, weight = flow reach."""
    from study import structure_flow
    if positions is None or moves is None:
        positions, moves = structure_flow.load_graph_inputs(db)
    reach = {row[0]: row[1] or 0.0 for row in db.execute(
        'SELECT position_key, reach_flow FROM position_flow WHERE source=?', (source,))}
    samples = []
    for row in db.execute('''SELECT position_key, pieces_json, castling_rights, dev_white,
                                    dev_black, dev_status, white_castled, black_castled, fen, role
                             FROM position WHERE structure_id=?''', (structure_id,)):
        key = row['position_key']
        meta = dict(row)
        meta['_key'] = key
        meta['_pieces'] = json.loads(row['pieces_json'])
        samples.append((key, reach.get(key, 0.0) or 1e-9, meta))
    return samples, positions, moves


def target_near(db, source='local2200'):
    cache = {}

    def inner(key, _meta):
        if key not in cache:
            cache[key] = {row['uci']: row['share'] for row in db.execute(
                'SELECT uci, share FROM move_source WHERE position_key=? AND source=?',
                (key, source))}
        return cache[key] or None
    return inner


def make_target_far(positions, moves, horizon=3):
    """Distribution over pawn structures reached within `horizon` plies."""
    cache = {}

    def propagate(key):
        mass = defaultdict(float)
        frontier = {key: 1.0}
        for _step in range(horizon):
            nxt = defaultdict(float)
            for node, weight in frontier.items():
                outgoing = moves.get(node, [])
                total = sum(games for _c, _u, games in outgoing)
                if not total:
                    continue
                for child, _uci, games in outgoing:
                    if child in positions:
                        share = weight * games / total
                        if positions[child]['structure'] != positions[key]['structure']:
                            mass[positions[child]['structure']] += share
                        else:
                            nxt[child] += share
            frontier = nxt
            if not frontier:
                break
        if not mass:
            mass[positions[key]['structure']] = 1.0
        return dict(mass)

    def inner(key, _meta):
        if key not in cache:
            cache[key] = propagate(key)
        return cache[key]
    return inner


def evaluate_family(db, structure_id, source='local2200', features=None,
                    min_bits=0.10, max_p=0.05, min_group_share=0.05, min_group_boards=5,
                    permutations=200, horizon=3, positions=None, moves=None):
    samples, positions, moves = family_samples(db, structure_id, source, positions, moves)
    pieces_of = lambda key: next((meta['_pieces'] for k, _w, meta in samples if k == key), None)
    total_mass = sum(weight for _k, weight, _m in samples)
    for _k, _w, meta in samples:
        meta['_open_files'] = {file for file in range(8)
                               if not any(meta['_pieces'].get(name, 0) & _file_mask(file)
                                          for name in ('wp', 'bp'))}
    functions = features or feature_functions(pieces_of)
    near = target_near(db, source)
    far = make_target_far(positions, moves, horizon)
    out = []
    for name, function in functions.items():
        for target_name, target in (('near', near), ('far', far)):
            mi, report = conditional_mi(samples, function, target)
            p = permutation_p(samples, function, target, mi, permutations=permutations)
            groups = report.get('groups', {})
            min_share = min((g['share'] for g in groups.values()), default=0.0)
            min_boards = min((g['boards'] for g in groups.values()), default=0)
            decision = ('split' if (mi >= min_bits and p < max_p
                                    and len(groups) >= 2
                                    and min_share >= min_group_share
                                    and min_boards >= min_group_boards)
                        else 'keep')
            out.append({'structure_id': structure_id, 'source': source, 'feature': name,
                        'target': target_name, 'mi_bits': mi, 'p_value': p,
                        'groups': len(groups), 'min_group_share': min_share,
                        'min_group_boards': min_boards, 'decision': decision,
                        'detail': json.dumps(report)})
    return out


def persist(db, rows):
    ensure_schema(db)
    for row in rows:
        db.execute('''INSERT OR REPLACE INTO template_split(
            structure_id, source, feature, target, mi_bits, p_value, groups,
            min_group_share, min_group_boards, decision, detail_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (row['structure_id'], row['source'], row['feature'], row['target'],
             row['mi_bits'], row['p_value'], row['groups'], row['min_group_share'],
             row['min_group_boards'], row['decision'], row['detail']))
    db.commit()
    return db.execute('SELECT COUNT(*) FROM template_split').fetchone()[0]


def _file_mask(file):
    mask = 0
    for rank in range(8):
        mask |= 1 << (8 * rank + file)
    return mask
