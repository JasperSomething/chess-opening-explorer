"""Curriculum items, complexity proxies and the overlap-aware value frontier.

Implements sections 1, 2 and 5 of STUDY-CURRICULUM.md against current data.

Two value axes, never summed:

* expected-points axis (`benefit_ep`) — requires engine evaluations, so it is
  always reported with the fraction of the item's mass that has evaluations.
* recognition axis (`coverage_flow`) — game mass whose flow reaches the item's
  scope, available for every position and engine-free.

Overlap rule: at a position covered by several items the learner uses the best
available answer, so retained value is a maximum over items, never a sum.

Two accounting rules that the first prototype run got wrong and that are now
enforced:

* coverage is a game mass, so it is computed by absorbing propagation on the
  position graph (a game is counted once, at its first covered position) — not by
  summing per-position reach, which counts a game once per ply and can exceed 1.
* items must not be selected and valued on the same information. `criterion` in
  `decision_items` controls this and each item carries a `circular` flag.
"""
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import db as studydb, metrics, structure_flow  # noqa: E402

WEIGHTS = {'moves': 1.0, 'slots': 1.0, 'conditions': 1.0, 'exceptions': 1.0, 'boards': 1.0}

SCHEMA = """
CREATE TABLE IF NOT EXISTS curriculum_item (
    item_id          TEXT PRIMARY KEY,
    run_id           INTEGER NOT NULL,
    type             TEXT NOT NULL,
    label            TEXT NOT NULL,
    scope_kind       TEXT NOT NULL,
    scope_id         TEXT NOT NULL,
    scope_json       TEXT NOT NULL,
    positions        INTEGER NOT NULL,
    coverage_flow    REAL NOT NULL,
    coverage_games   REAL NOT NULL,
    coverage_source  TEXT NOT NULL,
    prereq_json      TEXT NOT NULL,
    benefit_ep       REAL,
    benefit_basis    TEXT,
    benefit_sample   INTEGER,
    benefit_flags    TEXT NOT NULL,
    complexity_json  TEXT NOT NULL,
    exception_json   TEXT NOT NULL,
    n_exceptions     INTEGER NOT NULL
);
"""


def ensure_schema(db):
    db.executescript(SCHEMA)
    db.commit()


# --------------------------------------------------------------------- inputs
def load_evals(db):
    """{fen: {uci: {'ep_wp','ep_cp','mate'}}} from the cached evaluations."""
    best = {}
    for row in db.execute('SELECT fen, multipv, source, depth, pvs_json FROM eval'):
        rank = (0 if row['source'] == 'lichess_cloud' else 1, -row['depth'], row['multipv'])
        current = best.get(row['fen'])
        if current and current[0] <= rank:
            continue
        scores = {}
        for pv in json.loads(row['pvs_json']):
            ep = metrics.ep_from_score(cp=pv.get('cp'), mate=pv.get('mate'), wdl=pv.get('wdl'))
            if ep is not None:
                scores[pv['uci']] = {'ep_wp': ep, 'ep_cp': ep, 'mate': pv.get('mate')}
        best[row['fen']] = (rank, scores)
    return {fen: scores for fen, (_rank, scores) in best.items()}


def load_distributions(db, position_key, source):
    return {row['uci']: row['share'] for row in db.execute(
        'SELECT uci, share FROM move_source WHERE position_key=? AND source=? AND games >= 1',
        (position_key, source))}


def load_reach(db, source='local2200'):
    reach = {}
    for row in db.execute('''SELECT position_key, reach_flow, enter_mass FROM position_flow
                             WHERE source=?''', (source,)):
        reach[row['position_key']] = {'reach': row['reach_flow'] or 0.0,
                                      'enter': row['enter_mass'] or 0.0}
    return reach


def family_positions(db, structure_id):
    return [row[0] for row in db.execute(
        'SELECT position_key FROM position WHERE structure_id=?', (structure_id,))]


def position_role(db, position_key):
    row = db.execute('SELECT role, fen FROM position WHERE position_key=?', (position_key,)).fetchone()
    return (row['role'], row['fen']) if row else (None, None)


