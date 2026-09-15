"""Plan-compression prototype: does a recurring plan compress move choice better than
a family → fixed-answer rule?

Reported for the existing Scandinavian mature families only, from current data, with
no new engine compute. Three comparisons are deliberately kept separate:

* **plan EV vs fixed-rule EV vs exact decisions** — all on one shared surface and one
  shared denominator, with the max-not-sum overlap rule;
* **board-level gain distributions and medians**, because a plan whose value comes
  from one exceptional board has failed in exactly the way the fixed templates did;
* **compression** — how many distinct positions and decisions one plan replaces.

Engine evaluations are used only to value moves the plan already prescribes. A board
where the plan cannot prescribe (no actionable transformation, no strong-play support,
or an undecidable transformation) is counted as non-prescriptive, never as zero gain.
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

import chess  # noqa: E402

from study import (campaign, curriculum, db as studydb, motifs, plans,  # noqa: E402
                   rules, structure_flow)


class Surface:
    """Boards, evaluations and distributions, read once and reused across classes."""

    def __init__(self, db, source='local2200', population='auto'):
        self.db = db
        self.source = source
        self.population = population
        self.evals = curriculum.load_evals(db)
        self.reach = curriculum.load_reach(db, source)
        self.positions, self.moves = structure_flow.load_graph_inputs(db, source)
        self._boards = {}
        self._loss = {}
        self._scope = {}
        self._context = {}
        self._events = {}

    def scope(self, plan):
        """Boards a plan applies to, cached per (family, side): this is queried once
        per plan otherwise, and the prototype evaluates every plan."""
        memo = (plan['structure_id'], plan.get('side'))
        if memo not in self._scope:
            self._scope[memo] = plans.plan_scope(self.db, plan, self.source)
        return self._scope[memo]

    def events_for(self, key, uci, structure_id):
        """Events a move produces at a board, cached: plans in a family share boards."""
        memo = (key, uci, structure_id)
        if memo not in self._events:
            board = self.board(key)
            self._events[memo] = (motifs.classify_move(
                board, uci, None, {'structure': structure_id}, self.positions)
                if board is not None else ())
        return self._events[memo]

    def advancing(self, key, events, structure_id):
        """Legal moves performing one of ``events`` at this board (cached events)."""
        board = self.board(key)
        if board is None:
            return {}
        remaining = set(events)
        out = {}
        for move in board.legal_moves:
            uci = move.uci()
            matched = [event for event in self.events_for(key, uci, structure_id)
                       if event in remaining]
            if matched:
                out[uci] = matched
        return out

    def board(self, key):
        board = self._boards.get(key)
        if board is None:
            _role, fen = curriculum.position_role(self.db, key)
            board = chess.Board(fen + ' 0 1') if fen else None
            self._boards[key] = board
        return board

    def context(self, key):
        """Everything needed to value one board, or None when it is unavailable."""
        if key in self._context:
            return self._context[key]
        self._context[key] = self._context_of(key)
        return self._context[key]

    def _context_of(self, key):
        _role, fen = curriculum.position_role(self.db, key)
        scores = self.evals.get(fen)
        dist_strong = curriculum.load_distributions(self.db, key, self.source)
        if self.population == 'auto':
            pop_source, dist_pop, _games = curriculum.load_population(self.db, key)
        else:
            pop_source = self.population
            dist_pop = curriculum.load_distributions(self.db, key, self.population)
        if not scores or not dist_strong or not dist_pop:
            return None
        if not any(uci in scores for uci in dist_pop):
            return None
        best = max(entry['ep_wp'] for entry in scores.values())
        ordinary = campaign.value_of_answers(self.evals, scores, dist_strong, dist_pop, best)
        if ordinary['loss_pop'] is None:
            return None
        return {'scores': scores, 'dist_strong': dist_strong, 'dist_pop': dist_pop,
                'best': best, 'loss_pop': ordinary['loss_pop'], 'population_source': pop_source}

    def loss(self, key, answers):
        """Loss of a taught answer set at a board, memoised; None means unavailable."""
        if not answers:
            return None
        memo_key = (key, frozenset(answers))
        if memo_key in self._loss:
            return self._loss[memo_key]
        context = self.context(key)
        loss = None
        if context is not None:
            value = campaign.value_of_answers(self.evals, context['scores'],
                                              context['dist_strong'], context['dist_pop'],
                                              context['best'], answers=set(answers))
            loss = value['loss_taught']
        self._loss[memo_key] = loss
        return loss


def plan_rows(db, plan, surface):
    """Per-board prescription for one plan, with the reason when it prescribes nothing."""
    events = [tuple(event) for event in plan['transformations']]
    outcome = []
    unavailable = defaultdict(int)
    for row in surface.scope(plan):
        key = row['key']
        context = surface.context(key)
        if context is None:
            unavailable['board_unavailable'] += 1
            continue
        board = surface.board(key)
        if board is None:
            unavailable['board_unavailable'] += 1
            continue
        states = {event: plans.event_state(board, event) for event in events}
        remaining = [event for event, state in states.items()
                     if state == plans.ACTIONABLE]
        if not remaining:
            unavailable['no_actionable_transformation'] += 1
            continue
        advancing = surface.advancing(key, remaining, plan['structure_id'])
        taught = [uci for uci in advancing if uci in context['dist_strong']]
        if not taught:
            unavailable['no_strong_play_support'] += 1
            continue
        loss = surface.loss(key, taught)
        if loss is None:
            unavailable['taught_moves_not_evaluated'] += 1
            continue
        outcome.append({'position_key': key.hex(), 'reach': row['reach'],
                        'loss_pop': context['loss_pop'], 'loss_taught': loss,
                        'gain': context['loss_pop'] - loss, 'answers': sorted(taught),
                        'undecidable': sorted(str(event) for event, state in states.items()
                                              if state == plans.UNKNOWN),
                        'remaining': [str(event) for event in remaining]})
    return outcome, dict(unavailable)


def summarise_plan(db, plan, surface):
    rows, unavailable = plan_rows(db, plan, surface)
    scope = surface.scope(plan)
    gains = [row['gain'] for row in rows]
    weighted = [row['reach'] * row['gain'] for row in rows]
    positive = [value for value in weighted if value > 0]
    burden = plan_burden(plan, unavailable)
    total = sum(weighted)
    return {
        'plan_id': plan['plan_id'], 'structure_id': plan['structure_id'],
        'side': plan['side'], 'transformations': plan['transformations'],
        'support': plan['support'], 'order_flexibility': plan['order_flexibility'],
        'movable_transformations': plan.get('movable_transformations', []),
        'scope_boards': len(scope), 'prescriptive_boards': len(rows),
        'unavailable': unavailable,
        'positions_replaced': len(rows),
        'decisions_replaced': sum(len(row['answers']) for row in rows),
        'reach_covered': sum(row['reach'] for row in rows),
        'ev_weighted_total': total,
        'median_gain': statistics.median(gains) if gains else None,
        'mean_gain': statistics.fmean(gains) if gains else None,
        'share_non_negative': (sum(1 for gain in gains if gain >= 0) / len(gains)
                               if gains else None),
        'top_board_share_of_positive_total': (max(positive) / sum(positive)
                                              if positive else None),
        'burden': burden, 'ev_per_burden': (total / burden['cost']) if burden['cost'] else None,
        'rows': rows,
    }


def plan_burden(plan, unavailable):
    """Multi-dimensional learning burden; never reported as a single opaque number."""
    kinds = {event[0] for event in plan['transformations']}
    burden = {
        'transformations': len(plan['transformations']),
        'conditions': 1 + len(kinds & plans.ACTIONABLE_KINDS),
        'exceptions': unavailable.get('no_strong_play_support', 0),
        'boards': 1,
        'exact_moves': 0,
        'condition_kinds': sorted(kinds & plans.ACTIONABLE_KINDS),
        'effect_only_kinds': sorted(kinds - plans.ACTIONABLE_KINDS),
    }
    burden['cost'] = (plans.WEIGHTS['transformations'] * burden['transformations']
                      + plans.WEIGHTS['conditions'] * burden['conditions']
                      + plans.WEIGHTS['exceptions'] * burden['exceptions']
                      + plans.WEIGHTS['boards'] * burden['boards'])
    return burden


def dedupe_plans(summaries):
    """Collapse plans that prescribe identically, and say how many were collapsed.

    Two plans can differ only in transformations that are undecidable from a board
    (a structural exit vs a file opening), which leaves their prescriptions identical.
    Counting them separately would inflate both the plan count and the burden.
    """
    seen, kept, duplicates = {}, [], []
    for row in summaries:
        signature = tuple(sorted((entry['position_key'], tuple(entry['answers']))
                                 for entry in row['rows']))
        if not signature:
            kept.append(row)
            continue
        if signature in seen:
            duplicates.append({'plan_id': row['plan_id'], 'same_as': seen[signature],
                               'family': row['structure_id']})
            continue
        seen[signature] = row['plan_id']
        kept.append(row)
    return kept, duplicates


def plan_frontier(db, surface, summaries, k_max=10):
    """Greedy plan selection under the max-not-sum rule, as the frozen frontier does."""
    candidates = [row for row in summaries if row['prescriptive_boards']]
    chosen, rows = [], []
    for step in range(k_max):
        best, best_value, best_state = None, None, None
        for row in candidates:
            if row['plan_id'] in {entry['plan_id'] for entry in chosen}:
                continue
            state = {}
            for entry in chosen:
                for board in entry['rows']:
                    state[board['position_key']] = min(
                        state.get(board['position_key'], 1e9), board['loss_taught'])
            for board in row['rows']:
                state[board['position_key']] = min(
                    state.get(board['position_key'], 1e9), board['loss_taught'])
            value = 0.0
            for board in row['rows']:
                context = surface.context(bytes.fromhex(board['position_key']))
                if context is None:
                    continue
                value += surface.reach.get(bytes.fromhex(board['position_key']),
                                           {}).get('reach', 0.0) * max(
                    0.0, context['loss_pop'] - state[board['position_key']])
            if best_value is None or value > best_value:
                best, best_value, best_state = row, value, state
        if best is None:
            break
        chosen.append(best)
        rows.append({'k': len(chosen), 'plan_id': best['plan_id'],
                     'structure_id': best['structure_id'],
                     'coverage_value': best_value,
                     'marginal': best_value - (rows[-1]['coverage_value'] if rows else 0.0),
                     'burden': sum(entry['burden']['cost'] for entry in chosen),
                     'positions': len(best_state)})
    return rows


def class_comparison(db, structures, surface, plan_summaries, k_decisions=25, k_exceptions=20,
                     surface_keys=None):
    """The five curricula of this phase on one shared surface, max-not-sum overlap.

    Value retained at a board is the loss of the *best* covering item, never the sum
    of its items: two plans that both cover a board cannot both be paid for it.
    """
    decision_items = curriculum.decision_items(db, surface.source, 6, top=k_decisions,
                                               criterion='expert')
    exception_items = curriculum.exception_items(db, surface.source, 6, top=k_exceptions)
    rule_items = rules.as_items(db, [rule for rule in rules.derive_all(db, surface.source)
                                     if rule['kind'] == 'prescriptive' and rule['answers']],
                                surface.source)
    plan_items = [{'item_id': f"plan:{row['plan_id']}", 'keys': None, 'plan': row}
                  for row in plan_summaries]
    frontier = plan_frontier(db, surface, plan_summaries)
    frontier_ids = {row['plan_id'] for row in frontier}
    frontier_items = [item for item in plan_items
                      if item['plan']['plan_id'] in frontier_ids]

    # resolve each item to per-board taught losses
    def item_losses(item):
        """Per-board losses, restricted to the shared surface so classes are comparable.

        Without the restriction a class is paid for boards the other classes cannot
        reach at all, and a recovery fraction can exceed 1 while measuring nothing.
        """
        losses = {}
        if 'plan' in item:
            for row in item['plan']['rows']:
                losses[bytes.fromhex(row['position_key'])] = row['loss_taught']
            return losses
        answers = item.get('answers') or []
        for key in item['keys']:
            if surface_keys is not None and key not in surface_keys:
                continue
            loss = surface.loss(key, answers)
            if loss is not None:
                losses[key] = loss
        return losses

    classes = {
        'exact_decisions_only': decision_items,
        'fixed_family_rules': rule_items,
        'plan_conditioned': [item for item in plan_items],
        'plan_plus_exact_exceptions': plan_items + exception_items,
        'plan_frontier_selected': frontier_items or [],
        'engine_informed_ceiling': decision_items + rule_items + plan_items + exception_items,
    }
    out = []
    for name, items in classes.items():
        per_item = [(item, item_losses(item)) for item in items]
        per_item = [(item, losses) for item, losses in per_item if losses]
        boards = {key: None for item, losses in per_item for key in losses}
        retained, evaluated = 0.0, 0
        for key in boards:
            context = surface.context(key)
            if context is None:
                continue
            achieved = min((losses[key] for _item, losses in per_item if key in losses),
                           default=None)
            if achieved is None:
                continue
            evaluated += 1
            retained += surface.reach.get(key, {}).get('reach', 0.0) * max(
                0.0, context['loss_pop'] - achieved)
        burden = sum(item_burden(item) for item, _losses in per_item)
        positions = len({key for _item, losses in per_item for key in losses})
        out.append({'curriculum': name, 'items': len(per_item), 'positions': positions,
                    'evaluated_positions': evaluated, 'burden': burden,
                    'value_retained': retained,
                    'value_per_burden': (retained / burden) if burden else None,
                    'plan_boards': sum(len(losses) for _item, losses in per_item
                                       if 'plan' in _item)})
    denominator = family_surface_loss(db, structures, surface)
    for row in out:
        row['shared_denominator'] = denominator
        row['recovery_fraction'] = (row['value_retained'] / denominator) if denominator else None
    return out, frontier


def item_burden(item):
    if 'plan' in item:
        return item['plan']['burden']['cost']
    complexity = item.get('complexity') or {}
    return complexity.get('cost', 1.0)


def family_surface_keys(db, structures, surface):
    """Every board of the mature families under study, with data: the shared surface."""
    keys = set()
    for structure_id in structures:
        for row in plans.plan_scope(db, {'structure_id': structure_id, 'side': None},
                                    surface.source):
            keys.add(row['key'])
    return keys


def family_surface_loss(db, structures, surface):
    """One denominator for every class: the ordinary-play loss on the families' surface.

    It is defined by the mature families under study, not by whatever the classes
    happen to cover, so a class cannot enlarge or shrink its own denominator. It is
    also *not* the domain-wide loss, so these recovery fractions are not comparable
    with the domain-wide ones in the frozen experiment — that is stated in the report.
    """
    keys = family_surface_keys(db, structures, surface)
    total, covered = 0.0, 0
    for key in keys:
        context = surface.context(key)
        if context is None:
            continue
        covered += 1
        total += surface.reach.get(key, {}).get('reach', 0.0) * context['loss_pop']
    return total


def render(options):
    db = studydb.connect(options.db, create=False)
    surface = Surface(db, options.source)
    structures = options.structures or [row['structure_id'] for row in db.execute(
        'SELECT structure_id FROM structure_maturity WHERE source=? ORDER BY entry_mass DESC '
        'LIMIT 8', (options.source,))]
    vocabulary = plans.recurring_vocabulary(db, structures, source=options.source)
    oracle = [plans.family_oracle(db, structure_id, options.source,
                                  vocabulary=vocabulary.get(structure_id))
              for structure_id in structures]
    derived = plans.derive_plans(db, structures, source=options.source, only_movable=True)
    if options.top_per_family:
        top = defaultdict(list)
        for plan in sorted(derived, key=lambda plan: -plan['support']):
            bucket = top[(plan['structure_id'], plan['side'])]
            if len(bucket) < options.top_per_family:
                bucket.append(plan)
        chosen = [plan for bucket in top.values() for plan in bucket]
    else:
        # every mined plan is evaluated: ranking by support alone would select the
        # shortest plans, which are already exhausted on most of their own scope
        chosen = derived
    summaries = [summarise_plan(db, plan, surface) for plan in chosen]
    unique, duplicates = dedupe_plans(summaries)
    surface_keys = family_surface_keys(db, structures, surface)
    comparison, frontier = class_comparison(db, structures, surface, unique,
                                            surface_keys=surface_keys)

    lines = ['# Plan-compression prototype (Scandinavian mature families)\n']
    lines.append('Prototype only: the existing mature families, existing data, no new '
                 'engine compute, and the frozen family-rule experiment untouched. '
                 'Plans are mined from strong-play trajectories; engine evaluations are '
                 'used to value moves a plan already prescribes, never to choose them.\n')

    lines.append('## 1. Family-oracle upper bounds\n')
    lines.append('Three distinct levels. The oracle is an **upper bound, not a learnable '
                 'item**: it may pick a different action on every board. Two oracle '
                 'variants are reported — the loose one (any move strong players played '
                 'at that board) and the tight one (restricted to the family\'s recurring '
                 'transformation vocabulary mined from those same trajectories).\n')
    lines.append('| family | boards | unavailable | ordinary-play loss | oracle EV | oracle '
                 'recovery | recurring-vocabulary EV | recurring boards | fixed-rule EV | '
                 'headroom (oracle - fixed) |')
    lines.append('|' + '---|' * 11)
    for report in oracle:
        lines.append(
            f"| `{report['structure_id'][:8]}` | {report['boards_evaluated']} | "
            f"{report['boards_unavailable']} | {report['ordinary_play_loss']:.6f} | "
            f"{report['oracle']['ev_reach_weighted']:.6f} | "
            f"{report['oracle_recovery']:.4f} | "
            f"{report['oracle_recurring']['ev_reach_weighted']:.6f} | "
            f"{report['oracle_recurring']['boards']} | "
            f"{report['fixed_rule']['ev_reach_weighted']:.6f} | "
            f"{report['headroom_oracle_minus_fixed']:.6f} |")
    lines.append('\nFamilies whose oracle recovers little are poor candidates for '
                 'prescriptive compression; those are flagged below.\n')

    lines.append('## 2. Discovered plan objects\n')
    lines.append(f'Within the {len(surface_keys)} boards of the shared surface, '
                 f'{len(summaries)} evaluated plans collapse to {len(unique)} that '
                 f'prescribe identically; {len(duplicates)} are duplicates differing only '
                 f'in transformations that cannot be read off a board (a structural exit '
                 f'versus a file opening). Duplicates are collapsed before any comparison, '
                 f'so neither the plan count nor the burden is inflated.\n')
    lines.append(f'{len(derived)} plans were mined across {len(structures)} families '
                 f'(deduplicated by their own transformations); {len(chosen)} are '
                 f'evaluated here. Every plan is evaluated rather than a support-ranked '
                 f'subset: support selects short plans, which tend to be already complete '
                 f'on their own scope, so a support cut would quietly decide the result.\n')
    lines.append('| plan | family | side | support | order flexibility | transformations |')
    lines.append('|' + '---|' * 6)
    for row in sorted(summaries, key=lambda row: -row['support'])[:12]:
        events = ', '.join(_event_text(event) for event in row['transformations'])
        lines.append(f"| `{row['plan_id']}` | `{row['structure_id'][:8]}` | "
                     f"{row['side'].replace('_to_move', '')} | {row['support']:.3f} | "
                     f"{row['order_flexibility']:.2f} | {events} |")

    lines.append('\n## 3. State-transition representation (examples)\n')
    for row in sorted(summaries, key=lambda row: -row['support'])[:4]:
        lines.append(f"Plan `{row['plan_id']}` ({row['structure_id'][:8]}, "
                     f"{row['side'].replace('_to_move', '')}):\n")
        for event in row['transformations']:
            lines.append(f"    - transformation {_event_text(event)} "
                         f"[{_event_state_kind(event)}]")
        lines.append(f"    ordering: canonical order with flexibility "
                     f"{row['order_flexibility']:.2f}")
        lines.append(f"    completion: state test on the board, never an engine query")
        lines.append(f"    scope: {row['scope_boards']} boards, "
                     f"prescribes on {row['prescriptive_boards']}")
        lines.append('')

    lines.append('## 4. How many boards each plan can actually prescribe on\n')
    lines.append('| plan | family | scope boards | prescriptive boards | no actionable '
                 'transformation | no strong-play support | taught move not evaluated |')
    lines.append('|' + '---|' * 7)
    for row in sorted(summaries, key=lambda row: -row['prescriptive_boards']):
        reasons = row['unavailable']
        lines.append(f"| `{row['plan_id']}` | `{row['structure_id'][:8]}` | "
                     f"{row['scope_boards']} | {row['prescriptive_boards']} | "
                     f"{reasons.get('no_actionable_transformation', 0)} | "
                     f"{reasons.get('no_strong_play_support', 0)} | "
                     f"{reasons.get('taught_moves_not_evaluated', 0)} |")

    lines.append('\n## 5. Plan EV vs fixed rules vs exact decisions\n')
    lines.append('| curriculum | items | positions | evaluated | burden | value retained | '
                 'recovery | value per burden |')
    lines.append('|' + '---|' * 8)
    for row in comparison:
        lines.append(f"| {row['curriculum']} | {row['items']} | {row['positions']} | "
                     f"{row['evaluated_positions']} | {row['burden']:.1f} | "
                     f"{row['value_retained']:.6f} | "
                     f"{(('%.4f' % row['recovery_fraction']) if row['recovery_fraction'] is not None else '—')} | "
                     f"{(('%.6f' % row['value_per_burden']) if row['value_per_burden'] else '—')} |")
    lines.append(f"\nShared denominator (ordinary-play loss over the mature families' "
                 f"boards): {comparison[0]['shared_denominator']:.6f}. This is the "
                 f"family surface, not the whole domain, so these recovery fractions are "
                 f"not comparable with the domain-wide figures in the frozen experiment.\n")

    lines.append('## 5b. Plan frontier (max-not-sum overlap)\n')
    lines.append('Greedy selection under the same overlap rule the frozen frontier uses: '
                 'the value of a set is the best covering plan per board, never the sum.\n')
    lines.append('| k plans | plan added | family | coverage value | marginal | burden | positions |')
    lines.append('|' + '---|' * 7)
    for row in frontier:
        lines.append(f"| {row['k']} | `{row['plan_id']}` | `{row['structure_id'][:8]}` | "
                     f"{row['coverage_value']:.6f} | {row['marginal']:.6f} | "
                     f"{row['burden']:.1f} | {row['positions']} |")

    lines.append('\n## 6. Board-level gain distributions\n')
    lines.append('| plan | family | boards | median gain | mean gain | share non-negative | '
                 'top board share of positive total |')
    lines.append('|' + '---|' * 7)
    for row in sorted(summaries, key=lambda row: -(row['ev_weighted_total'] or 0)):
        if not row['prescriptive_boards']:
            continue
        top_share = row['top_board_share_of_positive_total']
        lines.append(f"| `{row['plan_id']}` | `{row['structure_id'][:8]}` | "
                     f"{row['prescriptive_boards']} | {row['median_gain']:+.4f} | "
                     f"{row['mean_gain']:+.4f} | {row['share_non_negative']:.2f} | "
                     f"{(('%.2f' % top_share) if top_share is not None else '—')} |")

    lines.append('\n## 7. Compression\n')
    lines.append('| plan | exact positions replaced | decisions replaced | reach covered | '
                 'burden | EV recovered | EV / burden | median board EV gain | '
                 'non-negative share |')
    lines.append('|' + '---|' * 9)
    for row in sorted(summaries, key=lambda row: -(row['ev_weighted_total'] or 0)):
        if not row['prescriptive_boards']:
            continue
        lines.append(f"| `{row['plan_id']}` | {row['positions_replaced']} | "
                     f"{row['decisions_replaced']} | {row['reach_covered']:.5f} | "
                     f"{row['burden']['cost']:.1f} | {row['ev_weighted_total']:.6f} | "
                     f"{(('%.6f' % row['ev_per_burden']) if row['ev_per_burden'] else '—')} | "
                     f"{row['median_gain']:+.4f} | {row['share_non_negative']:.2f} |")

    lines.append('\n## 8. Failure cases\n')
    failures = []
    for row in summaries:
        if not row['prescriptive_boards']:
            failures.append((row, 'never prescribes: no board had an actionable '
                                  'transformation with strong-play support'))
            continue
        if row['median_gain'] is not None and row['median_gain'] < 0:
            failures.append((row, 'median board gain is negative: the plan is worse than '
                                  'ordinary play on the typical board'))
        elif (row['top_board_share_of_positive_total'] or 0) > 0.9:
            failures.append((row, 'value concentrated: one board supplies over 90% of the '
                                  'positive gain'))
        if row['unavailable'].get('no_strong_play_support', 0) > row['prescriptive_boards']:
            failures.append((row, 'mostly non-prescriptive: strong-play support is absent '
                                  'on most of its scope'))
    if not failures:
        lines.append('No plan in the evaluated sample failed these tests.\n')
    for row, reason in failures:
        events = ', '.join(_event_text(event) for event in row['transformations'])
        median = row['median_gain']
        median_text = 'n/a' if median is None else format(median, '+.4f')
        lines.append(f"* `{row['plan_id']}` (`{row['structure_id'][:8]}`, {events}): {reason}; "
                     f"prescribes on {row['prescriptive_boards']} of {row['scope_boards']} "
                     f"boards, median gain {median_text}")

    (options.out / 'plan-compression-prototype.md').write_text('\n'.join(lines) + '\n')
    (options.out / 'plan-summaries.json').write_text(json.dumps(
        [{key: value for key, value in row.items() if key != 'rows'} for row in summaries],
        indent=1, default=str))
    print(json.dumps({
        'report': str(options.out / 'plan-compression-prototype.md'),
        'plans_mined': len(derived), 'plans_evaluated': len(summaries),
        'oracle': [{'structure_id': report['structure_id'],
                    'boards': report['boards_evaluated'],
                    'oracle_ev': report['oracle']['ev_reach_weighted'],
                    'oracle_recurring_ev': report['oracle_recurring']['ev_reach_weighted'],
                    'fixed_ev': report['fixed_rule']['ev_reach_weighted']}
                   for report in oracle],
        'comparison': [{key: (round(value, 6) if isinstance(value, float) else value)
                        for key, value in row.items()} for row in comparison],
        'duplicates_collapsed': len(duplicates),
        'frontier': frontier,
        'plan_evs': [{'plan_id': row['plan_id'], 'structure_id': row['structure_id'],
                      'boards': row['prescriptive_boards'],
                      'ev': round(row['ev_weighted_total'], 6),
                      'median_gain': (round(row['median_gain'], 5)
                                      if row['median_gain'] is not None else None)}
                     for row in sorted(summaries, key=lambda row: -(row['ev_weighted_total'] or 0))[:10]],
    }, indent=1))
    db.close()


def _event_text(event):
    kind = event[0]
    if kind in ('pawn_move', 'piece_move'):
        return f"{kind} {event[1]}-{event[2]}"
    if kind == 'capture':
        return f"capture {event[1]}x{event[2]} {event[3]}-{event[4]}"
    if kind == 'castle':
        return f"castle {event[1]}{event[2]}"
    if kind == 'file_open':
        return f"file_open {event[1]}"
    return kind


def _event_state_kind(event):
    kind = event[0]
    if kind in plans.ACTIONABLE_KINDS:
        return 'decidable from the board'
    if kind == 'file_open':
        return 'decidable only when the file is already open'
    return 'not decidable from a single board (effect, not state)'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=ROOT / 'data' / 'atlas-analysis.sqlite')
    parser.add_argument('--out', type=Path, default=ROOT / 'analysis')
    parser.add_argument('--source', default='local2200')
    parser.add_argument('--top-per-family', type=int, default=None)
    parser.add_argument('--structures', nargs='*')
    options = parser.parse_args()
    render(options)


if __name__ == '__main__':
    main()
