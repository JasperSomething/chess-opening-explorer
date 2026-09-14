"""Motif mining — section 4 of STUDY-CURRICULUM.md.

From a family's entering-mass distribution we sample forward trajectories through
the strong-play flow graph (sampling proportional to edge mass, fixed seed), turn
each ply into a canonical atomic transformation event, and mine the recurring
multi-step motifs:

* weighted sets (Apriori, up to size 4, min support 0.05);
* order flexibility: the canonical order is the order respected by the largest
  share of containing trajectories, and the complement is reported as
  ``order_flexibility = 1 - share`` (never discarded);
* family association: ``P(motif | family)`` and the lift against the pooled
  support of every family processed.

Event vocabulary (canonical, hashable, exactly the transformation report's):

    ('pawn_move', from_sq, to_sq)
    ('piece_move', letter, from_sq, to_sq)
    ('capture', subject_letter, victim_letter, from_sq, to_sq)
    ('castle', 'w'|'b', 'k'|'q')
    ('file_open', file_letter, None)
    ('structure_exit', None, None)

Nothing is named or interpreted, and no engine is consulted: this is pure
counting over the flow graph already in the analysis database.
"""
import argparse
import json
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

import chess

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import db as studydb, structure_flow, templates  # noqa: E402

PIECE_LETTER = {chess.PAWN: 'P', chess.KNIGHT: 'N', chess.BISHOP: 'B', chess.ROOK: 'R',
                chess.QUEEN: 'Q', chess.KING: 'K'}


def _fen_reader(db):
    """Cached position_key -> fen reader (the analysis database is read-only here)."""
    fens = {row['position_key']: row['fen'] for row in db.execute('SELECT position_key, fen FROM position')}
    return lambda key: fens[key]


def _open_files(board):
    pawns = board.pieces(chess.PAWN, chess.WHITE) | board.pieces(chess.PAWN, chess.BLACK)
    return {chr(ord('a') + file) for file in range(8) if not (pawns & chess.BB_FILES[file])}


def classify_move(board, uci, child, parent, positions):
    """The atomic events a move produces, in a fixed order (primary first)."""
    move = chess.Move.from_uci(uci)
    events = []
    piece = board.piece_at(move.from_square)
    if piece is not None:
        if board.is_castling(move):
            side = 'k' if chess.square_file(move.to_square) > 4 else 'q'
            events.append(('castle', 'w' if piece.color else 'b', side))
        else:
            letter = PIECE_LETTER[piece.piece_type]
            from_sq = chess.square_name(move.from_square)
            to_sq = chess.square_name(move.to_square)
            if board.is_capture(move):
                victim = board.piece_at(move.to_square)
                victim_letter = PIECE_LETTER[victim.piece_type] if victim else 'P'
                events.append(('capture', letter, victim_letter, from_sq, to_sq))
            elif piece.piece_type == chess.PAWN:
                events.append(('pawn_move', from_sq, to_sq))
            else:
                events.append(('piece_move', letter, from_sq, to_sq))
    after = board.copy(stack=False)
    after.push(move)
    for file in sorted(_open_files(after) - _open_files(board)):
        events.append(('file_open', file, None))
    child_meta = positions.get(child)
    if child_meta is not None and child_meta['structure'] != parent['structure']:
        events.append(('structure_exit', None, None))
    return tuple(events)


def sample_trajectories(db, seeds, positions, moves, n=20000, window_plies=12, seed=1337, fen_of=None):
    """Sample forward trajectories from a family's entering-mass distribution.

    ``seeds`` is {position_key: mass}. At each position the outgoing edge is
    chosen with probability games / sum(games); the walk stops after
    ``window_plies`` plies or at a dead end. Returns (trajectories, diagnostics).
    """
    fen_of = fen_of or _fen_reader(db)
    keys = sorted((k for k in seeds if k in positions), key=lambda k: (str(k),))
    weights = [float(seeds[k]) for k in keys]
    total_mass = sum(weights)
    diag = {'samples': n, 'window_plies': window_plies, 'seed': seed,
            'seeds_mass': float(total_mass), 'unique_events': 0}
    if not keys or total_mass <= 0:
        return [], diag

    rng = random.Random(seed)
    board_cache, event_cache, unique = {}, {}, set()

    def events_for(key, uci, child):
        cached = event_cache.get((key, uci))
        if cached is None:
            board = board_cache.get(key)
            if board is None:
                board = chess.Board(fen_of(key) + ' 0 1')
                board_cache[key] = board
            cached = classify_move(board, uci, child, positions[key], positions)
            event_cache[(key, uci)] = cached
        return cached

    trajectories = []
    for _ in range(n):
        key = rng.choices(keys, weights=weights, k=1)[0]
        trajectory = []
        for _ply in range(window_plies):
            outgoing = moves.get(key)
            if not outgoing:
                break
            games = [g for _c, _u, g in outgoing]
            if not sum(games):
                break
            child, uci, _games = rng.choices(outgoing, weights=games, k=1)[0]
            events = events_for(key, uci, child)
            trajectory.extend(events)
            unique.update(events)
            if child not in positions:
                break
            key = child
        trajectories.append(trajectory)
    diag['unique_events'] = len(unique)
    return trajectories, diag