def position_union_coverage(db, keys, source='local2200', positions=None, moves=None, entry=None):
    """Game mass that visits at least one position in `keys` (each game counted once)."""
    keys = set(keys)
    if positions is None or moves is None:
        positions, moves = structure_flow.load_graph_inputs(db)
    if entry is None:
        entry = db.execute('SELECT position_key FROM position WHERE is_entry=1').fetchone()[0]
    if entry not in positions:
        return 0.0
    mass_at = defaultdict(float)
    mass_at[entry] = 1.0
    covered = 0.0
    for key in sorted(positions, key=lambda k: (positions[k]['ply'], k)):
        mass = mass_at.pop(key, 0.0)
        if mass <= 0:
            continue
        if key in keys:
            covered += mass
            continue
        outgoing = moves.get(key, [])
        total = sum(games for _c, _u, games in outgoing)
        if not total:
            continue
        for child, _uci, games in outgoing:
            if child in positions:
                mass_at[child] += mass * games / total
    return covered


# ------------------------------------------------------------------ item build
def _item_id(kind, scope_id):
    return hashlib.sha1(f'{kind}:{scope_id}'.encode()).hexdigest()[:16]


def complexity(item):
    """Interpretable proxies; the weighted total is reported beside its inputs."""
    positions = item['positions']
    proxies = {
        'exact_moves': item.get('exact_moves', 0),
        'boards': item.get('boards', 0),
        'flexible_slots': item.get('flexible_slots', 0),
        'conditions': item.get('conditions', 1),
        'exceptions': item.get('n_exceptions', 0),
        'positions': positions,
        'move_orders': item.get('move_orders', 0),
    }
    cost = (WEIGHTS['moves'] * proxies['exact_moves']
            + WEIGHTS['slots'] * proxies['flexible_slots']
            + WEIGHTS['conditions'] * max(0, proxies['conditions'] - 1)
            + WEIGHTS['exceptions'] * proxies['exceptions']
            + WEIGHTS['boards'] * proxies['boards'])
    proxies['cost'] = cost
    proxies['compression_ratio'] = positions / max(cost, 1.0)
    proxies['weights'] = dict(WEIGHTS)
    return proxies


def _answers_from_distribution(dist, min_share=0.25, cap=3, min_games=0, games=None):
    ordered = [(u, s) for u, s in sorted(dist.items(), key=lambda kv: -kv[1])
               if not games or games.get(u, 0) >= min_games]
    if not ordered:
        return []
    total = sum(dist.values()) or 1.0
    answers = []
    for uci, share in ordered:
        if share / total < min_share or len(answers) >= cap:
            break
        answers.append(uci)
    return answers or [ordered[0][0]]


def orientation_items(db, source='local2200', min_mass=0.02, graph=None):
    """Corridor knowledge: high flow, undeveloped. Prescribes nothing."""
    positions, moves = graph or (None, None)
    items = []
    for row in db.execute('''SELECT * FROM structure_maturity WHERE source=?
                             AND entry_mass >= ? AND development_level < 0.35
                             ORDER BY entry_mass DESC''', (source, min_mass)):
        keys = family_positions(db, row['structure_id'])
        coverage = position_union_coverage(db, keys, source, positions, moves)
        items.append({
            'type': 'orientation', 'label': f"corridor {row['structure_id'][:8]}",
            'scope_kind': 'structure', 'scope_id': row['structure_id'],
            'keys': keys, 'positions': len(keys), 'coverage': coverage,
            'answers': [], 'exact_moves': 0, 'boards': 1,
            'flexible_slots': 0, 'conditions': 1, 'move_orders': 0,
            'coverage_source': source,
        })
    return items


