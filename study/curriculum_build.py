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
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import (components, compression, curriculum,  # noqa: E402
                   db as studydb, plans, structure_flow)

BUDGETS = (10, 25, 50)

# Every schema transformation is classified by what it is, generically by kind:
# * learner_action   - something the learner does on their move, and which the
#                      interface can test for completion on a board;
# * observable_state - a board fact that can be recognised but not performed by the
#                      learner (kept for recognition, never as the taught action);
# * outcome_effect   - a consequence of the position (a structure change, a file
#                      opening): legitimate to *expect*, never valid to *teach*.
TRANSFORMATION_CLASS = {
    'goal': 'learner_action',
    'transform': 'learner_action',
    'castle': 'learner_action',
    'exchange': 'learner_action',
    'file_open': 'outcome_effect',
    'transition': 'outcome_effect',
}
ACTIONABILITY_FLOOR = 1        # a prescriptive schema needs at least this many actions
# a decision is only admitted when it removes at least this much of a board's
# remaining ordinary-play loss; both a relative and an absolute floor, declared here
# rather than tuned after seeing what the generator picks
DECISION_MIN_SHARE_OF_RESIDUAL = 0.20
DECISION_MIN_ABSOLUTE = 0.005
# a board where the schema's prescription covers less than this much of strong play
# is an exception, not a rule
DEVIATION_MAX_STRONG_SHARE = 0.50
# a deviation earns curriculum attention only when it changes the recommended action
# and is both dominant at the board and actually reached by games
MATERIAL_DEVIATION_MIN_SHARE = 0.30
MATERIAL_DEVIATION_MIN_REACH = 1e-4


def provenance(*, family=None, schema_id=None, plan_ids=None, boards=None, statistics=None,
               sources=None):
    return {'family': family, 'schema_id': schema_id, 'plan_ids': plan_ids or [],
            'boards': boards or [], 'statistics': statistics or {},
            'sources': sources or []}


def family_feature_table(db, structures, source='local2200'):
    """Per-family piece/square and castling facts, for scoring distinctiveness.

    Computed from the existing family objects. The background is made of the other
    families under study, which is the comparison that matters: a fact is only worth
    telling a learner if it separates this family from the alternatives they will meet.
    """
    from study import family as family_module
    table = {}
    for structure_id in structures:
        try:
            fam = family_module.load_family(db, structure_id, source)
        except Exception:
            continue
        if not fam['positions']:
            continue
        counts, boards = Counter(), 0
        for _key, meta in fam['positions'].items():
            boards += 1
            for name in family_module.PIECES:
                bits = meta['pieces'][name]
                while bits:
                    square = (bits & -bits).bit_length() - 1
                    counts[(name, family_module.SQUARE_NAME[square])] += 1
                    bits &= bits - 1
        castled = {'white': 0, 'black': 0}
        for _key, meta in fam['positions'].items():
            castled['white'] += 1 if meta.get('white_castled') else 0
            castled['black'] += 1 if meta.get('black_castled') else 0
        table[structure_id] = {'counts': counts, 'boards': boards, 'castled': castled}
    return table


def distinctive_features(table, structure_id, top=8, universal=0.95, min_support=0.05):
    """Rank a family's facts by how much they separate it from the other families.

    A fact that holds almost everywhere (both here and in the alternatives) carries no
    information and is suppressed, however true it is. Features are scored by their
    information contribution p_family * log2(p_family / p_background), which is what
    makes 'the king is on e8' rank below 'the queen is on a5'.
    """
    own = table.get(structure_id)
    if not own or not own['boards']:
        return []
    background_boards = sum(entry['boards'] for key, entry in table.items()
                            if key != structure_id)
    background = Counter()
    for key, entry in table.items():
        if key != structure_id:
            background.update(entry['counts'])
    scored = []
    for (piece, square), count in own['counts'].items():
        p_family = count / own['boards']
        if p_family < min_support:
            continue
        p_background = (background[(piece, square)] / background_boards
                        if background_boards else 0.0)
        if p_family >= universal and p_background >= universal:
            continue                      # true everywhere: no distinguishing value
        lift = (p_family + 1e-6) / (p_background + 1e-6)
        score = p_family * math.log2(lift)
        scored.append({'piece': piece, 'square': square, 'p_family': p_family,
                       'p_background': p_background, 'lift': lift, 'score': score,
                       'suppressed': False})
    scored.sort(key=lambda row: -row['score'])
    chosen = scored[:top]
    for row in scored[top:]:
        row['suppressed'] = True
    return chosen, scored


