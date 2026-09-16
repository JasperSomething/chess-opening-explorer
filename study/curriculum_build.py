"""Generate a Scandinavian course from the existing objects. No new data, no manual
chess advice, and no layer of abstraction on top of what already exists.

The generator selects from the objects the project already produced: structural
families (orientation), reusable schemas, exact decisions, and the exceptions that
modify them. Every teaching statement it emits carries provenance back to the family,
schema, plan, board and statistic it came from; nothing is authored by hand.

Hierarchy, and it is enforced in code rather than described:

    opening -> orientation/structure -> reusable schema -> critical exact decision
            -> exception/deviation

* orientation items are **recognition only** — they carry no answers and cannot
  prescribe a move;
* schemas are admitted by marginal value per marginal burden, and display their
  reusable transformations, applicability, ordering flexibility and completion state;
* exact decisions are admitted **only** where the residual analysis shows the selected
  schemas still leave meaningful value on the board;
* exceptions are attached to the schema or decision they modify, never shown as
  independent opening-tree branches.

Budgets are marginal burden units, so a course is "what fits in N units", and the
marginal value of every item is reported.
"""
import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import (components, compression, curriculum,  # noqa: E402
                   db as studydb, plans, structure_flow)

BUDGETS = (10, 25, 50)
# a decision is only admitted when it removes at least this much of a board's
# remaining ordinary-play loss; both a relative and an absolute floor, declared here
# rather than tuned after seeing what the generator picks
DECISION_MIN_SHARE_OF_RESIDUAL = 0.20
DECISION_MIN_ABSOLUTE = 0.005
# a board where the schema's prescription covers less than this much of strong play
# is an exception, not a rule
DEVIATION_MAX_STRONG_SHARE = 0.50


def provenance(*, family=None, schema_id=None, plan_ids=None, boards=None, statistics=None,
               sources=None):
    return {'family': family, 'schema_id': schema_id, 'plan_ids': plan_ids or [],
            'boards': boards or [], 'statistics': statistics or {},
            'sources': sources or []}


def orientation_items(db, library, evidence, source='local2200'):
    """Recognition knowledge: which structure the learner is in, and what it looks like.

    Carries no move recommendations at all, so it cannot pretend to prescribe one.
    """
    items = []
    reach = curriculum.load_reach(db, source)
    for structure_id in library['structures']:
        keys = [row['key'] for row in plans.plan_scope(
            db, {'structure_id': structure_id, 'side': None}, source)]
        if not keys:
            continue
        entries = [row for row in db.execute(
            'SELECT entry_mass, boards, ply_mean, dwell_mean, development_level '
            'FROM structure_maturity WHERE structure_id=? AND source=?',
            (structure_id, source)).fetchall()]
        mass = entries[0]['entry_mass'] if entries else 0.0
        representative = _representative_board(db, keys, reach)
        items.append({
            'item_id': f'orientation:{structure_id}',
            'kind': 'orientation', 'level': 'structure',
            'title': f'structure {structure_id[:8]}',
            'prescribes': None,
            'destinations': destinations(db, structure_id, source),
            'recognition': {
                'look_for': _structure_signature(db, structure_id, source),
                'family': structure_id,
                'development_level': entries[0]['development_level'] if entries else None,
                'ply_mean': entries[0]['ply_mean'] if entries else None,
                'dwell_mean': entries[0]['dwell_mean'] if entries else None,
            },
            'boards': len(keys),
            'flow_mass': mass,
            'reach': sum(reach.get(key, {}).get('reach', 0.0) for key in keys),
            'representative_board': representative,
            'burden': 1.0,
            'marginal_value': None,
            'provenance': provenance(family=structure_id, boards=[representative['fen']]
                                     if representative else [],
                                     statistics={'entry_mass': mass, 'boards': len(keys),
                                                 'reach': sum(reach.get(key, {}).get(
                                                     'reach', 0.0) for key in keys)},
                                     sources=['position', 'structure_maturity',
                                              'position_flow', 'move_source']),
        })
    return items


def _representative_board(db, keys, reach):
    """The board with the highest reach in the family: a real position, not a picture."""
    key = max(keys, key=lambda candidate: reach.get(candidate, {}).get('reach', 0.0))
    _role, fen = curriculum.position_role(db, key)
    games = sum(row['games'] for row in db.execute(
        'SELECT games FROM move_source WHERE position_key=? AND source=?', (key, 'local2200')))
    return {'position_key': key.hex(), 'fen': fen, 'reach': reach.get(key, {}).get('reach', 0.0),
            'games': games}


