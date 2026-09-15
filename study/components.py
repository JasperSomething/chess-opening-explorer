"""Shared-knowledge compression of the frozen plan library.

Question: are hundreds of statistically distinct plans hundreds of things a human must
learn, or can they be generated from a much smaller reusable vocabulary plus a small
set of exceptions?

Nothing here creates a plan, an evaluation, a position or a request. It reads the
frozen library (``analysis/plan-library.json``) plus the existing evaluations, and
adds measurement only. Two accounting models are reported side by side and the
additive one stays the baseline:

* **additive (baseline)** — every plan is charged its own full burden, so a component
  shared by twenty plans is paid for twenty times;
* **shared (experimental)** — a component is charged once, and each plan is charged
  only the conditions, orderings and exceptions specific to it.

Two objectives are kept strictly apart, because conflating them would let engine
information leak into a behavioural claim:

* **behavioural** — how much of the strong players' actual move choice a curriculum
  explains (no engine involved);
* **engine-valued** — how much ordinary-player regret the curriculum removes
  (engine evaluations, used only for valuation).
"""
import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import plan_report, plans  # noqa: E402

HOME_RANK = {'1': 1, '8': 8}
LETTER_FOR = {'P': 'pawn', 'N': 'knight', 'B': 'bishop', 'R': 'rook', 'Q': 'queen',
              'K': 'king'}

# declared cost models; none is chosen after seeing results and none is tuned to make
# plans competitive. They differ only in how a *component* is priced relative to a
# plan-specific condition.
COST_MODELS = {
    'A_additive_baseline': {'kind': 'additive'},
    'B_shared_uniform': {'kind': 'shared', 'component': 1.0, 'specific': 1.0},
    'C_shared_cheap_components': {'kind': 'shared', 'component': 0.5, 'specific': 1.0},
    'D_shared_expensive_components': {'kind': 'shared', 'component': 2.0, 'specific': 1.0},
    'E_shared_typed': {'kind': 'shared', 'component': 1.0, 'specific': 1.0,
                       'typed': {'transformation': 1.0, 'goal': 0.5, 'action': 0.25,
                                 'ordering': 0.25, 'condition': 0.5, 'transition': 0.5}},
}


# ------------------------------------------------------------------- components
def is_home_square(square, colour):
    if not square:
        return False
    rank = square[1]
    return rank == ('1' if colour == 'w' else '8')


def _side_colour(plan):
    return 'w' if str(plan.get('side', '')).startswith('white') else 'b'


def plan_components(plan, level='goal'):
    """Decompose one plan into reusable components, at a declared abstraction level.

    * ``transformation`` — the exact event, piece and squares included;
    * ``goal`` — the destination square and the moving piece, not the origin, so two
      plans that develop the same piece to the same square share a component;
    * ``action`` — the action class only (development, break, castle, exchange,
      transition, file opening).
    """
    colour = _side_colour(plan)
    components = set()
    for event in plan['transformations']:
        kind = event[0]
        if kind in ('piece_move', 'pawn_move'):
            # a piece move carries its piece letter, a pawn move does not
            if kind == 'piece_move':
                letter, from_sq, to_sq = event[1], event[2], event[3]
            else:
                letter, from_sq, to_sq = 'P', event[1], event[2]
            if level == 'transformation':
                components.add(('transform', kind, letter, from_sq, to_sq))
            elif level == 'goal':
                components.add(('goal', letter, to_sq))
            else:
                if kind == 'pawn_move':
                    components.add(('action', 'pawn_break'))
                elif is_home_square(from_sq, colour):
                    components.add(('action', 'development'))
                else:
                    components.add(('action', 'relocation'))
        elif kind == 'castle':
            side = event[2]
            components.add(({'transformation': 'transform', 'goal': 'castle'}.get(
                level, 'action'), 'castle', side) if level != 'action'
                else ('action', f'castle_{side}'))
        elif kind == 'capture':
            components.add(({'transformation': 'transform', 'goal': 'exchange'}.get(
                level, 'action'), 'exchange') if level != 'action'
                else ('action', 'exchange'))
        elif kind == 'file_open':
            components.add(('file_open', event[1]) if level != 'action'
                           else ('action', 'file_open'))
        elif kind == 'structure_exit':
            components.add(('transition',) if level == 'action'
                           else ('transition', 'structure_exit'))
    return components


