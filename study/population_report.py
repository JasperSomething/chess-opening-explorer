"""Population-enrichment report: acquisition actuals, joint coverage, and the
distribution of template value across boards.

Reported after the run, from the database, so every number is an observation rather
than a projection. Coverage is reported both as raw board counts and weighted by
`reach_flow`, because a position nobody reaches cannot earn curriculum value.
"""
import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sqlite3  # noqa: E402

from study import curriculum, db as studydb, rules, template_value  # noqa: E402

BARS = (0.30, 0.40, 0.50, 0.60)


def acquisition_actuals(*log_paths):
    totals = {'attempted': 0, 'success': 0, 'skipped_cached': 0, 'failed': 0,
              'games_added': 0, 'moves_added': 0, 'logs': []}
    for path in log_paths:
        path = Path(path)
        if not path.exists():
            continue
        counts = {'success': 0, 'skipped_cached': 0, 'failed': 0}
        with open(path, newline='') as handle:
            for row in csv.DictReader(handle):
                counts[row['outcome']] = counts.get(row['outcome'], 0) + 1
                if row['outcome'] == 'success':
                    totals['games_added'] += int(row['snapshot_games'] or 0)
                    totals['moves_added'] += int(row['moves'] or 0)
        totals['attempted'] += counts['success'] + counts['failed']
        totals['success'] += counts['success']
        totals['skipped_cached'] += counts['skipped_cached']
        totals['failed'] += counts['failed']
        totals['logs'].append({'path': str(path), **counts})
    return totals


def joint_coverage(db, source='local2200', bars=BARS):
    """Population / engine / joint coverage over the analysis domain, raw and reach-weighted."""
    domain = {row['position_key']: row['fen'] for row in
              db.execute('SELECT position_key, fen FROM position')}
    reach = curriculum.load_reach(db, source)
    evals = curriculum.load_evals(db)
    population = {row['position_key'] for row in db.execute(
        'SELECT DISTINCT position_key FROM move_source WHERE source IN (?,?)',
        curriculum.POPULATION_PREFERENCE)}
    engine = {key for key, fen in domain.items() if evals.get(fen)}
    both = population & engine
    total_reach = sum(value.get('reach', 0.0) for value in reach.values())
    report = {
        'domain_positions': len(domain),
        'population_positions': len(population & set(domain)),
        'engine_positions': len(engine),
        'joint_positions': len(both),
        'total_reach': total_reach,
        'population_reach_share': sum(reach.get(k, {}).get('reach', 0.0)
                                      for k in population) / total_reach if total_reach else None,
        'engine_reach_share': sum(reach.get(k, {}).get('reach', 0.0)
                                  for k in engine) / total_reach if total_reach else None,
        'joint_reach_share': sum(reach.get(k, {}).get('reach', 0.0)
                                 for k in both) / total_reach if total_reach else None,
    }
    # frozen template sample
    sample = list(db.execute('SELECT structure_id, position_key, weight FROM template_sample'))
    report['template_sample'] = {
        'boards': len(sample),
        'with_engine': sum(1 for row in sample if evals.get(domain.get(row['position_key']))),
        'with_population': sum(1 for row in sample if row['position_key'] in population),
        'with_both': sum(1 for row in sample if row['position_key'] in both),
    }
    # prescriptive rule scopes per frozen bar
    report['bars'] = {}
    for bar in bars:
        derived = rules.derive_all(db, source=source, settings={'support_floor': bar})
        per_rule = []
        for rule in derived:
            if rule['kind'] != 'prescriptive' or not rule['answers']:
                continue
            scope = rules.rule_scope(db, rule, source)
            scope_reach = sum(reach.get(key, {}).get('reach', 0.0) for key in scope)
            per_rule.append({
                'rule_id': rule['rule_id'], 'structure_id': rule['structure_id'],
                'answers': rule['answers'], 'scope_boards': len(scope),
                'with_engine': sum(1 for k in scope if evals.get(domain.get(k))),
                'with_population': sum(1 for k in scope if k in population),
                'with_both': sum(1 for k in scope if k in both),
                'scope_reach': scope_reach,
                'scope_reach_covered': sum(reach.get(k, {}).get('reach', 0.0)
                                           for k in scope if k in both),
            })
        report['bars'][f'{bar:.2f}'] = {
            'prescriptive_rules': len(per_rule),
            'scope_boards': sum(row['scope_boards'] for row in per_rule),
            'boards_with_engine': sum(row['with_engine'] for row in per_rule),
            'boards_with_population': sum(row['with_population'] for row in per_rule),
            'boards_with_both': sum(row['with_both'] for row in per_rule),
            'rules_with_ev': sum(1 for row in per_rule if row['with_both']),
            'scope_reach': sum(row['scope_reach'] for row in per_rule),
            'scope_reach_covered': sum(row['scope_reach_covered'] for row in per_rule),
            'rules': per_rule,
        }
    return report


