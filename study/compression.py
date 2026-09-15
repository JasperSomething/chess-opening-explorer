"""Shared-knowledge experiment: does a reusable vocabulary generate the plan library?

Everything is built from the frozen plan library and the existing evaluations. No
plan, position, evaluation or request is created here; the plan library is loaded
from ``analysis/plan-library.json`` and never re-derived.

Two objectives are computed separately and never mixed into one number:

* **behavioural** — the share of strong players' actual move choice that a curriculum
  explains, measured on the strong-play distributions alone (no engine);
* **engine-valued** — the ordinary-player regret a curriculum removes, using the
  existing evaluations for valuation only.

The marginal burden of an addition is measured against what is already learned: a
component introduced by an earlier item is free the second time. That is the whole
point of the shared model, and it is applied identically to plans, schemas and
components so no class gets a cheaper yardstick than another.
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import components, db as studydb, plans  # noqa: E402


# ---------------------------------------------------------------------- evidence
_EVAL_CACHE = {}


def _evals(db):
    if 'evals' not in _EVAL_CACHE:
        from study import curriculum
        _EVAL_CACHE['evals'] = curriculum.load_evals(db)
    return _EVAL_CACHE['evals']


def curriculum_fen(db, key):
    from study import curriculum
    return curriculum.position_role(db, key)


def build_evidence(db, library, source='local2200'):
    """Board-level evidence: reach, ordinary-play loss, engine scores, strong shares."""
    from study import campaign, curriculum
    evals = _evals(db)
    evidence = {}
    for row in db.execute('SELECT position_key, fen FROM position'):
        evidence.setdefault(row['position_key'], {'fen': row['fen']})
    boards = {}
    for structure_id in library['structures']:
        for row in plans.plan_scope(db, {'structure_id': structure_id, 'side': None}, source):
            key = row['key']
            if key in boards:
                continue
            fen = evidence.get(key, {}).get('fen') or curriculum.position_role(db, key)[1]
            scores = evals.get(fen)
            dist_strong = curriculum.load_distributions(db, key, source)
            pop_source, dist_pop, _games = curriculum.load_population(db, key)
            if not scores or not dist_strong or not dist_pop:
                continue
            if not any(uci in scores for uci in dist_pop):
                continue
            best = max(entry['ep_wp'] for entry in scores.values())
            ordinary = campaign.value_of_answers(evals, scores, dist_strong, dist_pop, best)
            if ordinary['loss_pop'] is None:
                continue
            strong_total = sum(dist_strong.values()) or 1.0
            boards[key] = {
                'position_key': key.hex(), 'fen': fen, 'reach': row['reach'],
                'loss_pop': ordinary['loss_pop'], 'scores': scores,
                'strong_shares': {uci: share / strong_total
                                  for uci, share in dist_strong.items()},
            }
    return boards


# ------------------------------------------------------------------------ items
class Item:
    """A candidate piece of knowledge, with what it costs and what it explains."""

    def __init__(self, kind, item_id, items, boards, label=''):
        self.kind = kind
        self.item_id = item_id
        self.items = set(items)          # reusable information it introduces
        self.boards = boards             # {position_key: {'loss', 'explained'}}
        self.label = label


def plan_items(library, evidence, level='goal'):
    """Plans as candidates: their reusable items and their per-board effect."""
    out = []
    for plan in library['plans']:
        items = {('component', component) for component in
                 components.plan_components(plan, level)}
        items |= {('ordering', ordering) for ordering in
                  components.plan_orderings(plan, level)}
        boards = {}
        for row in plan['rows']:
            key = bytes.fromhex(row['position_key'])
            board = evidence.get(key)
            if board is None:
                continue
            # a prescription cannot explain more than all of strong play: without the
            # cap, two answers each covering most of the mass would count as 1.2
            explained = min(1.0, sum(board['strong_shares'].get(uci, 0.0)
                                     for uci in row['answers']))
            boards[key] = {'loss': row['loss_taught'], 'explained': explained}
        if boards:
            out.append(Item('plan', plan['plan_id'], items, boards, plan['structure_id']))
    return out


def schema_items(library, evidence, level='goal', min_plans=3, max_size=3):
    """Schemas as candidates: their components, explaining every plan they cover."""
    found = components.schemas(library, level, min_plans=min_plans, max_size=max_size)
    by_id = {plan['plan_id']: plan for plan in library['plans']}
    out = []
    for schema in found:
        items = {('component', component) for component in schema['required']}
        boards = {}
        for plan_id in schema['covered_plans']:
            plan = by_id.get(plan_id)
            if plan is None:
                continue
            for row in plan['rows']:
                key = bytes.fromhex(row['position_key'])
                board = evidence.get(key)
                if board is None:
                    continue
                explained = min(1.0, sum(board['strong_shares'].get(uci, 0.0)
                                         for uci in row['answers']))
                previous = boards.get(key)
                if previous is None or row['loss_taught'] < previous['loss']:
                    boards[key] = {'loss': min(row['loss_taught'],
                                               previous['loss'] if previous else 1e9),
                                   'explained': max(explained,
                                                    previous['explained'] if previous else 0.0)}
        if boards:
            out.append(Item('schema', schema['schema_id'], items, boards,
                            f"{schema['plans']} plans"))
    return out


def decision_items(evidence, top_per_board=1):
    """Exact decisions: the engine-best move at each board, one decision per board.

    Charged as a single exact item each — the most expensive kind of knowledge, and
    the yardstick the plans have to beat.
    """
    out = []
    for key, board in evidence.items():
        ranked = sorted(board['scores'].items(), key=lambda kv: -kv[1]['ep_wp'])
        for uci, _entry in ranked[:top_per_board]:
            explained = board['strong_shares'].get(uci, 0.0)
            out.append(Item('decision', f"d:{key.hex()}:{uci}",
                            {('decision', (key.hex(), uci))},
                            {key: {'loss': max(0.0, board['scores'][uci]['ep_wp']
                                               - board['scores'][uci]['ep_wp']),
                                   'explained': explained}},
                            'one exact move'))
    return out


def component_items(library, evidence, level='goal'):
    """Bare reusable components: the vocabulary on its own, as a lower rung."""
    usage = components.vocabulary(library, level)['components']
    out = []
    for component in usage:
        boards = {}
        for plan in library['plans']:
            if component not in components.plan_components(plan, level):
                continue
            for row in plan['rows']:
                key = bytes.fromhex(row['position_key'])
                board = evidence.get(key)
                if board is None:
                    continue
                explained = max(board['strong_shares'].get(uci, 0.0)
                                for uci in row['answers'])
                previous = boards.get(key)
                if previous is None or row['loss_taught'] < previous['loss']:
                    boards[key] = {'loss': row['loss_taught'],
                                   'explained': max(explained,
                                                    previous['explained'] if previous else 0.0)}
        if boards:
            out.append(Item('component', f"c:{component}", {('component', component)}, boards))
    return out


# -------------------------------------------------------------------- frontiers
def frontier(items, evidence, cost_model='B_shared_uniform', level='goal', steps=25,
             objective='value'):
    """Greedy construction with marginal burden net of what is already learned."""
    spec = components.COST_MODELS[cost_model]
    if spec['kind'] == 'additive':
        weight = lambda kind, payload: 1.0  # noqa: E731  (plan = one unit of burden)
    else:
        weight = lambda kind, payload: components.item_weight(kind, payload, spec)  # noqa: E731

    learned = set()
    state_loss, state_explained = {}, {}
    chosen, rows = [], []
    remaining = list(items)
    family_reach = sum(board['reach'] for board in evidence.values())
    for _step in range(steps):
        best, best_score, best_payload = None, None, None
        for item in remaining:
            new_items = item.items - learned
            marginal = sum(weight(kind, payload) for kind, payload in new_items)
            if marginal <= 0:
                marginal = 1e-9
            delta_value = 0.0
            delta_behaviour = 0.0
            for key, effect in item.boards.items():
                reach = evidence[key]['reach']
                previous_loss = state_loss.get(key)
                if previous_loss is None or effect['loss'] < previous_loss:
                    delta_value += reach * max(0.0, evidence[key]['loss_pop'] - effect['loss'])
                    if previous_loss is not None:
                        delta_value -= reach * max(0.0, evidence[key]['loss_pop'] - previous_loss)
                previous_explained = state_explained.get(key, 0.0)
                if effect['explained'] > previous_explained:
                    delta_behaviour += reach * (effect['explained'] - previous_explained)
            score = (delta_value if objective == 'value' else delta_behaviour) / marginal
            if best_score is None or score > best_score:
                best, best_score, best_payload = item, score, (delta_value, delta_behaviour,
                                                              new_items, marginal)
        if best is None or best_score is None or best_score <= 0:
            break
        delta_value, delta_behaviour, new_items, marginal = best_payload
        for key, effect in best.boards.items():
            reach = evidence[key]['reach']
            current = state_loss.get(key)
            if current is None or effect['loss'] < current:
                state_loss[key] = effect['loss']
            if effect['explained'] > state_explained.get(key, 0.0):
                state_explained[key] = effect['explained']
        learned |= best.items
        chosen.append(best.item_id)
        remaining.remove(best)
        value = sum(evidence[key]['reach'] * max(0.0, evidence[key]['loss_pop'] - loss)
                    for key, loss in state_loss.items())
        explained = sum(evidence[key]['reach'] * state_explained.get(key, 0.0)
                        for key in state_explained)
        rows.append({'k': len(chosen), 'item': best.item_id, 'kind': best.kind,
                     'marginal_burden': marginal, 'delta_value': delta_value,
                     'delta_behaviour': delta_behaviour,
                     'cumulative_value': value, 'cumulative_explained': explained,
                     'behaviour_fraction': explained / family_reach if family_reach else None,
                     'burden': sum(row['marginal_burden'] for row in rows) + marginal,
                     'value_per_burden': value / (sum(row['marginal_burden'] for row in rows)
                                                  + marginal)})
    return rows


def curriculum_summary(items, evidence, cost_model, level='goal', additive=False,
                       frozen_plans=None):
    """Value, behaviour and burden of a whole set, under one declared cost model.

    ``additive`` charges every plan its own frozen burden (the original, unchanged
    baseline); the shared models charge each distinct piece of information once.
    """
    spec = components.COST_MODELS[cost_model]
    learned = set()
    for item in items:
        learned |= item.items
    if additive:
        if frozen_plans is not None:
            wanted = {item.item_id for item in items if item.kind == 'plan'}
            burden = sum(plan['burden']['cost'] for plan in frozen_plans
                         if plan['plan_id'] in wanted)
            burden += float(sum(1 for item in items if item.kind != 'plan'))
        else:
            burden = float(len(items))
    elif spec['kind'] == 'shared':
        burden = sum(components.item_weight(kind, payload, spec) for kind, payload in learned)
    else:
        burden = float(len(items))
    state_loss, state_explained = {}, {}
    for item in items:
        for key, effect in item.boards.items():
            current = state_loss.get(key)
            if current is None or effect['loss'] < current:
                state_loss[key] = effect['loss']
            if effect['explained'] > state_explained.get(key, 0.0):
                state_explained[key] = effect['explained']
    family_reach = sum(board['reach'] for board in evidence.values())
    value = sum(evidence[key]['reach'] * max(0.0, evidence[key]['loss_pop'] - loss)
                for key, loss in state_loss.items())
    explained = sum(evidence[key]['reach'] * state_explained.get(key, 0.0)
                    for key in state_explained)
    denominator = sum(board['reach'] * board['loss_pop'] for board in evidence.values())
    return {'items': len(items), 'burden': burden,
            'burden_additive': burden if additive else None,
            'value_retained': value,
            'recovery': (value / denominator) if denominator else None,
            'behaviour_explained': explained,
            'behaviour_fraction': (explained / family_reach) if family_reach else None,
            'value_per_burden': (value / burden) if burden else None,
            'positions_covered': len(state_loss)}


def load(path=ROOT / 'analysis' / 'plan-library.json'):
    with open(path) as handle:
        return json.load(handle)


def build(db_path=ROOT / 'data' / 'atlas-analysis.sqlite', library_path=None,
          level='goal', source='local2200'):
    library = load(library_path or ROOT / 'analysis' / 'plan-library.json')
    db = studydb.connect(db_path, create=False)
    evidence = build_evidence(db, library, source)
    payload = {'library': library, 'evidence': evidence, 'db': db, 'level': level,
               'plans': plan_items(library, evidence, level),
               'schemas': schema_items(library, evidence, level),
               'decisions': decision_items(evidence),
               'components': component_items(library, evidence, level)}
    return payload


def render(options):
    payload = build(options.db, options.library, level=options.level)
    db, library, evidence = payload['db'], payload['library'], payload['evidence']
    level = options.level
    inventory = components.information_inventory(library, level)
    burd = components.burden_table(library, level)
    vocab = components.vocabulary(library, level)
    schemas = components.schemas(library, level, min_plans=3, max_size=3)

    lines = ['# Shared-knowledge compression experiment (Scandinavian mature families)\n']
    lines.append('Prototype only. Built from the frozen plan library '
                 f"({len(library['plans'])} behaviourally distinct plans, sha1 "
                 f"`{library.get('library_sha1', 'n/a')[:12]}`) and the existing "
                 'evaluations: no new plan, position, evaluation or request. Two '
                 'accounting models are reported side by side and the additive one '
                 'stays the baseline; two objectives (behavioural and engine-valued) '
                 'are never merged into one number.\n')

    lines.append('## 1. Component vocabulary\n')
    lines.append('| abstraction level | distinct components | used by >1 plan | distinct orderings |')
    lines.append('|' + '---|' * 4)
    for lvl in ('transformation', 'goal', 'action'):
        entry = components.vocabulary(library, lvl)
        lines.append(f"| {lvl} | {entry['distinct_components']} | {entry['reused_components']} | "
                     f"{entry['distinct_orderings']} |")
    lines.append(f"\nInventory at the **{level}** level: "
                 + ', '.join(f"{kind} {count}" for kind, count in
                             sorted(inventory_kinds(inventory).items()))
                 + f" — {len(inventory)} distinct pieces of information in total.\n")
    lines.append('Most reused components (goal level):\n')
    lines.append('| plans using it | component |')
    lines.append('|---|---|')
    for component, count in vocab['components'].most_common(12):
        lines.append(f'| {count} | `{component}` |')

    lines.append('\n## 2. Redundancy of the 245 plan policies\n')
    union_boards = {row['position_key'] for plan in library['plans'] for row in plan['rows']}
    signatures = Counter(tuple(sorted((row['position_key'], tuple(row['answers']))
                                      for row in plan['rows']))
                         for plan in library['plans'])
    multi = sum(1 for _sig, count in signatures.items() if count > 1)
    lines.append(f'- distinct prescribed board sets: {len(signatures)} across '
                 f'{len(library["plans"])} plans')
    lines.append(f'- board sets prescribed by more than one plan: {multi}')
    lines.append(f'- union of boards any plan prescribes on: {len(union_boards)}')
    lines.append(f'- total (plan, board) prescription slots: '
                 f'{sum(len(plan["rows"]) for plan in library["plans"])}')
    lines.append(f'- distinct exact decisions the plans collectively replace: '
                 f'{len({(row["position_key"], answer) for plan in library["plans"] for row in plan["rows"] for answer in row["answers"]})}\n')

    lines.append('## 3. Reusable schemas\n')
    lines.append(f'{len(schemas)} schemas cover two or more distinct plans. Smallest '
                 f'first, with the plans each one explains and the residual components '
                 f'it does not account for.\n')
    lines.append('| schema | required components | plans | positions | flow mass | residual components |')
    lines.append('|' + '---|' * 6)
    for schema in schemas[:15]:
        required = ', '.join(str(component) for component in schema['required'])
        lines.append(f"| `{schema['schema_id']}` | {required} | {schema['plans']} | "
                     f"{schema['positions']} | {schema['flow_mass']:.4f} | "
                     f"{schema['residual_count']} |")

    lines.append('\n## 4. Learning burden: additive baseline vs shared accounting\n')
    lines.append('| cost model | total burden | shared | plan-specific | items |')
    lines.append('|' + '---|' * 5)
    for row in burd:
        shared = ('—' if row.get('shared_cost') is None else f"{row['shared_cost']:.1f}")
        specific = ('—' if row.get('specific_cost') is None else f"{row['specific_cost']:.1f}")
        lines.append(f"| {row['model']} | {row['total']:.1f} | {shared} | {specific} | "
                     f"{row.get('components', '—')} |")
    lines.append(f"\nThe additive model charges every plan its own full burden "
                 f"({burd[0]['total']:.1f} for {len(library['plans'])} plans); the shared "
                 f"models charge each distinct piece of information once. No cost model "
                 f"was chosen after seeing these numbers.\n")

    lines.append('## 5. Curricula under both accountings\n')
    lines.append('| curriculum | items | burden (model) | value retained | recovery | '
                 'behaviour explained | value per burden |')
    lines.append('|' + '---|' * 7)
    comparison = []
    cases = [('exact decisions only', payload['decisions'], 'B_shared_uniform', False),
             ('plans, additive burden', payload['plans'], 'A_additive_baseline', True),
             ('plans, shared burden', payload['plans'], 'B_shared_uniform', False),
             ('schemas + residual exact decisions',
              payload['schemas'] + payload['decisions'], 'B_shared_uniform', False),
             ('unrestricted mixed (plans + schemas + components + decisions)',
              payload['plans'] + payload['schemas'] + payload['components'] + payload['decisions'],
              'B_shared_uniform', False)]
    for name, items, model, additive in cases:
        summary = curriculum_summary(items, evidence, model, level, additive=additive,
                                     frozen_plans=library['plans'])
        summary['curriculum'] = name
        comparison.append(summary)
        lines.append(f"| {name} | {summary['items']} | {summary['burden']:.1f} | "
                     f"{summary['value_retained']:.6f} | "
                     f"{(('%.4f' % summary['recovery']) if summary['recovery'] is not None else '—')} | "
                     f"{(('%.4f' % summary['behaviour_fraction']) if summary['behaviour_fraction'] is not None else '—')} | "
                     f"{(('%.6f' % summary['value_per_burden']) if summary['value_per_burden'] else '—')} |")

    lines.append('\n### Greedy frontiers by knowledge class (marginal burden net of '
                 'what is already learned)\n')
    lines.append('Each class is given the same greedy procedure and the same cost model, '
                 'so the curves are comparable: the question is which class buys the most '
                 'per unit of *marginal* burden once shared components are accounted for.\n')
    lines.append('| class | objective | k | cumulative value | recovery | behaviour '
                 'explained | burden | value per burden |')
    lines.append('|' + '---|' * 8)
    class_frontiers = {}
    for name, candidates in (('plans only', payload['plans']),
                             ('schemas only', payload['schemas']),
                             ('exact decisions only', payload['decisions']),
                             ('schemas + exact decisions',
                              payload['schemas'] + payload['decisions']),
                             ('mixed (all classes)',
                              payload['plans'] + payload['schemas'] + payload['components']
                              + payload['decisions'])):
        for objective in ('value', 'behaviour'):
            rows = frontier(candidates, evidence, options.cost_model, level,
                            steps=options.steps, objective=objective)
            class_frontiers[f'{name}|{objective}'] = rows
            if not rows:
                continue
            last = rows[-1]
            denominator = sum(board['reach'] * board['loss_pop']
                              for board in evidence.values())
            family_reach = sum(board['reach'] for board in evidence.values())
            lines.append(f"| {name} | {objective} | {len(rows)} | "
                         f"{last['cumulative_value']:.6f} | "
                         f"{(last['cumulative_value'] / denominator if denominator else 0):.4f} | "
                         f"{(last['cumulative_explained'] / family_reach if family_reach else 0):.4f} | "
                         f"{last['burden']:.1f} | {last['value_per_burden']:.6f} |")

    lines.append('\n### Greedy frontier detail (mixed class, value objective)\n')
    frontiers = {}
    for objective in ('value', 'behaviour'):
        rows = frontier(payload['plans'] + payload['schemas'] + payload['decisions'],
                        evidence, options.cost_model, level, steps=options.steps,
                        objective=objective)
        frontiers[objective] = rows
        lines.append(f'**Objective: {objective}**\n')
        lines.append('| k | item added | kind | marginal burden | delta | cumulative value | '
                     'cumulative behaviour | value per burden |')
        lines.append('|' + '---|' * 8)
        for row in rows:
            delta = row['delta_value'] if objective == 'value' else row['delta_behaviour']
            lines.append(f"| {row['k']} | `{row['item']}` | {row['kind']} | "
                         f"{row['marginal_burden']:.2f} | {delta:.5f} | "
                         f"{row['cumulative_value']:.6f} | {row['cumulative_explained']:.5f} | "
                         f"{row['value_per_burden']:.6f} |")
        lines.append('')

    lines.append('## 6. Sensitivity across declared cost models\n')
    lines.append('The same frontier objective re-run under every declared model, so the '
                 'conclusion cannot rest on one favourable price.\n')
    lines.append('| cost model | items chosen in 25 steps | cumulative value | burden | value per burden |')
    lines.append('|' + '---|' * 5)
    sensitivity = []
    for model in components.COST_MODELS:
        rows = frontier(payload['plans'] + payload['schemas'] + payload['decisions'],
                        evidence, model, level, steps=options.steps, objective='value')
        if not rows:
            continue
        last = rows[-1]
        sensitivity.append({'model': model, 'k': len(rows), 'value': last['cumulative_value'],
                            'burden': last['burden'],
                            'value_per_burden': last['value_per_burden']})
        lines.append(f"| {model} | {len(rows)} | {last['cumulative_value']:.6f} | "
                     f"{last['burden']:.1f} | {last['value_per_burden']:.6f} |")

    lines.append('\n## 7. Compression ladder\n')
    ladder = compression_ladder(library, evidence, payload, level, inventory, schemas)
    lines.append('| level | items | information still required |')
    lines.append('|' + '---|' * 3)
    for row in ladder:
        lines.append(f"| {row['level']} | {row['items']} | {row['residual']} |")

    (options.out / 'shared-knowledge-compression.md').write_text('\n'.join(lines) + '\n')
    (options.out / 'shared-knowledge-figures.json').write_text(json.dumps(
        {'burden': burd, 'curricula': comparison, 'sensitivity': sensitivity,
         'frontiers': frontiers, 'class_frontiers': class_frontiers, 'ladder': ladder,
         'vocabulary': {lvl: components.vocabulary(library, lvl)['distinct_components']
                        for lvl in ('transformation', 'goal', 'action')},
         'schemas': schemas[:25]}, indent=1, default=str))
    print(json.dumps({'report': str(options.out / 'shared-knowledge-compression.md'),
                      'curricula': [{k: (round(v, 6) if isinstance(v, float) else v)
                                     for k, v in row.items()} for row in comparison],
                      'sensitivity': [{k: (round(v, 6) if isinstance(v, float) else v)
                                       for k, v in row.items()} for row in sensitivity],
                      'ladder': ladder}, indent=1, default=str))
    db.close()


def inventory_kinds(inventory):
    return Counter(kind for kind, _payload in inventory)


def compression_ladder(library, evidence, payload, level, inventory, schemas):
    """How much information each rung of the compression actually needs."""
    boards = len(evidence)
    decisions = payload['decisions']
    distinct_decisions = len({(item.boards and list(item.boards)[0], item.item_id)
                              for item in decisions})
    plan_items_count = len(payload['plans'])
    vocab = components.vocabulary(library, level)
    schema_components = {component for schema in schemas
                         for component in schema['required']}
    ladder = [
        {'level': 'family surface boards', 'items': boards,
         'residual': 'the board set itself'},
        {'level': 'exact decisions (one best move per board)',
         'items': distinct_decisions,
         'residual': 'one exact move per board, no reuse'},
        {'level': 'mined plans', 'items': library['n_mined'],
         'residual': 'one prescription per board per plan, no deduplication'},
        {'level': 'behaviourally distinct plans', 'items': plan_items_count,
         'residual': f"{sum(len(plan['rows']) for plan in library['plans'])} "
                     f"(plan, board) prescription slots"},
        {'level': f'distinct primitive components ({level})',
         'items': vocab['distinct_components'],
         'residual': f"{vocab['distinct_orderings']} ordering constraints and "
                     f"{len(inventory)} distinct items in total"},
        {'level': 'reusable bundles (frequent component sets)',
         'items': len(components.bundles(library, level, min_plans=2, max_size=3)),
         'residual': 'which plans each bundle explains'},
        {'level': 'higher-level schemas', 'items': len(schemas),
         'residual': f"required/optional split, applicability, "
                     f"{sum(schema['residual_count'] for schema in schemas)} residual "
                     f"components over all schemas"},
    ]
    ladder[4]['residual'] = (f"{vocab['distinct_components']} components + "
                             f"{vocab['distinct_orderings']} orderings + "
                             f"{len(inventory)} items charged once each")
    ladder.append({'level': 'schemas + residual exact decisions',
                   'items': len(schemas) + len(decisions),
                   'residual': 'per-board exact moves only where no schema prescribes'})
    ladder[5]['residual'] = (f"required sets drawn from {len(schema_components)} distinct "
                             f"components; the rest of the vocabulary is residual")
    return ladder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=ROOT / 'data' / 'atlas-analysis.sqlite')
    parser.add_argument('--library', type=Path, default=ROOT / 'analysis' / 'plan-library.json')
    parser.add_argument('--out', type=Path, default=ROOT / 'analysis')
    parser.add_argument('--level', default='goal', choices=('transformation', 'goal', 'action'))
    parser.add_argument('--cost-model', default='B_shared_uniform')
    parser.add_argument('--steps', type=int, default=25)
    options = parser.parse_args()
    render(options)


if __name__ == '__main__':
    main()