def _structure_signature(db, structure_id, source='local2200'):
    """What is actually on the board in this family, read from the existing family objects.

    This is recognition knowledge: which pieces sit on which squares, and which pawns
    are committed. Nothing here is interpreted or named, and nothing prescribes a move.
    """
    from study import family as family_module
    signature = {'recurring_occupancy': [], 'pawn_commitments': [], 'castling': {},
                 'development': {}}
    try:
        fam = family_module.load_family(db, structure_id, source)
    except Exception:
        return signature
    if not fam['positions']:
        return signature
    occupancy, _weight = family_module.piece_occupancy(fam, 'reach_flow')
    # shares are per piece type: summing across piece types makes every share tiny and
    # meaningless, which is a rendering error rather than a property of the data
    by_piece = []
    for name in family_module.PIECES:
        piece_total = sum(occupancy[name].values()) or 0.0
        if piece_total <= 0:
            continue
        top = sorted(occupancy[name].items(), key=lambda kv: -kv[1])[:4]
        by_piece.append({
            'piece': family_module.PIECE_NAMES[name],
            'squares': [{'square': family_module.SQUARE_NAME[square],
                         'share': weight / piece_total} for square, weight in top]})
    by_piece.sort(key=lambda entry: -entry['squares'][0]['share'])
    signature['recurring_occupancy'] = by_piece
    pawns = defaultdict(float)
    for name in ('wp', 'bp'):
        for square, weight in occupancy[name].items():
            pawns[family_module.SQUARE_NAME[square]] += weight
    pawn_total = sum(pawns.values()) or 1.0
    signature['pawn_commitments'] = [
        {'square': square, 'share': weight / pawn_total}
        for square, weight in sorted(pawns.items(), key=lambda kv: -kv[1])[:8]]
    row = db.execute('SELECT white_pawns, black_pawns FROM structure WHERE structure_id=?',
                     (structure_id,)).fetchone()
    if row:
        signature['pawn_structure'] = {'white': row['white_pawns'], 'black': row['black_pawns']}
    return signature


def destinations(db, structure_id, source='local2200', top=12):
    """Recurring piece/pawn destinations inside a family: the arrows the UI draws.

    Read from strong-play move distributions at the family's boards, weighted by reach.
    """
    reach = curriculum.load_reach(db, source)
    counts = defaultdict(float)
    for (key,) in db.execute('SELECT position_key FROM position WHERE structure_id=?',
                             (structure_id,)):
        weight = reach.get(key, {}).get('reach', 0.0)
        if weight <= 0:
            continue
        _role, fen = curriculum.position_role(db, key)
        board = _board(fen)
        if board is None:
            continue
        for row in db.execute('SELECT uci, games FROM move_source WHERE position_key=? '
                              'AND source=?', (key, source)):
            try:
                move = chess_move(row['uci'])
            except Exception:
                continue
            if move is None or not board.is_legal(move):
                continue
            piece = board.piece_at(move.from_square)
            if piece is None:
                continue
            counts[(letter_of(piece), square_name(move.from_square),
                    square_name(move.to_square))] += weight * row['games']
    total = sum(counts.values()) or 1.0
    return [{'piece': piece, 'from': origin, 'to': destination, 'share': weight / total}
            for (piece, origin, destination), weight in
            sorted(counts.items(), key=lambda kv: -kv[1])[:top]]


def chess_move(uci):
    import chess
    try:
        return chess.Move.from_uci(uci)
    except Exception:
        return None


def square_name(square):
    import chess
    return chess.square_name(square)


def letter_of(piece):
    import chess
    from study import motifs
    return motifs.PIECE_LETTER[piece.piece_type]