def plan_orderings(plan, level='goal'):
    """Ordering constraints implied by the canonical order, as pairs of components."""
    ordered = [event for event in plan['transformations']]
    pairs = set()
    for i, j in combinations(range(len(ordered)), 2):
        first = _event_component(ordered[i], level, plan)
        second = _event_component(ordered[j], level, plan)
        if first and second and first != second:
            pairs.add(('before', first, second))
    return pairs


def _event_component(event, level, plan):
    single = {'transformations': [event], 'side': plan.get('side')}
    components = plan_components(single, level)
    return sorted(components)[0] if components else None


def plan_specific(plan, level='goal'):
    """What a plan carries that is not a reusable component: its applicability and
    its exceptions (the boards where strong play gives it no support)."""
    return {
        'applicability': (('family', plan['structure_id']), ('side', plan.get('side'))),
        'exceptions': (plan.get('unavailable') or {}).get('no_strong_play_support', 0),
        'orderings': len(plan_orderings(plan, level)),
        'undecidable': sum(1 for event in plan['transformations']
                           if event[0] in ('capture', 'file_open', 'structure_exit')),
    }


def vocabulary(library, level='goal'):
    """Distinct components and how many behaviourally distinct plans use each."""
    usage = Counter()
    for plan in library['plans']:
        usage.update(plan_components(plan, level))
    orderings = Counter()
    for plan in library['plans']:
        orderings.update(plan_orderings(plan, level))
    return {'components': usage, 'orderings': orderings,
            'distinct_components': len(usage), 'distinct_orderings': len(orderings),
            'reused_components': sum(1 for _component, count in usage.items() if count > 1)}


def bundles(library, level='goal', min_plans=2, max_size=3):
    """Frequent bundles of components across behaviourally distinct plans (Apriori).

    A bundle is only interesting if it recurs, so single-use components are excluded
    from the search rather than being reported as one-plan "schemas".
    """
    sets = [plan_components(plan, level) for plan in library['plans']]
    sets = [s for s in sets if s]
    frequent = {}
    level_items = {frozenset((component,)) for component, count in
                   Counter(c for s in sets for c in s).items() if count >= min_plans}
    for item in level_items:
        frequent[item] = sum(1 for s in sets if item <= s)
    current = level_items
    while current:
        candidates = set()
        for a, b in combinations(sorted(current, key=repr), 2):
            union = a | b
            if len(union) != len(a) + 1:
                continue
            if all(frozenset(sub) in frequent for sub in combinations(union, len(union) - 1)):
                candidates.add(union)
        nxt = {}
        for candidate in candidates:
            if len(candidate) > max_size:
                continue
            count = sum(1 for s in sets if candidate <= s)
            if count >= min_plans:
                nxt[candidate] = count
        frequent.update(nxt)
        current = set(nxt)
    return [{'components': sorted(bundle, key=repr), 'plans': count,
             'size': len(bundle)} for bundle, count in frequent.items()]


def schemas(library, level='goal', min_plans=2, max_size=3, max_schemas=400):
    """Minimal bundles that explain several distinct plans, with optional parts.

    A schema is preferred at its smallest size that still covers the plans; optional
    components are recorded rather than folded into the requirement, so a schema never
    claims more than it explains.
    """
    found = bundles(library, level, min_plans=min_plans, max_size=max_size)
    plan_sets = [(plan, plan_components(plan, level)) for plan in library['plans']]
    out = []
    for bundle in sorted(found, key=lambda b: (-b['plans'], b['size'])):
        required = frozenset(bundle['components'])
        covered = [(plan, components) for plan, components in plan_sets
                   if required <= components]
        if len(covered) < min_plans:
            continue
        # an optional part is present in most covered plans but not all of them
        extra_counter = Counter()
        for _plan, components in covered:
            extra_counter.update(components - required)
        optional = sorted((component for component, count in extra_counter.items()
                           if count >= 0.5 * len(covered)), key=repr)
        residual = sorted({component for _plan, components in covered
                           for component in (components - required)}, key=repr)
        positions = {row['position_key'] for plan, _components in covered
                     for row in plan['rows']}
        mass = sum(row['reach'] for plan, _components in covered for row in plan['rows'])
        out.append({
            'schema_id': f"L{level}:{abs(hash(tuple(sorted(required, key=repr)))) % 10 ** 8:08x}",
            'level': level, 'required': sorted(required, key=repr), 'optional': optional,
            'covered_plans': [plan['plan_id'] for plan, _components in covered],
            'plans': len(covered), 'positions': len(positions), 'flow_mass': mass,
            'residual_components': residual, 'residual_count': len(residual)})
    out.sort(key=lambda schema: (-schema['plans'], schema['residual_count'],
                                 len(schema['required'])))
    return out[:max_schemas]