def mine_motifs(trajectories, min_support=0.05, max_size=4):
    """Weighted Apriori over sampled trajectories, with order flexibility.

    A trajectory contains a set if it contains every event at least once.
    support(S) = share of sampled trajectories containing S.
    """
    n = len(trajectories)
    if n == 0:
        return []
    sets = [set(t) for t in trajectories]
    firsts = []
    for trajectory in trajectories:
        first = {}
        for i, event in enumerate(trajectory):
            first.setdefault(event, i)
        firsts.append(first)

    tid_of = defaultdict(set)
    for i, txn in enumerate(sets):
        for event in txn:
            tid_of[event].add(i)
    min_count = min_support * n
    level = {frozenset((event,)): tids for event, tids in tid_of.items() if len(tids) >= min_count}
    frequent = dict(level)
    while level and len(next(iter(level))) < max_size:
        items = list(level)
        candidates = set()
        for a, b in combinations(items, 2):
            union = a | b
            if len(union) != len(a) + 1:
                continue
            if all(frozenset(sub) in frequent for sub in combinations(union, len(a))):
                candidates.add(union)
        next_level = {}
        for candidate in candidates:
            parts = list(candidate)
            tids = level[frozenset(parts[:-1])] & level[frozenset(parts[1:])]
            if len(tids) >= min_count:
                next_level[candidate] = tids
        frequent.update(next_level)
        level = next_level

    motifs = []
    for events, tids in frequent.items():
        events = list(events)
        support = len(tids) / n
        flexibility, canonical = _order_flexibility(events, tids, firsts)
        events = list(canonical)
        motifs.append({'events': events, 'size': len(events), 'support': support,
                       'conditional_frequency': support, 'order_flexibility': flexibility,
                       'family_id': '', 'family_lift': 1.0, 'sample_mass': float(n),
                       'events_json': json.dumps([list(e) for e in events])})
    motifs.sort(key=lambda m: (-m['support'], m['size'], m['events']))
    return motifs


def _order_flexibility(events, tids, firsts):
    """Canonical order = the order respected by the largest share of trajectories.

    Returns (1 - canonical_share, canonical_events). For size 1 there is no order,
    so flexibility is 0 and the single event is canonical.
    """
    if len(events) < 2:
        return 0.0, tuple(events)
    orders = Counter()
    for i in tids:
        first = firsts[i]
        orders[tuple(sorted(events, key=lambda e: first[e]))] += 1
    canonical, count = min(orders.items(), key=lambda kv: (-kv[1], kv[0]))
    return 1.0 - count / len(tids), canonical


def mine_family(db, structure_id, source='local2200', n=20000, window_plies=12, min_support=0.05,
                fen_of=None, positions=None, moves=None):
    """Motifs for one family; ``family_lift`` is 1.0 until a pooled pass fills it in."""
    if positions is None or moves is None:
        positions, moves = structure_flow.load_graph_inputs(db, source)
    seeds = {row['position_key']: row['enter_mass'] for row in db.execute(
        "SELECT position_key, enter_mass FROM position_flow WHERE source=? AND enter_mass>0 "
        "AND position_key IN (SELECT position_key FROM position WHERE structure_id=?)",
        (source, structure_id))}
    trajectories, diag = sample_trajectories(db, seeds, positions, moves, n=n,
                                             window_plies=window_plies, fen_of=fen_of)
    motifs = mine_motifs(trajectories, min_support=min_support)
    for motif in motifs:
        motif['family_id'] = structure_id
        motif['conditional_frequency'] = motif['support']
        motif['family_lift'] = 1.0
    return motifs