def schema_course_items(payload, library, evidence):
    """Schemas as learnable objects: transformations, applicability, ordering, completion."""
    schemas = components.schemas(library, 'goal', min_plans=2, max_size=3)
    by_plan = {plan['plan_id']: plan for plan in library['plans']}
    items = []
    for schema in schemas:
        covered = [by_plan[pid] for pid in schema['covered_plans'] if pid in by_plan]
        if not covered:
            continue
        orderings = []
        for plan in covered:
            orderings.extend(components.plan_orderings(plan, 'goal'))
        flexibility = (statistics.fmean(plan['order_flexibility'] for plan in covered)
                       if covered else None)
        boards = {}
        for plan in covered:
            for row in plan['rows']:
                key = bytes.fromhex(row['position_key'])
                board = evidence.get(key)
                if board is None:
                    continue
                explained = min(1.0, sum(board['strong_shares'].get(uci, 0.0)
                                         for uci in row['answers']))
                previous = boards.get(key)
                if previous is None or row['loss_taught'] < previous['loss']:
                    boards[key] = {'loss': row['loss_taught'], 'explained': explained,
                                   'answers': row['answers']}
        if not boards:
            continue
        items.append({
            'item_id': f"schema:{schema['schema_id']}",
            'kind': 'schema', 'level': 'schema',
            'title': ' + '.join(_component_text(component)
                                for component in schema['required']),
            'prescribes': None,
            'transformations': {
                'required': [_component_detail(component) for component in schema['required']],
                'optional': [_component_detail(component) for component in schema['optional']],
                'residual': [_component_detail(component)
                             for component in schema['residual_components'][:6]],
            },
            'applicability': {
                'families': sorted({plan['structure_id'] for plan in covered}),
                'sides': sorted({plan['side'] for plan in covered}),
                'boards': len(boards),
            },
            'ordering': {'flexibility': flexibility,
                         'constrained': flexibility is not None and flexibility < 0.15,
                         'constraints': [_ordering_text(entry) for entry in orderings[:6]]},
            'completion_state': _completion_state(payload, boards),
            'covered_plans': schema['covered_plans'],
            'positions': schema['positions'], 'flow_mass': schema['flow_mass'],
            'boards': len(boards), 'boards_map': {key.hex(): value
                                                  for key, value in boards.items()},
            'burden': float(len(schema['required'])),
            'marginal_value': None,
            'provenance': provenance(
                family=covered[0]['structure_id'], schema_id=schema['schema_id'],
                plan_ids=schema['covered_plans'],
                boards=[key.hex() for key in list(boards)[:20]],
                statistics={'covered_plans': schema['plans'], 'positions': schema['positions'],
                            'flow_mass': schema['flow_mass'],
                            'order_flexibility': flexibility},
                sources=['plan-library', 'position', 'move_source', 'eval']),
        })
    return items


def _completion_state(payload, boards):
    """Completion is a board state test: which of the schema's transformations are done."""
    db = payload['db']
    positions = payload.get('positions_graph')
    if positions is None:
        positions, _moves = structure_flow.load_graph_inputs(db, 'local2200')
        payload['positions_graph'] = positions
    states = defaultdict(int)
    for key in boards:
        _role, fen = curriculum.position_role(db, key)
        board = _board(fen)
        if board is None:
            continue
        for event in _events_of(payload, key):
            states[plans.event_state(board, event)] += 1
    total = sum(states.values()) or 1
    return {state: {'count': count, 'share': count / total}
            for state, count in states.items()}


_EVENT_CACHE = {}


def _events_of(payload, key):
    """The transformation events that occur at this board in strong play (cached)."""
    if key in _EVENT_CACHE:
        return _EVENT_CACHE[key]
    db = payload['db']
    events = set()
    for row in db.execute('SELECT uci FROM move_source WHERE position_key=? AND source=? '
                          'AND games > 0 ORDER BY games DESC LIMIT 6', (key, 'local2200')):
        _role, fen = curriculum.position_role(db, key)
        board = _board(fen)
        if board is None:
            continue
        try:
            for event in _classify(board, row['uci']):
                events.add(event)
        except Exception:
            continue
    _EVENT_CACHE[key] = events
    return events


def _classify(board, uci):
    from study import motifs
    return motifs.classify_move(board, uci, None, {'structure': ''}, {})


def _board(fen):
    import chess
    try:
        return chess.Board(fen + ' 0 1')
    except Exception:
        return None