def template_items(db, source='local2200', graph=None):
    """Mature templates: the taught answers are the strong exit moves."""
    from study import templates as templates_module
    reach = load_reach(db, source)
    positions, moves = graph or (None, None)
    items = []
    for row in templates_module.candidate_pool(db, source):
        keys = family_positions(db, row['structure_id'])
        coverage = position_union_coverage(db, keys, source, positions, moves)
        exits = defaultdict(float)
        for key in keys:
            share_reach = reach.get(key, {}).get('reach', 0.0)
            for uci, share in load_distributions(db, key, source).items():
                exits[uci] += share * share_reach
        answers = _answers_from_distribution(exits, min_share=0.20, cap=3) if exits else []
        descriptor = templates_module.descriptors(db, row['structure_id'], source)
        items.append({
            'type': 'template', 'label': f"template {row['structure_id'][:8]}",
            'scope_kind': 'structure', 'scope_id': row['structure_id'],
            'keys': keys, 'positions': len(keys), 'coverage': coverage,
            'answers': answers, 'exact_moves': len(answers), 'boards': 1,
            'flexible_slots': len(descriptor['flexible_slots']) if descriptor else 0,
            'conditions': 1, 'move_orders': row.get('n_edges_in') or 0,
            'coverage_source': source,
        })
    return items


def decision_items(db, source='local2200', run_id=6, top=25, criterion='metric'):
    """Single positions where the move choice has measurable value.

    criterion='metric' ranks by the stored study priority, which was computed from
    both move distributions. Using that ranking and then valuing the items on the
    Lichess population is circular; every item built this way is flagged.

    criterion='expert' ranks using the strong-play source alone (reach in the
    expert flow times expert move concentration, with an expert-game floor), so
    the ordinary population stays untouched for evaluation.
    """
    reach = load_reach(db, source)
    items = []
    if criterion == 'expert':
        rows = []
        for row in db.execute('''SELECT p.position_key, p.role, pf.reach_flow
                                 FROM position p JOIN position_flow pf
                                   ON pf.position_key = p.position_key AND pf.source=?
                                 WHERE pf.reach_flow IS NOT NULL
                                 ORDER BY pf.reach_flow DESC LIMIT 400''', (source,)):
            key = row['position_key']
            dist = load_distributions(db, key, source)
            if not dist:
                continue
            totals = db.execute('''SELECT SUM(games) FROM move_source
                                   WHERE position_key=? AND source=?''', (key, source)).fetchone()
            if not totals or not totals[0] or totals[0] < 30:
                continue
            rows.append({'position_key': key, 'role': row['role'],
                         'study_priority_wp': (row['reach_flow'] or 0.0) * max(dist.values()),
                         'reach_prob': row['reach_flow'], 'confidence': 'expert_only',
                         'flags': 'selected from the expert source only'})
        rows.sort(key=lambda r: -r['study_priority_wp'])
        rows = rows[:top]
    else:
        rows = [dict(r) for r in db.execute(
            '''SELECT position_key, study_priority_wp, reach_prob, confidence, flags
               FROM metric WHERE run_id=? AND study_priority_wp IS NOT NULL
               ORDER BY study_priority_wp DESC LIMIT ?''', (run_id, top))]
    for row in rows:
        key = row['position_key']
        dist = load_distributions(db, key, source)
        answers = _answers_from_distribution(dist, min_share=0.5, cap=2)
        role, _fen = position_role(db, key)
        items.append({
            'type': 'decision', 'label': f"decision {key.hex()[:12]}",
            'selection_criterion': criterion, 'circular': criterion != 'expert',
            'scope_kind': 'position', 'scope_id': key.hex(),
            'keys': [key], 'positions': 1,
            'coverage': reach.get(key, {}).get('reach', row.get('reach_prob') or 0.0),
            'answers': answers, 'exact_moves': len(answers), 'boards': 1,
            'flexible_slots': 0, 'conditions': 1, 'move_orders': 0,
            'coverage_source': source, 'priority': row.get('study_priority_wp'),
            'role': role, 'flags': row.get('flags'),
        })
    return items


