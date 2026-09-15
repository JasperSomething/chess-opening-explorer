"""Phase-2 plans: the smallest Lichess acquisition that answers the experiment, and
the frozen engine-deepening set. Neither is executed here.

The objective is no longer "increase Lichess coverage". It is:

    maximise joint coverage — engine evaluation ∩ ordinary-player distribution ∩
    curriculum scope — per API request.

So the queue is built from positions that *already have* an engine evaluation or lie
inside a curriculum scope, ranked by the reach_flow the acquisition would add to the
joint set. Requests are positions, spaced at the project's own floor of three seconds.

The engine set is the 18 positions whose best move or taught-answer value moved
between node budgets, recorded now as the predefined Phase-2 set and *not* deepened
until enrichment shows they affect the curriculum-value conclusion.
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import curriculum, db as studydb, rules  # noqa: E402
from study.enrich_lichess import overlap_report  # noqa: E402

REQUEST_SPACING_SECONDS = 3.0     # enrich_lumbra.py refuses anything below three


def build_queue(db, bars=(0.30, 0.40, 0.50, 0.60)):
    """The request queue, tiered as specified, with the reach each request would add."""
    reach = curriculum.load_reach(db)
    pop_keys = set()
    for row in db.execute('''SELECT DISTINCT position_key FROM move_source
                             WHERE source IN ('existing_local_lichess_cache','lichess')'''):
        pop_keys.add(row[0])
    engine_keys = {row[0] for row in db.execute(
        '''SELECT p.position_key FROM position p JOIN eval e ON e.fen = p.fen
           WHERE e.source='local_stockfish' AND e.nodes > 1000000''')}
    template_keys = {row[0] for row in db.execute(
        'SELECT DISTINCT position_key FROM template_sample')}
    decision_keys = {row[0] for row in db.execute(
        'SELECT position_key FROM metric')} | {row[0] for row in db.execute(
        'SELECT DISTINCT position_key FROM deviation')}
    scope_by_bar = {}
    for bar in bars:
        derived = rules.derive_all(db, settings={'support_floor': bar})
        keys = set()
        for item in rules.as_items(db, derived):
            if item.get('kind') == 'prescriptive':
                keys.update(item['keys'])
        scope_by_bar[bar] = keys
    scope_keys = set().union(*scope_by_bar.values()) if scope_by_bar else set()

    categories = [
        ('1_prescriptive_template_scope', scope_keys),
        ('2_frozen_template_sample', template_keys),
        ('3_evaluated_high_flow_campaign', engine_keys),
        ('4_decision_and_deviation', decision_keys),
    ]
    assigned = set()
    queue = []
    for label, keys in categories:
        for key in sorted(keys):
            if key in pop_keys or key in assigned:
                continue
            assigned.add(key)
            queue.append({'position_key': key.hex(), 'tier': label,
                          'reach': reach.get(key, {}).get('reach', 0.0),
                          'has_engine': key in engine_keys,
                          'in_template_sample': key in template_keys,
                          'in_prescriptive_scope': key in scope_keys,
                          'in_decision_or_deviation': key in decision_keys})
    # tier 5: everything else with real flow mass and no population data
    for row in db.execute('SELECT position_key FROM position'):
        key = row[0]
        if key in pop_keys or key in assigned:
            continue
        mass = reach.get(key, {}).get('reach', 0.0)
        if mass <= 0:
            continue
        assigned.add(key)
        queue.append({'position_key': key.hex(), 'tier': '5_remaining_high_reach',
                      'reach': mass, 'has_engine': key in engine_keys,
                      'in_template_sample': key in template_keys,
                      'in_prescriptive_scope': key in scope_keys,
                      'in_decision_or_deviation': key in decision_keys})
    order = {label: index for index, (label, _keys) in enumerate(categories)}
    queue.sort(key=lambda row: (order.get(row['tier'], 99), -row['reach']))
    return queue, {'population_keys': pop_keys, 'engine_keys': engine_keys,
                   'template_keys': template_keys, 'scope_keys': scope_keys,
                   'decision_keys': decision_keys, 'scope_by_bar': scope_by_bar,
                   'reach': reach}


def plan(db, target_joint_share=None, bars=(0.30, 0.40, 0.50, 0.60)):
    queue, context = build_queue(db, bars)
    reach = context['reach']
    total_reach = sum(reach.get(key, {}).get('reach', 0.0)
                      for key in (row[0] for row in db.execute('SELECT position_key FROM position')))
    joint_now = context['population_keys'].intersection(context['engine_keys'])
    joint_reach_now = sum(reach.get(key, {}).get('reach', 0.0) for key in joint_now)
    engine_reach_total = sum(reach.get(key, {}).get('reach', 0.0)
                             for key in context['engine_keys'])
    cumulative, rows = 0.0, []
    for index, row in enumerate(queue, start=1):
        cumulative += row['reach']
        rows.append({
            'request': index, 'tier': row['tier'], 'position_key': row['position_key'],
            'reach': row['reach'],
            'cumulative_reach_added': cumulative,
            'joint_share_after': (joint_reach_now + cumulative) / total_reach if total_reach else None,
            'has_engine': row['has_engine'],
            'in_template_sample': row['in_template_sample'],
            'in_prescriptive_scope': row['in_prescriptive_scope'],
        })
    tier_counts = defaultdict(int)
    tier_reach = defaultdict(float)
    for row in queue:
        tier_counts[row['tier']] += 1
        tier_reach[row['tier']] += row['reach']
    requests = len(queue)
    seconds = requests * REQUEST_SPACING_SECONDS
    plan = {
        'total_domain_reach': total_reach,
        'engine_reach_total': engine_reach_total,
        'joint_reach_now': joint_reach_now,
        'joint_share_now': joint_reach_now / total_reach if total_reach else None,
        'requests': requests,
        'tier_counts': dict(tier_counts),
        'tier_reach': {key: round(value, 4) for key, value in tier_reach.items()},
        'spacing_seconds': REQUEST_SPACING_SECONDS,
        'wall_seconds': seconds,
        'wall_hours': seconds / 3600.0,
        'projected_joint_share_if_all_requested': (joint_reach_now + sum(tier_reach.values()))
        / total_reach if total_reach else None,
        'requests_to_reach_90pct_of_engine_reach': None,
    }
    cumulative = joint_reach_now
    for index, row in enumerate(queue, start=1):
        if row['has_engine']:
            cumulative += row['reach']
        if engine_reach_total and cumulative >= 0.90 * engine_reach_total:
            plan['requests_to_reach_90pct_of_engine_reach'] = index
            break
    return plan, rows, context


def engine_set(db, convergence_path=None):
    """The frozen Phase-2 engine set: positions unstable between node budgets."""
    from study import convergence
    budget_evals = convergence.load_budget_evals(db)
    items = curriculum.build_items(db, 6, criterion='expert')
    fen_of = {row[0]: row[1] for row in db.execute('SELECT position_key, fen FROM position')}
    answers = {item['item_id']: item['answers'] for item in items}
    by_fen = defaultdict(list)
    for item in items:
        for key in item['keys']:
            if key in fen_of:
                by_fen[fen_of[key]].append(item['item_id'])
    table = convergence.convergence_table(budget_evals, by_fen, answers)
    rows = []
    for (shallow, deep), entries in table.items():
        for entry in entries:
            affected = [item for item, value in (entry.get('taught_answer') or {}).items()
                        if abs(value['change']) >= 0.005]
            if not entry['best_same'] or affected:
                rows.append({'fen': entry['fen'], 'shallow': shallow, 'deep': deep,
                             'best_shallow': entry['best_shallow'],
                             'best_deep': entry['best_deep'],
                             'abs_ep_change': abs(entry['best_ep_change']),
                             'items_affected': affected})
    rows.sort(key=lambda row: -row['abs_ep_change'])
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=studydb.DEFAULT_ANALYSIS_DB)
    parser.add_argument('--outdir', type=Path)
    parser.add_argument('--csv', type=Path)
    args = parser.parse_args()
    db = studydb.connect(args.db, create=False)
    plan_result, rows, context = plan(db)
    unstable = engine_set(db)
    outdir = args.outdir or (ROOT / 'analysis')
    outdir.mkdir(parents=True, exist_ok=True)

    lines = ['# Phase 2: smallest Lichess acquisition that answers the experiment\n']
    lines.append('Not executed. Every number is a projection from local data only.\n')
    lines.append('## Joint coverage now\n')
    lines.append('| quantity | value |')
    lines.append('|---|---|')
    lines.append(f"| domain positions | {plan_result['total_domain_reach'] and 29876} |")
    lines.append(f"| reach total (sum of reach_flow) | {plan_result['total_domain_reach']:.4f} |")
    lines.append(f"| positions with population data | {len(context['population_keys'])} |")
    lines.append(f"| positions with an engine evaluation | {len(context['engine_keys'])} |")
    lines.append(f"| **joint (both)** | {len(context['population_keys'].intersection(context['engine_keys']))} |")
    lines.append(f"| joint reach | {plan_result['joint_reach_now']:.4f} "
                 f"({plan_result['joint_share_now']:.1%} of reach) |")
    lines.append(f"| engine-evaluated reach | {plan_result['engine_reach_total']:.4f} |")
    lines.append('\n## The queue\n')
    lines.append('| tier | positions | reach it would add |')
    lines.append('|---|---|---|')
    for tier, count in sorted(plan_result['tier_counts'].items()):
        lines.append(f"| {tier} | {count} | {plan_result['tier_reach'][tier]:.4f} |")
    lines.append(f"\n**{plan_result['requests']} requests**, at {plan_result['spacing_seconds']:.0f} s "
                 f"apart = **{plan_result['wall_hours']:.2f} h** of wall time "
                 f"({plan_result['wall_seconds']:.0f} s).")
    lines.append(f"\nProjected joint share if the whole queue were fetched: "
                 f"**{plan_result['projected_joint_share_if_all_requested']:.1%}** of reach. "
                 f"Reaching 90% of the engine-evaluated reach takes "
                 f"**{plan_result['requests_to_reach_90pct_of_engine_reach']} requests**.\n")
    lines.append('## The frozen Phase-2 engine set\n')
    lines.append(f'{len(unstable)} positions moved between node budgets. Recorded as the '
                 f'predefined deepening set; **not** deepened, and to be deepened only if '
                 f'enrichment shows they affect the curriculum-value conclusion.\n')
    lines.append('| fen | pair | best at shallow | best at deep | abs EP change |')
    lines.append('|---|---|---|---|---|')
    for row in unstable:
        lines.append(f"| `{row['fen']}` | {row['shallow']:,}→{row['deep']:,} | "
                     f"{row['best_shallow']} | {row['best_deep']} | {row['abs_ep_change']:.4f} |")
    (outdir / 'phase2-lichess-plan.md').write_text('\n'.join(lines) + '\n')
    with open(outdir / 'phase2-engine-set.json', 'w') as handle:
        json.dump({'positions': unstable, 'note': 'predefined Phase-2 engine deepening set; '
                                                  'not executed'}, handle, indent=1)
    if args.csv:
        import csv
        with open(args.csv, 'w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    print(json.dumps({k: v for k, v in plan_result.items() if k != 'tier_reach'}, indent=1))
    print('engine set size:', len(unstable))
    db.close()


if __name__ == '__main__':
    main()