def decision_course_items(payload, evidence, selected_schemas):
    """Exact decisions, admitted only against the residual the schemas leave behind."""
    covered_loss, covered_explained = {}, {}
    for item in selected_schemas:
        for position_key, effect in item['boards_map'].items():
            key = bytes.fromhex(position_key)
            if key not in covered_loss or effect['loss'] < covered_loss[key]:
                covered_loss[key] = effect['loss']
            covered_explained[key] = max(covered_explained.get(key, 0.0), effect['explained'])
    items = []
    for item in payload['decisions']:
        key = list(item.boards)[0]
        board = evidence.get(key)
        if board is None:
            continue
        remaining = board['loss_pop'] - covered_loss.get(key, board['loss_pop'])
        if remaining <= 0:
            continue
        effect = item.boards[key]
        gained = max(0.0, covered_loss.get(key, board['loss_pop']) - effect['loss'])
        if gained < DECISION_MIN_ABSOLUTE:
            continue
        if gained < DECISION_MIN_SHARE_OF_RESIDUAL * remaining:
            continue
        items.append({
            'item_id': item.item_id, 'kind': 'decision', 'level': 'decision',
            'title': f"exact move at {item.item_id.split(':')[1][:8]}",
            'prescribes': {'moves': [item.item_id.split(':')[-1]],
                           'source': 'engine evaluation of the best move at this board'},
            'board': {'position_key': key.hex(), 'fen': board['fen'],
                      'reach': board['reach']},
            'why_admitted': {
                'residual_before': remaining,
                'value_gained': gained,
                'share_of_residual': gained / remaining if remaining else None,
                'rule': (f'at least {DECISION_MIN_ABSOLUTE} value and at least '
                         f'{DECISION_MIN_SHARE_OF_RESIDUAL:.0%} of the residual the '
                         f'schemas leave'),
            },
            'burden': 1.0, 'marginal_value': None,
            'provenance': provenance(
                family=None, boards=[key.hex()],
                statistics={'loss_pop': board['loss_pop'], 'loss_after_decision': effect['loss'],
                            'reach': board['reach'], 'strong_share': board['strong_shares'].get(
                                item.item_id.split(':')[-1], 0.0)},
                sources=['eval', 'move_source', 'position']),
        })
    return items


def exception_course_items(payload, library, evidence, selected):
    """Deviations, attached to the schema or decision they modify.

    A board where the schema's prescription covers less than half of what strong
    players actually played is a deviation, and it is stored under that schema — never
    as a branch of its own. The observed strong move and its share are recorded so the
    learner can see what happens instead.
    """
    items = []
    for parent in selected:
        if parent['kind'] not in ('schema', 'decision'):
            continue
        boards = (parent.get('boards_map') or {})
        for position_key, effect in boards.items():
            if effect['explained'] >= DEVIATION_MAX_STRONG_SHARE:
                continue
            key = bytes.fromhex(position_key)
            board = evidence.get(key)
            if board is None:
                continue
            ranked = sorted(board['strong_shares'].items(), key=lambda kv: -kv[1])
            if not ranked:
                continue
            move, share = ranked[0]
            items.append({
                'item_id': f"exception:{parent['item_id']}:{position_key[:8]}",
                'kind': 'exception', 'level': 'exception',
                'modifies': parent['item_id'],
                'title': f"at {position_key[:8]} strong play chooses {move}",
                'prescribes': {'moves': [move],
                               'source': 'observed strong-play choice at this board'},
                'board': {'position_key': position_key, 'fen': board['fen'],
                          'reach': board['reach']},
                'why_selected': {
                    'schema_share_of_strong_play': effect['explained'],
                    'threshold': DEVIATION_MAX_STRONG_SHARE,
                    'observed_move': move, 'observed_share': share,
                },
                'burden': 1.0,
                'marginal_value': None,
                'provenance': provenance(
                    family=parent.get('provenance', {}).get('family'),
                    schema_id=parent.get('provenance', {}).get('schema_id'),
                    boards=[position_key],
                    statistics={'observed_share': share,
                                'schema_explained': effect['explained'],
                                'reach': board['reach']},
                    sources=['move_source', 'position', 'eval']),
            })
    return items