def exception_items(db, source='local2200', run_id=6, top=20):
    """Opponent deviations that deserve a specific answer.

    Scope is the position AFTER the deviation, not the position before it: the
    response is only playable once the opponent has committed. Scoping it at the
    parent would credit an answer that is not legal there, which is what the first
    version did (and produced exactly zero value for every deviation).
    """
    reach = load_reach(db, source)
    items = []
    for row in db.execute('''SELECT position_key, uci, san, prob_used, concession_wp, class,
                                    best_response_uci, deviation_priority
                             FROM deviation WHERE run_id=? AND class IN ('inaccurate','punishable')
                             ORDER BY deviation_priority DESC LIMIT ?''', (run_id, top)):
        key = row['position_key']
        child = db.execute('''SELECT child_key FROM provenance
                              WHERE parent_key=? AND uci=? LIMIT 1''',
                           (key, row['uci'])).fetchone()
        child_key = child[0] if child else None
        if child_key is not None:
            key = child_key
        items.append({
            'type': 'exception', 'label': f"deviation {key.hex()[:12]} {row['san']}",
            'scope_kind': 'deviation', 'scope_id': f"{key.hex()}:{row['uci']}",
            'keys': [key], 'positions': 1,
            'coverage': (reach.get(key, {}).get('reach', 0.0) * (row['prob_used'] or 0.0)),
            'answers': [row['best_response_uci']] if row['best_response_uci'] else [],
            'exact_moves': 1 if row['best_response_uci'] else 0, 'boards': 0,
            'flexible_slots': 0, 'conditions': 2,
            'n_exceptions': 1 if row['class'] == 'punishable' else 0,
            'move_orders': 0, 'coverage_source': source,
            'deviation_class': row['class'],
        })
    return items


def build_items(db, source='local2200', run_id=6, criterion='metric'):
    graph = structure_flow.load_graph_inputs(db)
    items = (orientation_items(db, source, graph=graph)
             + template_items(db, source, graph=graph)
             + decision_items(db, source, run_id, criterion=criterion)
             + exception_items(db, source, run_id))
    for item in items:
        item['complexity'] = complexity(item)
        item['item_id'] = _item_id(item['type'], item['scope_id'])
    return items


# ------------------------------------------------------------------ valuation
def position_values(db, evals, items, source='local2200', population='lichess',
                    answer_source='local2200'):
    """Per-position expected loss of the population distribution and of item answers.

    Only positions with evaluations appear. The value axis is therefore a sample
    of the domain and every number derived from it carries that sample size.
    """
    reach = load_reach(db, source)
    scope = defaultdict(list)
    for item in items:
        for key in item['keys']:
            scope[key].append(item)
    out = {}
    for key, covering in scope.items():
        _role, fen = position_role(db, key)
        scores = evals.get(fen)
        if not scores:
            continue
        dist_pop = load_distributions(db, key, population)
        dist_exp = load_distributions(db, key, answer_source)
        if not dist_pop:
            continue
        pop_result = metrics.expected_regret(dist_pop, scores)
        if pop_result.regret is None:
            continue
        exp_result = metrics.expected_regret(dist_exp, scores) if dist_exp else None
        best = pop_result.best_ep
        entry = {'loss_pop': pop_result.regret, 'best': best,
                 'loss_expert': exp_result.regret if exp_result else None,
                 'reach': reach.get(key, {}).get('reach', 0.0),
                 'n_moves_evaluated': pop_result.n_moves,
                 'mass_evaluated': pop_result.mass_covered, 'per_item': {}}
        for item in covering:
            # The taught answer set is modelled as equally weighted alternatives: the
            # learner plays one of the taught moves and the model does not claim to
            # know which. Weighting by the strong distribution instead would give a
            # deviation's prescribed punishment weight zero whenever strong play
            # happens not to have chosen it, which is exactly when it matters most.
            #
            # An item with NO answer set (orientation, recognition-only) is recorded as
            # None, never as 0.0: a taught loss of zero means perfect play, so scoring
            # recognition knowledge as 0.0 would hand it the entire value axis. This is
            # the mechanism that keeps recognition knowledge out of the EV comparison.
            if not item['answers']:
                entry['per_item'][item['item_id']] = None
                continue
            taught = {u: 1.0 for u in item['answers'] if u in scores}
            if not taught:
                entry['per_item'][item['item_id']] = None
                continue
            normalised, _total = metrics.renormalise(taught)
            loss = sum(prob * max(0.0, best - scores[uci]['ep_wp'])
                       for uci, prob in normalised.items())
            entry['per_item'][item['item_id']] = loss
        out[key] = entry
    return out