def orientation_items(db, library, evidence, source='local2200'):
    """Recognition knowledge: which structure the learner is in, and what distinguishes it.

    Carries no move recommendations at all, so it cannot pretend to prescribe one. The
    facts it leads with are chosen by discriminative power against the other families
    under study, so 'the king is on e8' is dropped while 'the queen is on a5' survives.
    """
    items = []
    reach = curriculum.load_reach(db, source)
    features = family_feature_table(db, library['structures'], source)
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
        ranked, scored = distinctive_features(features, structure_id)
        signature = _structure_signature(db, structure_id, source)
        distinctive_score = sum(row['score'] for row in ranked)
        items.append({
            'item_id': f'orientation:{structure_id}',
            'kind': 'orientation', 'level': 'structure',
            'title': f'structure {structure_id[:8]}',
            'prescribes': None,
            'destinations': destinations(db, structure_id, source),
            'distinctive': [
                {'fact': f"{row['piece']} on {row['square']}",
                 'present_in_family': row['p_family'],
                 'present_elsewhere': row['p_background'],
                 'lift': row['lift'], 'score': row['score']}
                for row in ranked],
            'distinctive_information_score': distinctive_score,
            'suppressed_facts': [f"{row['piece']} on {row['square']}"
                                 for row in scored if row.get('suppressed')][:12],
            'recognition': {
                # raw occupancy stays available as provenance, no longer the headline
                'look_for': signature,
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
        required_rows = [_component_detail(component) for component in schema['required']]
        buckets = classify_required(required_rows)
        actionable = buckets['learner_action']
        item_id = f"schema:{schema['schema_id']}"
        items.append({
            'item_id': item_id,
            'kind': 'schema', 'level': 'schema',
            'title': ' + '.join(row['text'] for row in actionable)
                     or ' + '.join(row['text'] for row in required_rows),
            'prescribes': None,
            # a schema is only teachable if at least one of its transformations is
            # something the learner can do; effects are expectations, not lessons
            'prescriptive': len(actionable) >= ACTIONABILITY_FLOOR,
            'actionability': {name: [row['text'] for row in rows]
                              for name, rows in buckets.items()},
            'transformations': {
                'required': actionable,
                'optional': [_component_detail(component) for component in schema['optional']],
                'residual': [_component_detail(component)
                             for component in schema['residual_components'][:6]],
                'expected_consequence': buckets['outcome_effect'],
                'observable_state': buckets['observable_state'],
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
            'coherence': coherence(required_rows, schema['optional'],
                                   schema['residual_components'], None, covered, boards, []),
            'covered_plans': schema['covered_plans'],
            'positions': schema['positions'], 'flow_mass': schema['flow_mass'],
            'boards': len(boards), 'boards_map': {key.hex(): value
                                                  for key, value in boards.items()},
            'burden': float(max(len(actionable), 1)),
            'marginal_value': None,
            'marginal_burden': None,
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
    """Deviations that earn curriculum attention, grouped under what they modify.

    A deviation is kept only when it materially changes the recommended action — the
    move strong players actually choose is not the one the schema prescribes — and it
    is both dominant at that board and reached by real games. Equivalent responses
    collapse into one entry. Where a deviation and a decision would give incompatible
    instructions about the same board, the conflict is resolved here rather than shown.
    """
    items = []
    for parent in selected:
        if parent['kind'] not in ('schema', 'decision'):
            continue
        boards = parent.get('boards_map') or {}
        parent_answers = {answer for effect in boards.values()
                          for answer in (effect.get('answers') or [])}
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
            if share < MATERIAL_DEVIATION_MIN_SHARE:
                continue                                  # not what people actually play
            if board['reach'] < MATERIAL_DEVIATION_MIN_REACH:
                continue                                  # nobody reaches it
            if move in parent_answers:
                continue                                  # same action: not a deviation
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
                    'differs_from_prescription': sorted(parent_answers)[:4],
                },
                'condition': {'board': board['fen'],
                              'state': 'the remaining schema transformations are as shown '
                                       'on this board'},
                'burden': 1.0, 'marginal_value': None,
                'provenance': provenance(
                    family=parent.get('provenance', {}).get('family'),
                    schema_id=parent.get('provenance', {}).get('schema_id'),
                    boards=[position_key],
                    statistics={'observed_share': share,
                                'schema_explained': effect['explained'],
                                'reach': board['reach']},
                    sources=['move_source', 'position', 'eval']),
            })
    grouped = _group_deviations(items)
    return _resolve_conflicts(grouped, selected)


def _group_deviations(items):
    """Collapse deviations that ask for the same response under the same parent."""
    grouped, order = {}, []
    for item in items:
        signature = (item['modifies'], tuple(item['prescribes']['moves']))
        if signature not in grouped:
            grouped[signature] = dict(item, boards=[], occurrences=0)
            order.append(signature)
        entry = grouped[signature]
        entry['boards'].append(item['board'])
        entry['occurrences'] += 1
    out = []
    for signature in order:
        entry = grouped[signature]
        if entry['occurrences'] > 1:
            entry['title'] = (f"{entry['prescribes']['moves'][0]} instead, on "
                              f"{entry['occurrences']} equivalent boards")
        out.append(entry)
    return out


def _resolve_conflicts(items, selected):
    """Never hand the learner two incompatible instructions without a condition.

    A deviation whose board is the same board as the decision it hangs off would
    contradict it outright, so it is demoted to a note that states the condition
    explicitly. A deviation on any other board already carries its own condition and
    can remain an instruction.
    """
    by_id = {item['item_id']: item for item in selected}
    resolved = []
    for item in items:
        parent = by_id.get(item['modifies'])
        if parent is not None and parent['kind'] == 'decision':
            same_board = (item['board']['position_key']
                          == parent['board']['position_key'])
            if same_board:
                item = dict(item, kind='note', prescriptive_note=True,
                            title=(f"note: the engine prefers "
                                   f"{' '.join(parent['prescribes']['moves'])} here, while "
                                   f"strong play most often chooses "
                                   f"{item['prescribes']['moves'][0]}"),
                            condition={'explicit': 'same board as the decision above',
                                       'board': item['board']['fen']})
        resolved.append(item)
    return resolved


PRECEDENCE = {'decision': 3, 'schema': 2, 'exception': 1}


def resolve_board_conflicts(selected, exceptions):
    """Never leave two incompatible instructions about the same board unresolved.

    Any board where more than one selected item prescribes, and the prescriptions do not
    agree, is settled by an explicit precedence rule rather than shown as competing
    advice: the most specific item wins, and every other statement on that board is
    demoted to a note whose condition is the board itself. A schema cannot be dropped for
    one board, so it records which boards a decision supersedes.
    """
    by_board = defaultdict(list)
    for item in selected:
        if item['kind'] == 'decision':
            for move in item['prescribes']['moves']:
                by_board[item['board']['position_key']].append(('decision', item, move))
        elif item['kind'] == 'schema':
            for position_key, effect in (item.get('boards_map') or {}).items():
                for move in effect.get('answers') or []:
                    by_board[position_key].append(('schema', item, move))
    for item in exceptions:
        if item['kind'] == 'exception':
            # a grouped deviation covers several boards; every one of them counts, not
            # just the representative board kept for display
            for board_entry in (item.get('boards') or [item['board']]):
                by_board[board_entry['position_key']].append(
                    ('exception', item, item['prescribes']['moves'][0]))
    notes, demoted = [], set()
    for position_key, entries in by_board.items():
        moves = {move for _kind, _item, move in entries}
        if len(entries) < 2 or len(moves) <= 1:
            continue
        winner = max(entries, key=lambda entry: PRECEDENCE[entry[0]])
        # a deviation disagreeing with its parent schema is the normal subordinate case:
        # that is what makes it a deviation, and it already carries the board condition.
        # Only a decision — the most specific kind of instruction — genuinely overrides
        # what is prescribed on the same board.
        if winner[0] != 'decision':
            continue
        for kind, item, move in entries:
            if item is winner[1]:
                continue
            if kind == 'exception':
                # demote the original object, and remember which object to drop: the
                # note is a new dict, so its id must not be used to filter the originals
                demoted.add(id(item))
                if id(item) not in {id(note) for note in notes}:
                    notes.append(dict(item, kind='note', prescriptive_note=True,
                                      title=(f"note: {move} is played here too, but "
                                             f"{winner[0]} {winner[2]} takes precedence"),
                                      condition={'explicit': 'the same board as the '
                                                             f'{winner[0]} above',
                                                 'board': item['board']['fen']}))
            else:
                superseded = item.setdefault('superseded_boards', [])
                if position_key not in superseded:
                    superseded.append(position_key)
    if demoted:
        exceptions = [item for item in exceptions if id(item) not in demoted] + notes
    return exceptions


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


def _item_components(item):
    """The reusable pieces of information an item introduces, for marginal costing."""
    if item['kind'] == 'schema':
        return {('component', tuple(row['component']))
                for row in item['transformations']['required']}
    if item['kind'] == 'decision':
        return {('decision', item['item_id'])}
    if item['kind'] == 'orientation':
        return {('orientation', item['provenance'].get('family'))}
    return {('exception', item['item_id'])}


def select(payload, library, evidence, budget, pool):
    """Greedy marginal selection under a budget of *marginal* burden units.

    Cost and value are both marginal with respect to what is already selected: a
    component already introduced by an earlier item is free the second time, and a
    board already handled as well by an earlier item yields no further value. A schema
    that is a strict subset of a selected one is recorded explicitly and keeps only the
    value its own applicability adds.
    """
    selected, spent = [], 0.0
    learned, state_loss, state_explained = set(), {}, {}
    marginal_log = []
    for item in [entry for entry in pool if entry['kind'] == 'orientation']:
        if spent + 1.0 > budget:
            break
        item['marginal_burden'] = 1.0
        item['marginal_value'] = None
        selected.append(item)
        learned |= _item_components(item)
        spent += 1.0
    candidates = [item for item in pool if item['kind'] != 'orientation']
    while candidates:
        best, best_score, best_payload = None, None, None
        for item in candidates:
            boards = _candidate_boards(item, evidence)
            if not boards:
                continue
            components = _item_components(item)
            marginal_burden = len(components - learned) or 1.0
            # a schema that is a special case of an already selected one earns nothing
            # for boards the general schema already handles: only its *novel*
            # applicability can carry value
            subset_of = _strict_subset_of(item, selected)
            if subset_of is not None:
                general = next(entry for entry in selected
                               if entry['item_id'] == subset_of)
                general_boards = set(general.get('boards_map') or {})
                boards = {position_key: effect for position_key, effect in boards.items()
                          if position_key not in general_boards}
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
                    gain += 0.25 * context['reach'] * (effect['explained'] - previous_explained)
            score = gain / marginal_burden if marginal_burden else gain
            if best_score is None or score > best_score:
                best, best_score, best_payload = item, score, (gain, boards, marginal_burden,
                                                                components)
        if best is None:
            break
        gain, boards, marginal_burden, components = best_payload
        if spent + marginal_burden > budget or gain <= 0:
            candidates.remove(best)
            if gain <= 0:
                break
            continue
        # strict-subset detection against what is already selected, reported not hidden
        best['subset_of'] = subset_of
        if subset_of is not None:
            best['retained_because'] = (f"special case of {subset_of}, kept for its novel "
                                        f"applicability on {len(boards)} further boards")
        best['marginal_burden'] = marginal_burden
        best['marginal_value'] = gain
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
        selected.append(best)
        learned |= components
        spent += marginal_burden
        marginal_log.append({'item_id': best['item_id'], 'kind': best['kind'],
                             'marginal_burden': marginal_burden, 'marginal_value': gain,
                             'value_per_burden': gain / marginal_burden,
                             'cumulative_burden': spent,
                             'subset_of': best.get('subset_of')})
        candidates.remove(best)
    report = budget_report(selected, evidence, state_loss, state_explained)
    report['burden'] = spent
    report['marginal_burden_total'] = spent
    return selected, report, marginal_log


BETTER_ON_SHARED_BOARDS = 0.002      # a subset must beat the general schema by this much


def prune_redundant(selected):
    """Drop a selected schema that teaches nothing its selected superset does not.

    Selection runs greedily, so a narrow schema can be chosen before the broader one
    that contains it arrives. Both are then in the curriculum — one a special case of
    the other. A subset is removed when the broader schema covers every board it
    covers, and kept only when it reaches boards the broader one does not, or is
    materially better on the boards they share. Either way the decision is recorded.
    """
    schemas = [item for item in selected if item['kind'] == 'schema']
    pruned, reasons = [], {}
    removed = set()
    for narrow in schemas:
        own = {tuple(row['component']) for row in narrow['transformations']['required']}
        if not own:
            continue
        narrow_boards = narrow.get('boards_map') or {}
        # every selected schema that contains this one, not just the first encountered:
        # stopping early left containments the selector never noticed
        supersets = [item for item in schemas
                     if item is not narrow
                     and own < {tuple(row['component'])
                                for row in item['transformations']['required']}
                     and item['item_id'] not in removed]
        if not supersets:
            continue
        redundant_via = None
        for broad in supersets:
            broad_boards = broad.get('boards_map') or {}
            if any(key not in broad_boards for key in narrow_boards):
                continue                          # this superset does not reach everywhere
            better = [key for key in narrow_boards
                      if key in broad_boards
                      and (broad_boards[key]['loss'] - narrow_boards[key]['loss'])
                      > BETTER_ON_SHARED_BOARDS]
            if not better:
                redundant_via = broad
                break
        if redundant_via is not None:
            removed.add(narrow['item_id'])
            pruned.append({'item_id': narrow['item_id'], 'title': narrow['title'],
                           'contained_in': redundant_via['item_id'],
                           'reason': 'a containing schema covers every board it does with no '
                                     'worse loss, so it teaches nothing extra'})
            continue
        cover = set()
        for broad in supersets:
            cover |= set((broad.get('boards_map') or {}).keys())
        novel = [key for key in narrow_boards if key not in cover]
        reasons[narrow['item_id']] = (
            f"contained in {len(supersets)} selected schema(s) but kept: {len(novel)} boards "
            f"no containing schema covers")
        narrow['contained_in'] = supersets[0]['item_id']
        narrow['novel_boards'] = len(novel)
    kept = []
    for item in selected:
        if item['item_id'] in removed:
            continue
        if item['item_id'] in reasons:
            item['retained_because'] = reasons[item['item_id']]
        if item.get('subset_of') and 'contained_in' not in item:
            item['contained_in'] = item['subset_of']
        if item['kind'] == 'schema' and item.get('contained_in') and not item.get(
                'retained_because'):
            # one narrow schema can sit inside several broader ones; the reason is about
            # the containment itself, so it is recorded once and applies to all of them
            item['retained_because'] = ('contained in a broader selected schema but kept: '
                                        'it reaches boards that schema does not')
        kept.append(item)
    return kept, pruned


def _strict_subset_of(item, selected):
    """Report when an item is a strict subset of another selected schema.

    Both surviving is allowed, but never silently: the caller records the containment
    so the audit can state why the smaller one is still there.
    """
    if item['kind'] != 'schema':
        return None
    own = {tuple(row['component']) for row in item['transformations']['required']}
    if not own:
        return None
    for other in selected:
        if other is item or other['kind'] != 'schema':
            continue
        theirs = {tuple(row['component']) for row in other['transformations']['required']}
        if own < theirs:
            return other['item_id']
    return None


def budget_report(selected, evidence, state_loss, state_explained):
    family_reach = sum(board['reach'] for board in evidence.values())
    denominator = sum(board['reach'] * board['loss_pop'] for board in evidence.values())
    value = sum(evidence[key]['reach'] * max(0.0, evidence[key]['loss_pop'] - loss)
                for key, loss in state_loss.items())
    explained = sum(evidence[key]['reach'] * state_explained.get(key, 0.0)
                    for key in state_explained)
    return {
        'items': len(selected),
        # the budget currency is *marginal* burden: components already introduced by an
        # earlier item are not charged again, so this can be well below the sum
        'burden': sum(item.get('marginal_burden') or item['burden'] for item in selected),
        'burden_if_charged_independently': sum(item['burden'] for item in selected),
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
    all_schemas = schema_course_items(payload, library, evidence)
    # a schema with no learner action cannot be taught: it is recorded as invalidated
    # and never competes for the budget, so its slot goes to the next best item
    invalid = [{key: item[key] for key in ('item_id', 'title', 'actionability',
                                           'burden', 'boards')} |
               {'reason': 'no learner_action among its required transformations'}
               for item in all_schemas if not item['prescriptive']]
    schemas = [item for item in all_schemas if item['prescriptive']]
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
        exceptions = resolve_board_conflicts(selected, exceptions)
        report['exceptions_available'] = len(exceptions)
        report['decisions_offered'] = len(decisions)
        selected, pruned = prune_redundant(selected)
        report['pruned_redundant'] = len(pruned)
        report['actionable_schema_fraction'] = (
            sum(1 for item in selected if item['kind'] == 'schema'
                and item.get('prescriptive'))
            / max(1, sum(1 for item in selected if item['kind'] == 'schema')))
        report['distinctive_information'] = sum(
            item.get('distinctive_information_score', 0.0) for item in selected
            if item['kind'] == 'orientation')
        courses[str(budget)] = {'budget': budget, 'items': selected, 'report': report,
                                'marginal': marginal, 'exceptions': exceptions,
                                'invalidated_schemas': invalid, 'pruned': pruned,
                                'schema_pool': {'offered': len(all_schemas),
                                                'prescriptive': len(schemas),
                                                'invalidated': len(invalid)},
                                'lines': {item['provenance'].get('family'):
                                          representative_line(db, item['provenance'].get('family'))
                                          for item in selected
                                          if item.get('provenance', {}).get('family')}}
    db.close()
    return {'budgets': list(budgets), 'courses': courses,
            'constants': {'DECISION_MIN_SHARE_OF_RESIDUAL': DECISION_MIN_SHARE_OF_RESIDUAL,
                          'DECISION_MIN_ABSOLUTE': DECISION_MIN_ABSOLUTE,
                          'DEVIATION_MAX_STRONG_SHARE': DEVIATION_MAX_STRONG_SHARE}}


def classify_required(required):
    """Split a schema's required transformations by actionability."""
    buckets = {'learner_action': [], 'observable_state': [], 'outcome_effect': []}
    for row in required:
        kind = row['component'][0] if row['component'] else 'observable_state'
        buckets[TRANSFORMATION_CLASS.get(kind, 'observable_state')].append(row)
    return buckets


def coherence(required, optional, residual, orderings, covered, boards, exceptions):
    """Measurable coherence diagnostics, reported separately and never summed.

    Nothing here is tuned: the dimensions are reported on their own so a reader can
    decide which schemas are worth learning from, rather than being handed a score.
    """
    orders = Counter()
    for plan in covered:
        orders[tuple(sorted(str(event) for event in plan['transformations']))] += 1
    total = sum(orders.values()) or 1
    entropy = -sum((count / total) * math.log2(count / total) for count in orders.values())
    flexibility = (statistics.fmean(plan['order_flexibility'] for plan in covered)
                   if covered else None)
    return {
        'required_count': len(required),
        'optional_count': len(optional),
        'residual_count': len(residual),
        'distinct_orderings': len(orders),
        'ordering_entropy_bits': entropy,
        'ordering_flexibility_mean': flexibility,
        'conditional_branches': len({(plan['structure_id'], plan['side']) for plan in covered})
                                + len(optional),
        'exception_burden': (len(exceptions) / len(boards)) if boards else None,
    }


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