def _candidate_boards(item, evidence):
    """What an item does, board by board, in the form the selector needs."""
    if item.get('boards_map'):
        return item['boards_map']
    if 'board' in item:
        position_key = item['board']['position_key']
        key = bytes.fromhex(position_key)
        context = evidence.get(key)
        if context is None:
            return {}
        moves = (item.get('prescribes') or {}).get('moves') or []
        explained = min(1.0, sum(context['strong_shares'].get(move, 0.0) for move in moves))
        # an exact decision is the engine-best move at that board, so its own loss is
        # zero by construction; what it buys is the board's whole remaining loss
        return {position_key: {'loss': 0.0, 'explained': explained}}
    return {}


def select(payload, library, evidence, budget, pool):
    """Greedy marginal selection under a budget of marginal burden units.

    Value is measured on the shared surface with the max-not-sum rule, and the
    behavioural target is tracked alongside it. Orientation is included first because
    it is recognition knowledge: it costs a unit and prescribes nothing.
    """
    selected, spent = [], 0.0
    state_loss, state_explained = {}, {}
    orientation = [item for item in pool if item['kind'] == 'orientation']
    candidates = [item for item in pool if item['kind'] != 'orientation']
    for item in orientation:
        if spent + item['burden'] > budget:
            break
        selected.append(item)
        spent += item['burden']
    marginal_log = []
    while candidates:
        best, best_gain, best_payload = None, None, None
        for item in candidates:
            boards = _candidate_boards(item, evidence)
            if not boards:
                continue
            gain = 0.0
            for position_key, effect in boards.items():
                key = bytes.fromhex(position_key)
                context = evidence.get(key)
                if context is None:
                    continue
                previous = state_loss.get(key)
                if previous is None or effect['loss'] < previous:
                    gain += context['reach'] * max(0.0, context['loss_pop'] - effect['loss'])
                    if previous is not None:
                        gain -= context['reach'] * max(0.0, context['loss_pop'] - previous)
                previous_explained = state_explained.get(key, 0.0)
                if effect['explained'] > previous_explained:
                    # behavioural progress counts, but at a declared quarter weight: this
                    # phase is about showing the learner a course, and value leads
                    gain += 0.25 * context['reach'] * (effect['explained']
                                                       - previous_explained)
            if best_gain is None or gain > best_gain:
                best, best_gain, best_payload = item, gain, (gain, boards)
        if best is None:
            break
        gain, boards = best_payload
        if spent + best['burden'] > budget:
            candidates.remove(best)
            continue
        if gain <= 0:
            break
        for position_key, effect in boards.items():
            key = bytes.fromhex(position_key)
            context = evidence.get(key)
            if context is None:
                continue
            current = state_loss.get(key)
            if current is None or effect['loss'] < current:
                state_loss[key] = effect['loss']
            if effect['explained'] > state_explained.get(key, 0.0):
                state_explained[key] = effect['explained']
        best['marginal_value'] = gain
        selected.append(best)
        spent += best['burden']
        marginal_log.append({'item_id': best['item_id'], 'kind': best['kind'],
                             'marginal_burden': best['burden'], 'marginal_value': gain,
                             'value_per_burden': gain / best['burden'] if best['burden'] else None,
                             'cumulative_burden': spent})
        candidates.remove(best)
    report = budget_report(selected, evidence, state_loss, state_explained)
    return selected, report, marginal_log


def budget_report(selected, evidence, state_loss, state_explained):
    family_reach = sum(board['reach'] for board in evidence.values())
    denominator = sum(board['reach'] * board['loss_pop'] for board in evidence.values())
    value = sum(evidence[key]['reach'] * max(0.0, evidence[key]['loss_pop'] - loss)
                for key, loss in state_loss.items())
    explained = sum(evidence[key]['reach'] * state_explained.get(key, 0.0)
                    for key in state_explained)
    return {
        'items': len(selected),
        'burden': sum(item['burden'] for item in selected),
        'schemas': sum(1 for item in selected if item['kind'] == 'schema'),
        'orientation': sum(1 for item in selected if item['kind'] == 'orientation'),
        'decisions': sum(1 for item in selected if item['kind'] == 'decision'),
        'exceptions': sum(1 for item in selected if item['kind'] == 'exception'),
        'positions_covered': len(state_loss),
        'positions_memorised': sum(1 for item in selected if item['kind'] == 'decision'),
        'flow_coverage': (sum(evidence[key]['reach'] for key in state_loss) / family_reach
                          if family_reach else None),
        'behaviour_explained': explained / family_reach if family_reach else None,
        'value_recovered': value,
        'recovery': value / denominator if denominator else None,
        'residual_value': max(0.0, denominator - value),
        'residual_share': max(0.0, 1 - value / denominator) if denominator else None,
    }


