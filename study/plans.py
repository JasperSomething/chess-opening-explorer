"""Plan objects and the family-oracle decomposition.

This module answers a different question from the frozen family-rule experiment:
can a *recurring plan* — a partially ordered set of state transformations — compress
move choice better than a family → fixed-answer rule?

Three things are kept strictly apart, because conflating them is how an experiment
rescues a hypothesis:

* **plans are built from strong-play trajectories only** (the existing motif miner);
  the engine never selects an action or contributes a transformation. A plan whose
  implied action cannot be read off the strong-play data is marked
  *non-prescriptive* on that board and counted, never valued at zero;
* **engine evaluations are used for valuation only** — the EP of the moves a plan
  already prescribes, and the baseline they are compared against;
* the **family oracle is an upper bound, not a learnable item**. It may pick a
  different action on every board, so its EV is reported explicitly as a bound, and
  the gap to the fixed rule is the headroom any plan could hope to recover.

A transformation is a motif event (see ``study/motifs.py``): a piece move, a pawn
move, a capture, castling, a file opening, or a structural-family exit. Completion
is decided by a *state test* on the board — never by asking an engine.
"""
import argparse
import hashlib
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import chess  # noqa: E402

from study import (campaign, curriculum, db as studydb, metrics,  # noqa: E402
                   motifs, structure_flow)

WEIGHTS = {'transformations': 0.5, 'conditions': 0.5, 'exceptions': 1.0, 'boards': 1.0}
ACTIONABLE_KINDS = {'pawn_move', 'piece_move', 'castle'}


def _sample_coloured(db, seeds, positions, moves, role, n=20000, window_plies=12,
                     seed=1337, fen_of=None):
    """Edge-mass trajectory sampling that also records whose move each event was.

    Same weighting and stopping rule as ``motifs.sample_trajectories`` (which is what
    makes the mined motifs comparable), plus the mover's colour: motif events do not
    carry it, and a plan has to be about the learner's own moves.
    """
    import random
    fen_of = fen_of or motifs._fen_reader(db)
    keys = sorted((key for key in seeds if key in positions), key=lambda key: (str(key),))
    weights = [float(seeds[key]) for key in keys]
    total = sum(weights)
    if not keys or total <= 0:
        return [], {}
    rng = random.Random(seed)
    board_cache, event_cache, colours = {}, {}, {}
    trajectories = []
    for _ in range(n):
        key = rng.choices(keys, weights=weights, k=1)[0]
        trajectory = []
        for _ply in range(window_plies):
            outgoing = moves.get(key)
            if not outgoing:
                break
            games = [games for _child, _uci, games in outgoing]
            if not sum(games):
                break
            child, uci, _games = rng.choices(outgoing, weights=games, k=1)[0]
            cached = event_cache.get((key, uci))
            if cached is None:
                board = board_cache.get(key)
                if board is None:
                    board = chess.Board(fen_of(key) + ' 0 1')
                    board_cache[key] = board
                colour = 'white_to_move' if board.turn == chess.WHITE else 'black_to_move'
                cached = [(event, colour) for event in
                          motifs.classify_move(board, uci, child, positions[key], positions)]
                event_cache[(key, uci)] = cached
            for event, colour in cached:
                trajectory.append(event)
                colours[event] = colour
            if child not in positions:
                break
            key = child
        trajectories.append(trajectory)
    return trajectories, colours
COMPLETE, ACTIONABLE, UNKNOWN = 'complete', 'actionable', 'unknown'