def mine_families(db, structure_ids, source='local2200', n=20000, window_plies=12,
                  min_support=0.05, fen_of=None):
    """Mine several families, then fill family_lift against the pooled sample.

    Returns {structure_id: {'motifs': [...], 'diagnostics': {...}}}.
    """
    positions, moves = structure_flow.load_graph_inputs(db, source)
    result = {}
    for structure_id in structure_ids:
        seeds = {row['position_key']: row['enter_mass'] for row in db.execute(
            "SELECT position_key, enter_mass FROM position_flow WHERE source=? AND enter_mass>0 "
            "AND position_key IN (SELECT position_key FROM position WHERE structure_id=?)",
            (source, structure_id))}
        trajectories, diag = sample_trajectories(db, seeds, positions, moves, n=n,
                                                 window_plies=window_plies, fen_of=fen_of)
        motifs = mine_motifs(trajectories, min_support=min_support)
        for motif in motifs:
            motif['family_id'] = structure_id
            motif['conditional_frequency'] = motif['support']
        result[structure_id] = {'motifs': motifs, 'diagnostics': diag,
                                'trajectories': trajectories}
    _apply_pooled_lift(result)
    return result


def _apply_pooled_lift(per_family):
    pooled = [txn for payload in per_family.values() for txn in payload['trajectories']]
    pooled_sets = [set(t) for t in pooled]
    n = len(pooled_sets)
    needed = {frozenset(m['events']) for payload in per_family.values() for m in payload['motifs']}
    support = {events: (sum(1 for s in pooled_sets if events <= s) / n if n else 0.0)
               for events in needed}
    for payload in per_family.values():
        for motif in payload['motifs']:
            pooled_support = support[frozenset(motif['events'])]
            motif['family_lift'] = (motif['conditional_frequency'] / pooled_support
                                    if pooled_support > 0 else 0.0)


def format_event(event):
    kind = event[0]
    if kind == 'pawn_move':
        return f'pawn {event[1]}->{event[2]}'
    if kind == 'piece_move':
        return f'piece {event[1]} {event[2]}->{event[3]}'
    if kind == 'capture':
        return f'capture {event[1]} {event[3]}->{event[4]}'
    if kind == 'castle':
        return f'castle {event[1]} {"kingside" if event[2] == "k" else "queenside"}'
    if kind == 'file_open':
        return f'file {event[1]} opens'
    if kind == 'structure_exit':
        return 'structure exit'
    return ' '.join(str(part) for part in event if part is not None)


def motif_summary(motifs, top=20):
    """Motif dicts plus a 'description' built only from the event tuples."""
    ordered = sorted(motifs, key=lambda m: (-m['support'], m['size'], m['events']))[:top]
    return [dict(motif, description=' THEN '.join(format_event(e) for e in motif['events']))
            for motif in ordered]


def main(argv=None):
    parser = argparse.ArgumentParser(description='Motif mining (section 4 of STUDY-CURRICULUM.md).')
    parser.add_argument('--db', default=str(studydb.DEFAULT_ANALYSIS_DB))
    parser.add_argument('--families', default=None, help='comma-separated structure ids')
    parser.add_argument('--samples', type=int, default=20000)
    parser.add_argument('--window-plies', type=int, default=12)
    parser.add_argument('--min-support', type=float, default=0.05)
    parser.add_argument('--top', type=int, default=10)
    args = parser.parse_args(argv)

    db = studydb.readonly(args.db)
    db.row_factory = sqlite3.Row
    if args.families:
        families = [f.strip() for f in args.families.split(',') if f.strip()]
    else:
        families = [row['structure_id'] for row in templates.candidate_pool(db)][:3]

    result = mine_families(db, families, n=args.samples, window_plies=args.window_plies,
                           min_support=args.min_support)
    print(f'motif mining: {len(families)} families, {args.samples} samples each, '
          f'window {args.window_plies} plies, min support {args.min_support}')
    print('mass is approximate Monte-Carlo (sampling proportional to edge mass).\n')
    for structure_id in families:
        payload = result[structure_id]
        diag = payload['diagnostics']
        print(f'== family {structure_id} ==')
        entry = db.execute('SELECT entry_mass FROM structure_maturity WHERE structure_id=? AND source=?',
                           (structure_id, 'local2200')).fetchone()
        coverage = f"{diag['seeds_mass'] / entry['entry_mass']:.4f}" if entry and entry['entry_mass'] else 'n/a'
        print(f"   samples={diag['samples']} window_plies={diag['window_plies']} "
              f"seed={diag['seed']} seeds_mass={diag['seeds_mass']:.6f} "
              f"(sampled mass coverage={coverage} of family entry mass) "
              f"unique_events={diag['unique_events']}")
        print('   mass is approximate Monte-Carlo: seeds are drawn proportional to entering mass.')
        for motif in motif_summary(payload['motifs'], top=args.top):
            print(f"   support={motif['support']:.4f} P(m|fam)={motif['conditional_frequency']:.4f} "
                  f"lift={motif['family_lift']:.3f} flex={motif['order_flexibility']:.4f} "
                  f"n={motif['size']} :: {motif['description']}")
        print()


if __name__ == '__main__':
    main()