def representative_line(db, structure_id, plies=8, source='local2200'):
    """A representative move order: always the highest-mass edge, so it is data-chosen."""
    _positions, moves = structure_flow.load_graph_inputs(db, source)
    start = None
    for (key,) in db.execute('SELECT position_key FROM position WHERE structure_id=?',
                             (structure_id,)):
        if moves.get(key):
            start = key
            break
    if start is None:
        return []
    line, key = [], start
    for _ply in range(plies):
        outgoing = moves.get(key)
        if not outgoing:
            break
        child, uci, games = max(outgoing, key=lambda edge: edge[2])
        _role, fen = curriculum.position_role(db, key)
        line.append({'position_key': key.hex(), 'fen': fen, 'uci': uci, 'games': games})
        key = child
    return line


def build_course(db_path=ROOT / 'data' / 'atlas-analysis.sqlite', budgets=BUDGETS):
    payload = compression.build(db_path)
    library, evidence, db = payload['library'], payload['evidence'], payload['db']
    orientation = orientation_items(db, library, evidence)
    schemas = schema_course_items(payload, library, evidence)
    courses = {}
    for budget in budgets:
        pool = orientation + schemas
        selected_schemas, _, _ = select(payload, library, evidence, budget, pool)
        decisions = decision_course_items(payload, evidence,
                                          [item for item in selected_schemas
                                           if item['kind'] == 'schema'])
        pool2 = selected_schemas + decisions
        selected, report, marginal = select(payload, library, evidence, budget, pool2)
        # selection is driven by marginal value, but the learner is shown the hierarchy
        # in order: orientation, then schemas, then the decisions that survived the
        # residual test. The marginal log keeps the true selection order.
        rank = {'orientation': 0, 'schema': 1, 'decision': 2, 'exception': 3}
        selected = sorted(selected, key=lambda item: (rank.get(item['kind'], 9),
                                                      -(item.get('marginal_value') or 0.0)))
        exceptions = exception_course_items(payload, library, evidence, selected)
        report['exceptions_available'] = len(exceptions)
        report['decisions_offered'] = len(decisions)
        courses[str(budget)] = {'budget': budget, 'items': selected, 'report': report,
                                'marginal': marginal, 'exceptions': exceptions,
                                'lines': {item['provenance'].get('family'):
                                          representative_line(db, item['provenance'].get('family'))
                                          for item in selected
                                          if item.get('provenance', {}).get('family')}}
    db.close()
    return {'budgets': list(budgets), 'courses': courses,
            'constants': {'DECISION_MIN_SHARE_OF_RESIDUAL': DECISION_MIN_SHARE_OF_RESIDUAL,
                          'DECISION_MIN_ABSOLUTE': DECISION_MIN_ABSOLUTE,
                          'DEVIATION_MAX_STRONG_SHARE': DEVIATION_MAX_STRONG_SHARE}}


def _component_text(component):
    kind = component[0]
    if kind == 'goal':
        return f"{component[1]} to {component[2]}"
    if kind == 'castle':
        return f"castle {component[2]}"
    if kind == 'transition':
        return 'the position leaves this structure'
    if kind == 'transform':
        return f"{component[2]} {component[3]}-{component[4]}"
    return str(component)


def _component_detail(component):
    return {'component': list(component), 'text': _component_text(component)}


def _ordering_text(ordering):
    _prefix, first, second = ordering
    return f"{_component_text(first)} before {_component_text(second)}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=ROOT / 'data' / 'atlas-analysis.sqlite')
    parser.add_argument('--out', type=Path, default=ROOT / 'analysis' / 'course.json')
    parser.add_argument('--budgets', type=int, nargs='*', default=list(BUDGETS))
    options = parser.parse_args()
    course = build_course(options.db, tuple(options.budgets))
    options.out.parent.mkdir(parents=True, exist_ok=True)
    options.out.write_text(json.dumps(course, indent=1, default=str))
    print(json.dumps({'out': str(options.out),
                      'reports': {budget: entry['report']
                                  for budget, entry in course['courses'].items()}},
                     indent=1, default=str))


if __name__ == '__main__':
    main()
