"""Prescriptive rules vs recognition knowledge.

A structural template must not earn expected-value credit merely because it helps
recognise a board. Only a rule that *implies an action* can be credited, and such a
rule must be derived from the strong/training data with an explicit applicability
condition and a taught answer set:

    IF structural_family = S [AND condition C] -> answer set A

Both kinds are kept, separately typed, and they carry different burdens:

* `recognition_only` — the family helps orientation. Cost is charged (it is still
  something to learn), EV contribution is exactly zero, by construction rather than
  by measurement.
* `prescriptive` — the rule names one or more moves to play. This is the only kind
  that may enter the EV calculation.

Every rule records: support (flow mass share where the answer is what strong play
actually chose), coverage (flow mass), exceptions (boards where strong play chose
something outside the answer set), the derivation (source, share floor, game floor,
aggregation), the applicability condition, and competing answers.

Nothing here calls an engine: the derivation uses the strong-play move distributions
only, which is what keeps the later EV comparison fair between templates and exact
decision items.
"""
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import curriculum, db as studydb, templates  # noqa: E402

DEFAULTS = {
    'answer_share': 0.35,      # a board's answer must hold this share of strong play
    'answer_cap': 2,           # a rule teaches at most this many moves
    'support_floor': 0.60,     # the answer set must hold on this share of family mass
    'min_games': 20,           # strong-play sample floor at a board
    'min_group_share': 0.10,   # a conditioned group must carry this share of the family
    'min_answers': 1,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS template_rule (
    rule_id        TEXT PRIMARY KEY,
    structure_id   TEXT NOT NULL,
    source         TEXT NOT NULL,
    kind           TEXT NOT NULL,        -- prescriptive | recognition_only
    condition_json TEXT NOT NULL,        -- explicit applicability condition
    answer_json    TEXT NOT NULL,        -- taught answer set A ([] for recognition only)
    derivation_json TEXT NOT NULL,       -- how A was derived, from which data
    support        REAL,                 -- flow mass share where strong play plays A
    coverage       REAL NOT NULL,        -- flow mass the rule applies to
    applicability  REAL NOT NULL,        -- share of the family the condition selects
    exceptions_json TEXT NOT NULL,       -- boards where strong play leaves A
    n_exceptions   INTEGER NOT NULL,
    competing_json TEXT NOT NULL,        -- moves competing with A, with their mass
    boards         INTEGER NOT NULL,
    reason_text    TEXT NOT NULL         -- why not prescriptive, when it is not
);
"""


def ensure_schema(db):
    db.executescript(SCHEMA)
    db.commit()


def _rule_id(structure_id, condition):
    payload = json.dumps([structure_id, condition], sort_keys=True)
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def board_answers(dist, answer_share=None, answer_cap=None, min_games=20, games=None):
    """The moves strong play chose at one board, filtered by evidence."""
    answer_share = answer_share if answer_share is not None else DEFAULTS['answer_share']
    answer_cap = answer_cap if answer_cap is not None else DEFAULTS['answer_cap']
    usable = [(uci, share) for uci, share in dist.items()
              if not games or games.get(uci, 0) >= min_games]
    if not usable:
        return [], None
    ordered = sorted(usable, key=lambda pair: -pair[1])
    total = sum(dist.values()) or 1.0
    answers = []
    for uci, share in ordered:
        if share / total < answer_share or len(answers) >= answer_cap:
            break
        answers.append(uci)
    if not answers:
        return [], ordered[0][0]        # contested board: no answer, top move recorded
    return answers, ordered[0][0]


def _family_boards(db, structure_id, source='local2200'):
    reach = curriculum.load_reach(db, source)
    roles = {row['position_key']: row['role'] for row in db.execute(
        'SELECT position_key, role FROM position WHERE structure_id=?', (structure_id,))}
    boards = []
    for key in curriculum.family_positions(db, structure_id):
        dist = curriculum.load_distributions(db, key, source)
        if not dist:
            continue
        games = {row['uci']: row['games'] for row in db.execute(
            'SELECT uci, games FROM move_source WHERE position_key=? AND source=?',
            (key, source))}
        boards.append({'key': key, 'weight': reach.get(key, {}).get('reach', 0.0),
                       'dist': dist, 'games': games, 'role': roles.get(key)})
    return boards


def _evaluate_condition(condition, board, meta):
    """Explicit applicability test.

    `role` is handled first and by default: a taught answer set is only meaningful
    for the side to move, so a rule never mixes white-to-move and black-to-move
    answers into one instruction. Feature conditions come from the split test.
    """
    if condition and condition.get('feature') == 'role':
        return board.get('role') == condition.get('value')
    if condition:
        feature, value = condition.get('feature'), condition.get('value')
        if meta.get(feature) != value:
            return False
    return True


def derive_rule(db, structure_id, source='local2200', condition=None, meta_of=None,
                settings=None, role=None):
    """Derive one rule for a family (optionally restricted to a side and a condition)."""
    settings = {**DEFAULTS, **(settings or {})}
    boards = _family_boards(db, structure_id, source)
    if role is not None:
        boards = [board for board in boards if board['role'] == role]
    selected = [board for board in boards
                if _evaluate_condition(condition, board, (meta_of or {}).get(board['key'], {}))]
    total_mass = sum(board['weight'] for board in boards) or 1.0
    coverage = sum(board['weight'] for board in selected)
    answer_mass = defaultdict(float)
    exceptions, competing = [], defaultdict(float)
    for board in selected:
        answers, top = board_answers(board['dist'], settings['answer_share'],
                                     settings['answer_cap'], settings['min_games'],
                                     board['games'])
        for uci in answers:
            answer_mass[uci] += board['weight']
        if top is not None and top not in answers:
            competing[top] += board['weight']
    ordered = sorted(answer_mass.items(), key=lambda pair: -pair[1])
    answer_set, covered = [], 0.0
    for uci, mass in ordered:
        if len(answer_set) >= settings['answer_cap'] or covered >= settings['support_floor']:
            break
        answer_set.append(uci)
        covered += mass
    support = (covered / coverage) if coverage else None
    # Strict support: the share of mass where the board's OWN answer set is exactly A.
    # Without this, a family split 50/50 between two answers passes the floor as soon
    # as the cap allows two moves, even though no board ever teaches that pair. A
    # multi-move rule must therefore be earned strictly; a single-move rule only needs
    # the mass share, since its answer is the top choice everywhere it is not beaten.
    strict = 0.0
    for board in selected:
        answers, _top = board_answers(board['dist'], settings['answer_share'],
                                      settings['answer_cap'], settings['min_games'],
                                      board['games'])
        if answers == answer_set:
            strict += board['weight']
    strict_support = (strict / coverage) if coverage else None
    for board in selected:
        answers, top = board_answers(board['dist'], settings['answer_share'],
                                     settings['answer_cap'], settings['min_games'],
                                     board['games'])
        if top is not None and top not in answer_set:
            exceptions.append({'position': board['key'].hex(), 'weight': board['weight'],
                               'strong_top': top})
    exceptions.sort(key=lambda item: -item['weight'])
    required = (support if len(answer_set) == 1 else strict_support)
    prescriptive = (bool(answer_set)
                    and required is not None
                    and required >= settings['support_floor']
                    and len(answer_set) >= settings['min_answers']
                    and coverage > 0)
    reason = ''
    if not prescriptive:
        if not answer_set:
            reason = 'no board in scope has a strong-play answer above the share floor'
        elif required is None or required < settings['support_floor']:
            reason = (f'no answer set holds on the required share of the family '
                      f'(best {required if required is None else round(required, 2)} '
                      f'< {settings["support_floor"]}'
                      + (', multi-move set must be matched strictly' if len(answer_set) > 1 else '')
                      + ')')
        elif coverage <= 0:
            reason = 'condition selects no boards with strong-play data'
    rule = {
        'rule_id': _rule_id(structure_id, {'condition': condition, 'role': role}),
        'structure_id': structure_id, 'source': source,
        'kind': 'prescriptive' if prescriptive else 'recognition_only',
        'condition': (condition or {'feature': 'structure_id', 'value': structure_id})
        | ({'role': role} if role else {}),
        'answers': answer_set if prescriptive else [],
        'derivation': {'source': source, 'settings': settings, 'role': role,
                       'method': 'flow-weighted strong-play top moves per board, '
                                 'answers taken up to the share floor, per side to move'},
        'support': support, 'strict_support': strict_support, 'coverage': coverage,
        'applicability': (coverage / total_mass) if total_mass else 0.0,
        'exceptions': exceptions[:20], 'n_exceptions': len(exceptions),
        'competing': dict(sorted(competing.items(), key=lambda pair: -pair[1])[:5]),
        'boards': len(selected),
        'reason_text': reason,
    }
    return rule


def derive_family_rules(db, structure_id, source='local2200', settings=None):
    """Derive rules per side to move, then per accepted split within each side.

    A rule is only an instruction if it is scoped to the side to move, so every
    family is partitioned by `role` first. Order: role-wide rule, then, if that is
    not prescriptive, conditioned rules inside the role using accepted behaviour
    splits from the split test.
    """
    settings = {**DEFAULTS, **(settings or {})}
    boards = _family_boards(db, structure_id, source)
    roles = sorted({board['role'] for board in boards if board['role']})
    rules = []
    for role in roles:
        role_condition = {'feature': 'role', 'value': role}
        role_rule = derive_rule(db, structure_id, source, role_condition, None, settings,
                                role=role)
        rules.append(role_rule)
        if role_rule['kind'] == 'prescriptive':
            continue
        # conditioned fallback: only accepted behaviour splits, and only inside role
        splits = list(db.execute('''SELECT DISTINCT feature FROM template_split
                                    WHERE structure_id=? AND source=? AND decision='split'
                                    AND target='near' ''', (structure_id, source)))
        if not splits:
            continue
        meta_of = _feature_meta(db, structure_id)
        for (feature,) in splits:
            values = sorted({meta.get(feature) for meta in meta_of.values()
                             if meta.get(feature) is not None})
            for value in values:
                condition = {'feature': feature, 'value': value, 'role': role}
                rule = derive_rule(db, structure_id, source, condition, meta_of, settings,
                                   role=role)
                if rule['kind'] == 'prescriptive' and rule['applicability'] >= settings['min_group_share']:
                    rules.append(rule)
    return rules


def _feature_meta(db, structure_id):
    """Feature values per board, using the same functions as the split test."""
    from study import splitting
    samples, _positions, _moves = splitting.family_samples(db, structure_id)
    pieces_of = lambda key: next((meta['_pieces'] for k, _w, meta in samples if k == key), None)
    functions = splitting.feature_functions(pieces_of)
    meta_of = {}
    for key, _weight, meta in samples:
        meta = dict(meta)
        meta['_open_files'] = {file for file in range(8)
                               if not any(meta['_pieces'].get(name, 0) & splitting._file_mask(file)
                                          for name in ('wp', 'bp'))}
        meta['_key'] = key
        values = {}
        for name, function in functions.items():
            try:
                values[name] = function(meta)
            except Exception:
                values[name] = None
        meta_of[key] = values
    return meta_of


def derive_all(db, source='local2200', settings=None):
    rules = []
    for row in templates.candidate_pool(db, source):
        rules.extend(derive_family_rules(db, row['structure_id'], source, settings))
    return rules


def persist(db, rules):
    ensure_schema(db)
    for rule in rules:
        db.execute('''INSERT OR REPLACE INTO template_rule(
            rule_id, structure_id, source, kind, condition_json, answer_json,
            derivation_json, support, coverage, applicability, exceptions_json,
            n_exceptions, competing_json, boards, reason_text)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (rule['rule_id'], rule['structure_id'], rule['source'], rule['kind'],
                    json.dumps(rule['condition']), json.dumps(rule['answers']),
                    json.dumps(rule['derivation']), rule['support'], rule['coverage'],
                    rule['applicability'], json.dumps(rule['exceptions']),
                    rule['n_exceptions'], json.dumps(rule['competing']), rule['boards'],
                    rule['reason_text']))
    db.commit()
    return db.execute('SELECT COUNT(*) FROM template_rule').fetchone()[0]


def rule_scope(db, rule, source='local2200'):
    """The exact positions a rule applies to: family, side to move, and condition."""
    condition = rule['condition']
    role = condition.get('role')
    meta_of = _feature_meta(db, rule['structure_id']) if (
        condition.get('feature') not in (None, 'structure_id', 'role')) else {}
    keys = []
    for board in _family_boards(db, rule['structure_id'], source):
        if role and board['role'] != role:
            continue
        if not _evaluate_condition(condition, board, meta_of.get(board['key'], {})):
            continue
        keys.append(board['key'])
    return keys


def as_items(db, rules, source='local2200'):
    """Turn rules into curriculum items so the EV/frontier layers can consume them.

    Recognition-only rules become orientation items with no answers: they cost burden
    and can never earn value. Prescriptive rules become template items whose answer
    set is exactly the rule's A and whose scope is exactly the rule's applicability
    scope.
    """
    items = []
    for rule in rules:
        keys = rule_scope(db, rule, source)
        if not keys:
            continue
        condition = rule['condition']
        feature = condition.get('feature')
        label_kind = 'prescriptive rule' if rule['kind'] == 'prescriptive' else 'recognition'
        item = {
            'type': 'template' if rule['kind'] == 'prescriptive' else 'orientation',
            'label': f"{label_kind} {rule['structure_id'][:8]}",
            'scope_kind': 'structure', 'scope_id': f"{rule['structure_id']}:{rule['rule_id']}",
            'keys': keys, 'positions': len(keys),
            'coverage': rule['coverage'], 'answers': rule['answers'],
            'exact_moves': len(rule['answers']), 'boards': 1,
            'flexible_slots': 0,
            'conditions': 2 if feature not in (None, 'structure_id', 'role') else 1,
            'n_exceptions': rule['n_exceptions'], 'move_orders': 0,
            'coverage_source': source, 'rule_id': rule['rule_id'],
            'kind': rule['kind'], 'condition': condition,
        }
        item['complexity'] = curriculum.complexity(item)
        item['item_id'] = curriculum._item_id(item['type'], item['scope_id'])
        items.append(item)
    return items


def estimate_rule_ev(db, rules, source='local2200', population='lichess'):
    """Expected-value credit for rules. Prescriptive rules only.

    Recognition-only rules are returned with `ev_per_board: None` and an explicit
    reason: they are charged learning burden but earn no expected value, which is
    what makes the template-versus-exact-item comparison fair.
    """
    from study import campaign
    evals = curriculum.load_evals(db)
    out = {}
    for rule in rules:
        scope = rule_scope(db, rule, source)
        if rule['kind'] != 'prescriptive' or not rule['answers']:
            out[rule['rule_id']] = {
                'kind': rule['kind'], 'structure_id': rule['structure_id'],
                'boards': len(scope), 'boards_evaluated': 0, 'ev_per_board': None,
                'reason': 'recognition-only: not an action rule, so no EV credit',
            }
            continue
        weights, gains, missing = [], [], 0
        for key in scope:
            _role, fen = curriculum.position_role(db, key)
            scores = evals.get(fen)
            dist_pop = curriculum.load_distributions(db, key, population)
            dist_strong = curriculum.load_distributions(db, key, source)
            if not scores or not dist_pop or not dist_strong:
                missing += 1
                continue
            reach = curriculum.load_reach(db, source).get(key, {}).get('reach', 0.0)
            best = max(entry['ep_wp'] for entry in scores.values())
            value = campaign.value_of_answers(db, scores, dist_strong, dist_pop, best)
            if value['gain'] is None:
                missing += 1
                continue
            weights.append(reach or 1e-9)
            gains.append((reach or 1e-9) * value['gain'])
        out[rule['rule_id']] = {
            'kind': rule['kind'], 'structure_id': rule['structure_id'],
            'answers': rule['answers'], 'boards': len(scope),
            'boards_evaluated': len(weights), 'boards_missing_evaluations': missing,
            'ev_per_board': (sum(gains) / sum(weights)) if weights else None,
            'reason': ('' if weights else 'no evaluated board in scope yet'),
        }
    return out