# --------------------------------------------------------------------------- state
def event_state(board, event):
    """Is this transformation already done, still available, or not decidable?

    ``unknown`` is a real answer: a capture is an effect rather than a state, and a
    structural exit cannot be read off a single board. A plan containing an
    undecidable transformation is marked non-prescriptive where that matters, rather
    than being credited with a guess.
    """
    kind = event[0]
    turn = board.turn
    if kind in ('pawn_move', 'piece_move'):
        from_square = chess.parse_square(event[-2] if kind == 'piece_move' else event[1])
        piece = board.piece_at(from_square)
        if kind == 'piece_move':
            letter = event[1]
            if piece is None:
                return COMPLETE
            if piece.color != turn:
                return UNKNOWN                   # an opponent piece: not this side's plan
            if motifs.PIECE_LETTER[piece.piece_type] == letter:
                return ACTIONABLE
            return COMPLETE
        if piece is None:
            return COMPLETE                      # the pawn is no longer there
        if piece.color != turn:
            return UNKNOWN                       # an opponent piece: not this side's plan
        if piece.piece_type == chess.PAWN:
            return ACTIONABLE
        return COMPLETE
    if kind == 'castle':
        colour = chess.WHITE if event[1] == 'w' else chess.BLACK
        home = chess.E1 if colour == chess.WHITE else chess.E8
        king = board.king(colour)
        if king is None:
            return COMPLETE
        return ACTIONABLE if king == home else COMPLETE
    if kind == 'file_open':
        pawns = (board.pieces(chess.PAWN, chess.WHITE)
                 | board.pieces(chess.PAWN, chess.BLACK))
        file_index = ord(event[1]) - ord('a')
        return COMPLETE if not (pawns & chess.BB_FILES[file_index]) else UNKNOWN
    return UNKNOWN                                # capture / structure_exit


def moves_advancing(board, events, structure_id, positions):
    """Legal moves that perform one of ``events``, mapped to the events they advance.

    Purely syntactic: the mapping is read from the move itself, so a move is never
    included because an engine likes it.
    """
    remaining = set(events)
    out = {}
    parent = {'structure': structure_id}
    for move in board.legal_moves:
        uci = move.uci()
        produced = motifs.classify_move(board, uci, None, parent, positions)
        matched = [event for event in produced if event in remaining]
        if matched:
            out[uci] = matched
    return out


# --------------------------------------------------------------------------- plans
def derive_plans(db, structure_ids, source='local2200', n=20000, window_plies=12,
                 min_support=0.05, max_size=4, seed=1337, min_transformations=2,
                 positions=None, moves=None, only_movable=True):
    """Mine plans per (family, side to move) from strong-play trajectories.

    Seeding per side is what gives a plan an explicit `side`: the events themselves
    do not carry the mover's colour.
    """
    if positions is None or moves is None:
        positions, moves = structure_flow.load_graph_inputs(db, source)
    fen_of = motifs._fen_reader(db)
    best = {}
    for structure_id in structure_ids:
        roles = [row['role'] for row in db.execute(
            'SELECT DISTINCT role FROM position WHERE structure_id=?', (structure_id,))]
        for role in roles:
            seeds = {row['position_key']: row['enter_mass'] for row in db.execute(
                'SELECT position_key, enter_mass FROM position_flow WHERE source=? '
                'AND enter_mass>0 AND position_key IN (SELECT position_key FROM position '
                'WHERE structure_id=? AND role=?)', (source, structure_id, role))}
            if not seeds:
                continue
            trajectories, colours = _sample_coloured(db, seeds, positions, moves, role,
                                                     n=n, window_plies=window_plies,
                                                     seed=seed, fen_of=fen_of)
            mined = motifs.mine_motifs(trajectories, min_support=min_support,
                                       max_size=max_size)
            for motif in mined:
                # a plan is what the learner does: keep only the mover's own
                # transformations, and re-measure support on that subset
                own = [e for e in motif['events'] if colours.get(e) == role]
                if len(own) < min_transformations:
                    continue
                own_set = set(own)
                support = sum(1 for txn in trajectories if own_set <= set(txn)) / max(n, 1)
                if support < min_support:
                    continue
                key = (structure_id, role, tuple(sorted(own, key=repr)))
                previous = best.get(key)
                if previous is None or support > previous['support']:
                    best[key] = {
                        'plan_id': hashlib.sha1(
                            f"{structure_id}|{role}|{sorted(own, key=repr)}".encode()).hexdigest()[:12],
                        'structure_id': structure_id, 'side': role,
                        'transformations': [list(e) for e in own],
                        'support': support,
                        'order_flexibility': motif['order_flexibility'],
                        'sample_mass': motif['sample_mass'],
                        'min_support': min_support,
                    }
    # many distinct motifs collapse onto the same own-event set (an effect event such
    # as a structural exit rides along with almost every move), so identical own-sets
    # are one plan, keeping the best support, and the id is derived from the set itself
    plans = sorted(best.values(), key=lambda plan: (plan['structure_id'], plan['side'],
                                                    -plan['support']))
    for plan in plans:
        kinds = {event[0] for event in plan['transformations']}
        plan['movable_transformations'] = sorted(kinds & ACTIONABLE_KINDS)
        plan['effect_only'] = not plan['movable_transformations']
    return [plan for plan in plans if not plan['effect_only'] or not only_movable]