def prescriptive_rules_all_bars(db, source='local2200', bars=BARS):
    """Every prescriptive rule that qualifies at any frozen bar, deduplicated.

    The distribution question ('broadly useful or carried by a handful of boards?')
    is only answerable on the widest rule scopes, which appear at the lower bars, so
    restricting this to the default bar would hide the evidence.
    """
    seen = {}
    for bar in bars:
        for rule in rules.derive_all(db, source=source, settings={'support_floor': bar}):
            if rule['kind'] != 'prescriptive' or not rule['answers']:
                continue
            entry = seen.setdefault(rule['rule_id'], dict(rule, bars=[]))
            entry['bars'].append(f'{bar:.2f}')
    return list(seen.values())


def render(options):
    db = studydb.connect(options.db, create=False)
    actuals = acquisition_actuals(*options.logs)
    coverage = joint_coverage(db, bars=options.bars)
    candidates = prescriptive_rules_all_bars(db, bars=options.bars)
    value = template_value.per_burden_dimension(db, candidates)
    bars_of = {rule['rule_id']: ','.join(rule['bars']) for rule in candidates}
    for row in value['rules']:
        row['bars'] = bars_of.get(row['rule_id'], '')

    lines = ['# Population enrichment, measured after the run\n']
    lines.append('Nothing in this report is projected: every figure is read back from the '
                 'cache and the analysis database after the acquisition.\n')
    lines.append('## Acquisition actuals\n')
    lines.append('| requests attempted | successful | skipped (already cached) | failed | games added | moves added |')
    lines.append('|---|---|---|---|---|---|')
    lines.append(f"| {actuals['attempted']} | {actuals['success']} | {actuals['skipped_cached']} | "
                 f"{actuals['failed']} | {actuals['games_added']:,} | {actuals['moves_added']:,} |")
    for entry in actuals['logs']:
        lines.append(f"\n* `{entry['path']}`: {entry['success']} success, "
                     f"{entry['skipped_cached']} skipped as cached, {entry['failed']} failed")

    lines.append('\n## Joint coverage over the analysis domain\n')
    lines.append('| measure | positions | share of reach_flow |')
    lines.append('|---|---|---|')
    share = lambda value: ('%.4f' % value) if value is not None else '—'
    lines.append(f"| analysis domain | {coverage['domain_positions']:,} | 1.0000 |")
    lines.append(f"| population positions | {coverage['population_positions']:,} | "
                 f"{share(coverage['population_reach_share'])} |")
    lines.append(f"| engine-evaluated positions | {coverage['engine_positions']:,} | "
                 f"{share(coverage['engine_reach_share'])} |")
    lines.append(f"| **engine + population (joint)** | {coverage['joint_positions']:,} | "
                 f"**{share(coverage['joint_reach_share'])}** |")

    sample = coverage['template_sample']
    lines.append('\n## Frozen template sample (201 boards)\n')
    lines.append('| boards | with engine evaluation | with population | with both |')
    lines.append('|---|---|---|---|')
    lines.append(f"| {sample['boards']} | {sample['with_engine']} | {sample['with_population']} | "
                 f"**{sample['with_both']}** |")

    lines.append('\n## Prescriptive-rule joint coverage per frozen condition\n')
    lines.append('| bar | rules | scope boards | engine | population | both | rules with EV | scope reach covered |')
    lines.append('|---|---|---|---|---|---|---|---|')
    for bar, entry in coverage['bars'].items():
        lines.append(f"| {bar} | {entry['prescriptive_rules']} | {entry['scope_boards']} | "
                     f"{entry['boards_with_engine']} | {entry['boards_with_population']} | "
                     f"**{entry['boards_with_both']}** | {entry['rules_with_ev']} | "
                     f"{share(entry['scope_reach_covered'] / entry['scope_reach'] if entry['scope_reach'] else None)} |")

    lines.append('\n## Template value: absolute recovered and per burden dimension\n')
    lines.append('Absolute recovered EV is the reach-weighted sum of per-board gains over the '
                 'rule scope. `unavailable` boards are those lacking the data needed to value '
                 'them; recognition-only templates stay at EV `NULL`.\n')
    available = [row for row in value['rules'] if row.get('ev_available')]
    if not available:
        lines.append('No prescriptive rule has an evaluable board yet, so every rule is '
                     '**EV unavailable** rather than zero-valued.\n')
    else:
        header = ['rule', 'family', 'bars', 'scope', 'boards evaluated',
                  'unavailable (no eval / no population / taught move not evaluated)',
                  'EV recovered (reach-weighted)']
        dimensions = ['exact_moves', 'flexible_slots', 'conditions', 'exceptions', 'boards', 'cost']
        header += [f'EV per {d}' for d in dimensions]
        lines.append('| ' + ' | '.join(header) + ' |')
        lines.append('|' + '---|' * len(header))
        for row in available:
            reasons = row.get('unavailable_reasons') or {}
            cells = [f"`{row['rule_id'][:8]}`", f"`{row['structure_id'][:8]}`",
                     row.get('bars', ''), str(row.get('scope_boards', '—')),
                     str(row['boards_evaluated']),
                     f"{row['boards_unavailable']} ({reasons.get('no_evaluation', 0)} / "
                     f"{reasons.get('no_population', 0)} / "
                     f"{reasons.get('taught_moves_not_evaluated', 0)})",
                     '%.5f' % row['ev_weighted_total']]
            cells += [('%.5f' % row[f'ev_per_{d}']) if row.get(f'ev_per_{d}') is not None else '—'
                      for d in dimensions]
            lines.append('| ' + ' | '.join(cells) + ' |')
        lines.append('\n### Pooled across prescriptive rules\n')
        lines.append('| burden dimension | EV recovered | burden | EV per burden |')
        lines.append('|---|---|---|---|')
        for dimension, entry in value['pooled_per_dimension'].items():
            lines.append(f"| {dimension} | {entry['ev']:.5f} | {entry['burden']:.0f} | "
                         f"{('%.5f' % entry['ev_per_burden']) if entry['ev_per_burden'] else '—'} |")

    lines.append('\n## Distribution of board-level gains\n')
    lines.append('A mean alone cannot say whether a template is broadly useful or whether a '
                 'handful of boards produce its value, so the shape is reported: the share of '
                 'boards with positive gain, the quantiles, and the share of all *positive* gain '
                 '(reach-weighted) contributed by the single best board. Concentration is '
                 'measured against the positive gains, because dividing by the net total would '
                 'let losing boards push the top board above 100% of it.\n')
    if not available:
        lines.append('No distribution is available yet: no board carries both an evaluation and '
                     'a population distribution inside any prescriptive scope.\n')
    else:
        lines.append('| rule | family | bars | boards | positive share | min | p25 | median | p75 | p90 | '
                     'max | top board share | top-3 share |')
        lines.append('|' + '---|' * 13)
        for row in available:
            stats = row['distribution']
            lines.append(f"| `{row['rule_id'][:8]}` | `{row['structure_id'][:8]}` | {row.get('bars', '')} | {stats['boards']} | "
                         f"{stats['share_positive']:.2f} | {stats['min']:.4f} | {stats['p25']:.4f} | "
                         f"{stats['median']:.4f} | {stats['p75']:.4f} | {stats['p90']:.4f} | "
                         f"{stats['max']:.4f} | "
                         f"{('%.2f' % stats['top_board_share_of_positive_total']) if stats['top_board_share_of_positive_total'] is not None else '—'} | "
                         f"{('%.2f' % stats['top3_share_of_positive_total']) if stats['top3_share_of_positive_total'] is not None else '—'} |")
        lines.append('\nPer-board gains, rule by rule (reach, gain):\n')
        for row in available:
            detail = template_value.rule_board_gains(db, _rule_by_id(db, row['rule_id']))
            pairs = ', '.join(f"({board['reach']:.3f}, {board['gain']:+.4f})"
                              for board in sorted(detail['boards'],
                                                  key=lambda b: -b['reach']))
            lines.append(f"* `{row['rule_id'][:8]}`: {pairs}")

    (options.out / 'population-enrichment.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({'report': str(options.out / 'population-enrichment.md'),
                      'acquisition': {k: v for k, v in actuals.items() if k != 'logs'},
                      'joint': {k: v for k, v in coverage.items()
                                if k not in ('bars', 'template_sample')},
                      'template_sample': coverage['template_sample'],
                      'bars': {bar: {k: v for k, v in entry.items() if k != 'rules'}
                               for bar, entry in coverage['bars'].items()},
                      'rules_with_ev': [row['rule_id'] for row in available]}, indent=1))
    db.close()


def _rule_by_id(db, rule_id):
    for rule in rules.derive_all(db):
        if rule['rule_id'] == rule_id:
            return rule
    return {'rule_id': rule_id, 'structure_id': '?', 'kind': 'prescriptive',
            'answers': [], 'condition': {'role': None}, 'coverage': 0.0, 'n_exceptions': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=ROOT / 'data' / 'atlas-analysis.sqlite')
    parser.add_argument('--out', type=Path, default=ROOT / 'analysis')
    parser.add_argument('--logs', type=Path, nargs='*', default=[
        ROOT / 'analysis' / 'campaign' / 'acquire-log-t12.csv',
        ROOT / 'analysis' / 'campaign' / 'acquire-log-t3.csv'])
    parser.add_argument('--bars', type=float, nargs='*', default=list(BARS))
    options = parser.parse_args()
    render(options)


if __name__ == '__main__':
    main()