# ------------------------------------------------------------------ burden models
def information_inventory(library, level='goal'):
    """Every distinct piece of information in the library, and how many plans use it.

    The rule the shared model applies is simply: **information is charged once**, no
    matter how many plans use it. Items used by more than one plan are the shared
    vocabulary; items used by exactly one plan are that plan's specific overhead.
    Orderings count as components here because an ordering that recurs across plans is
    reusable knowledge, not per-plan overhead.
    """
    inventory = defaultdict(set)
    for plan in library['plans']:
        pid = plan['plan_id']
        for component in plan_components(plan, level):
            inventory[('component', component)].add(pid)
        for ordering in plan_orderings(plan, level):
            inventory[('ordering', ordering)].add(pid)
        detail = plan_specific(plan, level)
        if detail['exceptions']:
            inventory[('exception', (plan['structure_id'], detail['exceptions']))].add(pid)
        if detail['undecidable']:
            inventory[('undecidable', (plan['structure_id'], detail['undecidable']))].add(pid)
    return inventory


def additive_burden(library):
    """The frozen baseline: every plan is charged its own full burden."""
    total = sum(plan['burden']['cost'] for plan in library['plans'])
    return {'model': 'A_additive_baseline', 'total': total,
            'per_plan_mean': total / max(len(library['plans']), 1)}


def item_weight(kind, component, spec):
    if spec.get('typed'):
        if kind == 'component':
            return spec['typed'].get(component[0], spec['component'])
        if kind == 'ordering':
            return spec['typed'].get('ordering', spec['component'])
        return spec['typed'].get(kind, spec['specific'])
    return spec['component'] if kind == 'component' else spec['specific']


def shared_burden(library, model, level='goal', inventory=None):
    """Charge each distinct item once, and report the shared/specific split."""
    spec = COST_MODELS[model]
    inventory = inventory if inventory is not None else information_inventory(library, level)
    shared_cost = specific_cost = 0.0
    shared_items = specific_items = 0
    kinds = Counter()
    for (kind, component), users in inventory.items():
        weight = item_weight(kind, component, spec)
        kinds[kind] += 1
        if len(users) > 1:
            shared_cost += weight
            shared_items += 1
        else:
            specific_cost += weight
            specific_items += 1
    return {'model': model, 'total': shared_cost + specific_cost,
            'shared_cost': shared_cost, 'specific_cost': specific_cost,
            'shared_items': shared_items, 'specific_items': specific_items,
            'items': len(inventory), 'kinds': dict(kinds)}


def per_plan_marginal(library, model, level='goal', inventory=None):
    """Marginal cost of adding each plan to a curriculum that already has the others
    before it: exactly the items it introduces that no earlier plan did."""
    inventory = inventory if inventory is not None else information_inventory(library, level)
    spec = COST_MODELS[model]
    first_user = {}
    for (kind, component), users in inventory.items():
        for pid in users:
            first_user.setdefault((kind, component), None)
    # deterministic order: plans sorted by id, so the marginal profile is reproducible
    ordered = sorted(library['plans'], key=lambda plan: plan['plan_id'])
    seen, marginal = set(), {}
    for plan in ordered:
        detail_items = set()
        for component in plan_components(plan, level):
            detail_items.add(('component', component))
        for ordering in plan_orderings(plan, level):
            detail_items.add(('ordering', ordering))
        cost = 0.0
        for item in detail_items:
            if item not in seen:
                cost += item_weight(item[0], item[1], spec)
        seen |= detail_items
        marginal[plan['plan_id']] = cost
    return marginal


def burden_table(library, level='goal'):
    inventory = information_inventory(library, level)
    rows = [dict(additive_burden(library), shared_cost=None, specific_cost=None,
                 components=None)]
    for model in COST_MODELS:
        if COST_MODELS[model]['kind'] != 'shared':
            continue
        entry = shared_burden(library, model, level, inventory)
        rows.append({'model': model, 'total': entry['total'],
                     'shared_cost': entry['shared_cost'],
                     'specific_cost': entry['specific_cost'],
                     'components': entry['items'],
                     'shared_items': entry['shared_items'],
                     'specific_items': entry['specific_items']})
    return rows