def plan_scope(db, plan, source='local2200'):
    """Boards the plan can be applied to: the family, on the plan's side to move.

    ``side=None`` means the whole family, which the oracle uses (it is an upper bound
    over the family's vocabulary, not a side-conditioned item).
    """
    reach = curriculum.load_reach(db, source)
    if plan.get('side'):
        rows = db.execute('SELECT position_key FROM position WHERE structure_id=? AND role=?',
                          (plan['structure_id'], plan['side']))
    else:
        rows = db.execute('SELECT position_key FROM position WHERE structure_id=?',
                          (plan['structure_id'],))
    keys = [row['position_key'] for row in rows]
    return [{'key': key, 'reach': reach.get(key, {}).get('reach', 0.0)}
            for key in keys if curriculum.load_distributions(db, key, source)]


def evaluate_plan(db, plan, source='local2200', population='auto'):
    """Per-board prescriptiveness and value for one plan.

    Boards where the plan prescribes nothing are counted with a reason — they are
    *not prescriptive*, which is different from prescribing something worthless.
    """
    evals = curriculum.load_evals(db)
    positions, _moves = structure_flow.load_graph_inputs(db, source)
    events = [tuple(e) for e in plan['transformations']]
    rows = []
    unavailable = defaultdict(int)
    for board_row in plan_scope(db, plan, source):
        key, reach = board_row['key'], board_row['reach']
        _role, fen = curriculum.position_role(db, key)
        scores = evals.get(fen)
        dist_strong = curriculum.load_distributions(db, key, source)
        pop_source, dist_pop, _games = (curriculum.load_population(db, key)
                                        if population == 'auto'
                                        else (population,
                                              curriculum.load_distributions(db, key, population),
                                              0))
        if not scores or not dist_strong:
            unavailable['no_engine_evaluation'] += 1
            continue
        board = chess.Board(fen + ' 0 1')
        states = {event: event_state(board, event) for event in events}
        remaining = [event for event, state in states.items() if state == ACTIONABLE]
        if not remaining:
            unavailable['no_actionable_transformation'] += 1
            continue
        advancing = moves_advancing(board, remaining, plan['structure_id'], positions)
        # the taught set is restricted to moves strong players actually played here
        taught = {uci: matched for uci, matched in advancing.items() if uci in dist_strong}
        if not taught:
            unavailable['no_strong_play_support'] += 1
            continue
        best = max(entry['ep_wp'] for entry in scores.values())
        if not dist_pop or not any(uci in scores for uci in dist_pop):
            unavailable['no_population_distribution'] += 1
            continue
        value = campaign.value_of_answers(evals, scores, dist_strong, dist_pop, best,
                                          answers=set(taught))
        if value['gain'] is None:
            unavailable['no_population_distribution'] += 1
            continue
        rows.append({'position_key': key.hex(), 'reach': reach,
                     'gain': value['gain'], 'loss_pop': value['loss_pop'],
                     'loss_taught': value['loss_taught'], 'answers': sorted(taught),
                     'undecidable': sorted(event for event, state in states.items()
                                           if state == UNKNOWN),
                     'population_source': pop_source})
    weights = [row['reach'] or 1e-9 for row in rows]
    gains = [row['gain'] for row in rows]
    scope = plan_scope(db, plan, source)
    # burden is reported per dimension, never as one opaque number: a plan costs no
    # exact moves, but it does cost its transformations, the recognition conditions
    # needed to know they are still live, and its exceptions.
    state_tested = sorted({event[0] for event in events
                           if event[0] in ('pawn_move', 'piece_move', 'castle')})
    burden = {'transformations': len(events),
              'conditions': 1 + len(state_tested),
              'exceptions': unavailable['no_strong_play_support'],
              'boards': 1, 'exact_moves': 0,
              'condition_kinds': state_tested}
    burden['cost'] = (WEIGHTS['transformations'] * burden['transformations']
                      + WEIGHTS['conditions'] * burden['conditions']
                      + WEIGHTS['exceptions'] * burden['exceptions']
                      + WEIGHTS['boards'] * burden['boards'])
    weighted_total = sum(w * row['gain'] for w, row in zip(weights, rows))
    return {
        'plan': plan, 'scope_boards': len(scope), 'prescriptive_boards': len(rows),
        'unavailable': dict(unavailable),
        'reach_covered': sum(row['reach'] for row in rows),
        'scope_reach': sum(row['reach'] for row in scope),
        'ev_weighted_total': weighted_total,
        'ev_per_board': (weighted_total / sum(weights)) if weights else None,
        'median_gain': statistics.median(gains) if gains else None,
        'share_non_negative': (sum(1 for gain in gains if gain >= 0) / len(gains)
                               if gains else None),
        'answer_slots': sum(len(row['answers']) for row in rows),
        'distribution': _distribution(rows),
        'burden': burden,
        'boards': rows,
    }