def baseline(values):
    """Ordinary-play loss on the evaluated sample (the denominator for recovery)."""
    return {'reach_covered': sum(v['reach'] for v in values.values()),
            'lost_population': sum(v['reach'] * v['loss_pop'] for v in values.values()),
            'lost_expert': sum(v['reach'] * (v.get('loss_expert') or 0.0) for v in values.values()),
            'positions': len(values)}


def frontier(items, values, k_max=40, min_step=1e-4):
    """Greedy value-per-cost selection with best-item-not-sum overlap accounting.

    Convention, stated once because it is easy to invert: `values[key]['per_item']`
    is the expected LOSS of the item's taught answers at that position, and
    `values[key]['loss_pop']` is the loss of ordinary play. Value is therefore the
    REDUCTION in loss, a lower taught loss is better, and at a position covered by
    several items the learner uses the best one — the minimum remaining loss.

    (An earlier version maximised the taught-answer loss, which inverted both the
    selection order and the meaning of `value_retained`.)
    """
    achieved = {key: entry['loss_pop'] for key, entry in values.items()}
    chosen, rows = [], []
    remaining = list(items)
    while remaining and len(chosen) < k_max:
        scored = []
        for item in remaining:
            gain = 0.0
            for key in item['keys']:
                entry = values.get(key)
                if not entry:
                    continue
                candidate = entry['per_item'].get(item['item_id'])
                if candidate is None:
                    continue
                current = achieved.get(key, entry['loss_pop'])
                if candidate < current:
                    gain += entry['reach'] * (current - candidate)
            scored.append((gain, item))
        scored.sort(key=lambda pair: (-pair[0], pair[1]['complexity']['cost'], pair[1]['item_id']))
        gain, item = scored[0]
        if gain < min_step:
            break
        for key in item['keys']:
            entry = values.get(key)
            if not entry:
                continue
            candidate = entry['per_item'].get(item['item_id'])
            if candidate is None:
                continue
            achieved[key] = min(achieved.get(key, entry['loss_pop']), candidate)
        chosen.append(item)
        remaining.remove(item)
        rows.append({
            'step': len(chosen), 'item_id': item['item_id'], 'type': item['type'],
            'label': item['label'], 'cost': item['complexity']['cost'],
            'marginal_value': gain,
            'value_retained': sum(v['reach'] * max(0.0, v['loss_pop'] - achieved[key])
                                  for key, v in values.items()),
            'coverage_flow': item['coverage'], 'circular': item.get('circular', False),
            'cumulative_cost': sum(i['complexity']['cost'] for i in chosen),
        })
    return rows, chosen, achieved


def recognition(db, items, source='local2200'):
    """Game mass whose flow reaches at least one item's scope (engine-free)."""
    covered = set()
    for item in items:
        covered.update(item['keys'])
    return position_union_coverage(db, covered, source)


def behavioural_reach(db, items, population='lichess', source='local2200'):
    """Engine-free axis: mass of ordinary play already playing the taught answers.

    Returns {'taught_mass', 'deviation_mass', 'positions', 'coverage_checked'} —
    the second number is the share of ordinary play the item would have to change.
    """
    reach = load_reach(db, source)
    taught = deviation = checked = 0.0
    positions = 0
    for item in items:
        if not item['answers']:
            continue
        for key in item['keys']:
            dist = load_distributions(db, key, population)
            if not dist:
                continue
            weight = reach.get(key, {}).get('reach', 0.0)
            positions += 1
            checked += weight
            taught += weight * sum(share for uci, share in dist.items()
                                   if uci in item['answers'])
            deviation += weight * sum(share for uci, share in dist.items()
                                      if uci not in item['answers'])
    return {'taught_mass': taught, 'deviation_mass': deviation, 'positions': positions,
            'coverage_checked': checked}