def _distribution(rows):
    if not rows:
        return {'boards': 0}
    gains = sorted(row['gain'] for row in rows)
    weighted = sorted((row['reach'] * row['gain'] for row in rows), reverse=True)
    positive = [value for value in weighted if value > 0]
    return {'boards': len(gains), 'min': gains[0], 'median': statistics.median(gains),
            'max': gains[-1], 'mean': statistics.fmean(gains),
            'p25': gains[len(gains) // 4], 'p75': gains[(3 * len(gains)) // 4],
            'share_positive': sum(1 for gain in gains if gain > 0) / len(gains),
            'top_board_share_of_positive_total': (weighted[0] / sum(positive)
                                                  if positive and weighted[0] > 0 else None),
            'negative_total': sum(value for value in weighted if value < 0)}


# -------------------------------------------------------------------------- oracle
def recurring_vocabulary(db, structure_ids, source='local2200', n=20000, window_plies=12,
                         min_support=0.05, max_size=4, seed=1337):
    """The family's recurring transformation vocabulary: events the miner keeps.

    Used to tighten the oracle. A per-board set of "moves strong players happened to
    play here" recovers nearly everything by construction, which makes it useless for
    deciding whether a *recurring plan* could do the job. Restricting the candidates
    to transformations that recur across the family is the honest upper bound for a
    plan built from that same vocabulary.
    """
    positions, moves = structure_flow.load_graph_inputs(db, source)
    fen_of = motifs._fen_reader(db)
    vocabulary = {}
    for structure_id in structure_ids:
        events = set()
        roles = [row['role'] for row in db.execute(
            'SELECT DISTINCT role FROM position WHERE structure_id=?', (structure_id,))]
        for role in roles:
            seeds = {row['position_key']: row['enter_mass'] for row in db.execute(
                'SELECT position_key, enter_mass FROM position_flow WHERE source=? '
                'AND enter_mass>0 AND position_key IN (SELECT position_key FROM position '
                'WHERE structure_id=? AND role=?)', (source, structure_id, role))}
            if not seeds:
                continue
            trajectories, _colours = _sample_coloured(db, seeds, positions, moves, role,
                                                      n=n, window_plies=window_plies,
                                                      seed=seed, fen_of=fen_of)
            for motif in motifs.mine_motifs(trajectories, min_support=min_support,
                                            max_size=max_size):
                events.update(tuple(event) for event in motif['events'])
        vocabulary[structure_id] = events
    return vocabulary


def family_oracle(db, structure_id, source='local2200', population='auto', vocabulary=None):
    """The three levels for one family, and the headroom between them.

    1. recognition only — no action is implied, so EV is undefined, not zero;
    2. the frozen fixed-rule value (the completed negative experiment);
    3. a **family-conditioned oracle**: on each evaluated board, the best action
       available from the moves strong players actually played in that family. It is
       an upper bound on any plan over this vocabulary, not a learnable item.
    """
    evals = curriculum.load_evals(db)
    reach = curriculum.load_reach(db, source)
    positions_for_events, _moves_for_events = structure_flow.load_graph_inputs(db, source)
    from study import rules as rules_module
    rule_answers = {}
    for rule in rules_module.derive_all(db, source=source):
        if rule['kind'] == 'prescriptive' and rule['answers']:
            for key in rules_module.rule_scope(db, rule, source):
                rule_answers[key] = set(rule['answers'])
    rows, counts = [], defaultdict(int)
    for board_row in plan_scope(db, {'structure_id': structure_id,
                                     'side': None}, source):
        key = board_row['key']
        _role, fen = curriculum.position_role(db, key)
        scores = evals.get(fen)
        dist_strong = curriculum.load_distributions(db, key, source)
        pop_source, dist_pop, _games = (curriculum.load_population(db, key)
                                        if population == 'auto'
                                        else (population,
                                              curriculum.load_distributions(db, key, population),
                                              0))
        if not scores or not dist_strong or not dist_pop:
            counts['unavailable'] += 1
            continue
        best = max(entry['ep_wp'] for entry in scores.values())
        if not any(uci in scores for uci in dist_pop):
            counts['unavailable'] += 1
            continue
        ordinary = campaign.value_of_answers(evals, scores, dist_strong, dist_pop, best)
        if ordinary['loss_pop'] is None:
            counts['unavailable'] += 1
            continue
        candidates = [uci for uci in dist_strong if uci in scores]
        if not candidates:
            counts['no_family_candidate'] += 1
            continue
        oracle_loss = min(max(0.0, best - scores[uci]['ep_wp']) for uci in candidates)
        plan_loss = None
        if vocabulary:
            board = chess.Board(fen + ' 0 1')
            recurring = [uci for uci in candidates
                         if any(event in vocabulary
                                for event in motifs.classify_move(board, uci, None,
                                                                  {'structure': structure_id},
                                                                  positions_for_events))]
            # a recurring-vocabulary candidate must exist as a legal move here
            legal = {move.uci() for move in board.legal_moves}
            recurring = [uci for uci in recurring if uci in legal]
            if recurring:
                plan_loss = min(max(0.0, best - scores[uci]['ep_wp']) for uci in recurring)
            else:
                counts['no_recurring_candidate'] += 1
        fixed = None
        if key in rule_answers:
            value = campaign.value_of_answers(evals, scores, dist_strong, dist_pop, best,
                                              answers=rule_answers[key])
            fixed = value['loss_taught']
            if fixed is None:
                fixed = None
        rows.append({'position_key': key.hex(), 'reach': reach.get(key, {}).get('reach', 0.0),
                     'loss_pop': ordinary['loss_pop'], 'loss_oracle': oracle_loss,
                     'loss_recurring': plan_loss, 'loss_fixed': fixed,
                     'candidates': len(candidates)})
    total_reach = sum(row['reach'] for row in rows) or 1e-9
    oracle_ev = sum(row['reach'] * max(0.0, row['loss_pop'] - row['loss_oracle'])
                    for row in rows)
    fixed_rows = [row for row in rows if row['loss_fixed'] is not None]
    fixed_ev = sum(row['reach'] * max(0.0, row['loss_pop'] - row['loss_fixed'])
                   for row in fixed_rows)
    recurring_rows = [row for row in rows if row['loss_recurring'] is not None]
    recurring_ev = sum(row['reach'] * max(0.0, row['loss_pop'] - row['loss_recurring'])
                       for row in recurring_rows)
    population_loss = sum(row['reach'] * row['loss_pop'] for row in rows)
    return {
        'structure_id': structure_id,
        'boards_evaluated': len(rows),
        'boards_unavailable': counts['unavailable'],
        'boards_without_family_candidate': counts['no_family_candidate'],
        'boards_without_recurring_candidate': counts['no_recurring_candidate'],
        'recognition_only': {'ev': None,
                             'reason': 'no action is implied, so no EV is defined'},
        'fixed_rule': {'boards': len(fixed_rows), 'ev_reach_weighted': fixed_ev},
        'oracle_recurring': {'labelled_as': 'upper bound over the recurring transformation '
                                             'vocabulary only, not a learnable item',
                             'boards': len(recurring_rows),
                             'ev_reach_weighted': recurring_ev,
                             'median_board_ev': statistics.median(
                                 [max(0.0, row['loss_pop'] - row['loss_recurring'])
                                  for row in recurring_rows]) if recurring_rows else None},
        'oracle': {'labelled_as': 'upper bound, not a learnable curriculum item',
                   'ev_reach_weighted': oracle_ev, 'median_board_ev':
                   statistics.median([max(0.0, row['loss_pop'] - row['loss_oracle'])
                                      for row in rows]) if rows else None},
        'ordinary_play_loss': population_loss,
        'oracle_recovery': (oracle_ev / population_loss) if population_loss else None,
        'headroom_oracle_minus_fixed': oracle_ev - fixed_ev,
        'median_board_ev': statistics.median(
            [max(0.0, row['loss_pop'] - row['loss_oracle']) for row in rows]) if rows else None,
        'boards': rows,
    }


def retain(db, structures, source='local2200'):
    """Rank families as candidates for prescriptive compression."""
    out = []
    for structure_id in structures:
        report = family_oracle(db, structure_id, source)
        out.append({key: report[key] for key in
                    ('structure_id', 'boards_evaluated', 'boards_unavailable',
                     'oracle_recovery', 'headroom_oracle_minus_fixed', 'median_board_ev',
                     'ordinary_play_loss')} | {
                        'oracle_ev': report['oracle']['ev_reach_weighted'],
                        'fixed_ev': report['fixed_rule']['ev_reach_weighted']})
    return sorted(out, key=lambda row: -(row['oracle_recovery'] or 0.0))


def _summarise(reports):
    rows = []
    for report in reports:
        rows.append({
            'structure_id': report['structure_id'],
            'boards_evaluated': report['boards_evaluated'],
            'boards_unavailable': report['boards_unavailable'],
            'boards_without_recurring_candidate': report['boards_without_recurring_candidate'],
            'ordinary_play_loss': report['ordinary_play_loss'],
            'oracle_ev': report['oracle']['ev_reach_weighted'],
            'oracle_median_board_ev': report['oracle']['median_board_ev'],
            'oracle_recurring_ev': report['oracle_recurring']['ev_reach_weighted'],
            'oracle_recurring_boards': report['oracle_recurring']['boards'],
            'oracle_recurring_median_board_ev': report['oracle_recurring']['median_board_ev'],
            'fixed_rule_ev': report['fixed_rule']['ev_reach_weighted'],
            'fixed_rule_boards': report['fixed_rule']['boards'],
            'oracle_recovery': report['oracle_recovery'],
        })
    return sorted(rows, key=lambda row: -(row['oracle_ev'] or 0))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=ROOT / 'data' / 'atlas-analysis.sqlite')
    parser.add_argument('--source', default='local2200')
    parser.add_argument('--plans', action='store_true')
    parser.add_argument('--oracle', action='store_true')
    parser.add_argument('--structures', nargs='*')
    parser.add_argument('--out', default=str(ROOT / 'analysis' / 'plan-oracle.json'))
    options = parser.parse_args()
    db = studydb.connect(options.db, create=False)
    if options.structures:
        structures = options.structures
    else:
        structures = [row['structure_id'] for row in db.execute(
            'SELECT structure_id FROM structure_stat ORDER BY boards DESC LIMIT 8')]
    if options.oracle:
        vocabulary = recurring_vocabulary(db, structures, source=options.source)
        reports = []
        for structure_id in structures:
            report = family_oracle(db, structure_id, options.source,
                                   vocabulary=vocabulary.get(structure_id))
            reports.append(report)
        Path(options.out).write_text(json.dumps(reports, indent=1, default=str))
        print(json.dumps([{k: (round(v, 6) if isinstance(v, float) else v)
                           for k, v in row.items()} for row in _summarise(reports)], indent=1))
    if options.plans:
        plans = derive_plans(db, structures)
        print(json.dumps({'plans': len(plans),
                          'sample': plans[:3]}, indent=1, default=str))
    db.close()


if __name__ == '__main__':
    main()