def compare_curricula(db, run_id=6, source='local2200', population='lichess'):
    """The six curricula the phase must compare, on the same value surface.

    Value credit is only ever given to items with a taught answer set (prescriptive
    rules, decision items, deviations). Recognition items are charged their burden
    and earn nothing, which is what keeps the comparison honest.

    Returns one row per curriculum with: items, burden (sum of complexity cost),
    recognition coverage (flow mass), value retained, recovery fraction of the
    ordinary-play loss on the evaluated sample, and the sample size behind it.
    """
    from study import rules as rules_module
    items = build_items(db, source, run_id, criterion='expert')
    derived = rules_module.derive_all(db, source)
    rule_items = rules_module.as_items(db, derived, source)
    prescriptive = [item for item in rule_items if item.get('kind') == 'prescriptive']
    recognition_items = [item for item in rule_items if item.get('kind') != 'prescriptive']
    decisions = [item for item in items if item['type'] == 'decision']
    deviations = [item for item in items if item['type'] == 'exception']

    curricula = {
        'ordinary_baseline': [],
        'exact_decisions_only': decisions,
        'prescriptive_templates_only': prescriptive,
        'deviations_only': deviations,
        'hybrid_templates_plus_exceptions': prescriptive + deviations,
        'engine_informed_ceiling': items + rule_items,
    }
    evals = load_evals(db)
    # one value surface and one denominator for every curriculum: the population
    # loss is a property of the domain, not of how much a curriculum happens to cover
    everything = [item for chosen in curricula.values() for item in chosen]
    shared_values = position_values(db, evals, everything, source, population) if everything else {}
    shared_base = baseline(shared_values) if shared_values else {'lost_population': None}
    out = []
    for name, chosen in curricula.items():
        values = {key: entry for key, entry in shared_values.items() if key in
                  {k for item in chosen for k in item['keys']}} if chosen else {}
        base = shared_base
        # value = reduction of the ordinary-play loss; the learner uses the best item
        achieved = {}
        for key, entry in values.items():
            losses = [loss for loss in
                      (entry['per_item'].get(item['item_id']) for item in chosen
                       if key in item['keys'])
                      if loss is not None]
            achieved[key] = min(losses) if losses else entry['loss_pop']
        retained = sum(values[key]['reach'] * max(0.0, values[key]['loss_pop'] - achieved[key])
                       for key in values)
        burden = sum(item['complexity']['cost'] for item in chosen)
        out.append({
            'curriculum': name, 'items': len(chosen), 'burden': burden,
            'positions_in_scope': len({k for item in chosen for k in item['keys']}),
            'recognition_coverage': (recognition(db, chosen, source) if chosen else 0.0),
            'evaluated_positions': len(values),
            'value_retained': retained,
            'lost_population_sample': base['lost_population'],
            'recovery_fraction': (retained / base['lost_population']
                                  if base['lost_population'] else None),
            'value_per_burden': (retained / burden) if burden else None,
            'recognition_only_items': sum(1 for item in chosen
                                          if item.get('kind') == 'recognition_only'),
        })
    return out


def persist(db, run_id, items):
    ensure_schema(db)
    for item in items:
        db.execute('''INSERT OR REPLACE INTO curriculum_item(
            item_id, run_id, type, label, scope_kind, scope_id, scope_json, positions,
            coverage_flow, coverage_games, coverage_source, prereq_json, benefit_ep,
            benefit_basis, benefit_sample, benefit_flags, complexity_json,
            exception_json, n_exceptions)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (item['item_id'], run_id, item['type'], item['label'], item['scope_kind'],
             item['scope_id'], json.dumps(_scope_payload(item)), item['positions'],
             item['coverage'], 0.0, item['coverage_source'],
             json.dumps(item.get('prereq', [])), item.get('benefit_ep'),
             item.get('benefit_basis'), item.get('benefit_sample'),
             json.dumps(item.get('benefit_flags', [])),
             json.dumps(item['complexity']), json.dumps(item.get('exceptions', [])),
             item.get('n_exceptions', 0)))
    db.commit()
    return db.execute('SELECT COUNT(*) FROM curriculum_item').fetchone()[0]


def _scope_payload(item):
    extras = {k: v for k, v in item.items()
              if k in ('priority', 'role', 'deviation_class', 'flags', 'selection_criterion',
                       'circular') and v is not None}
    return {'positions': [k.hex() for k in item['keys']], 'answers': item['answers'],
            'extras': extras}
