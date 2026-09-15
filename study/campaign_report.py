"""Render the post-campaign reports.

Three separate artefacts, in the order the review asked for them:

1. `analysis/template-value-experiment.md` — the template-value experiment, reported
   on its own before anything else changes;
2. `analysis/eval-convergence.md` — convergence across node budgets, the achieved
   depth distributions, and whether any curriculum item changes;
3. `analysis/curriculum-frontier-v2.md` — the six curriculum comparisons and the four
   frozen support conditions.

Nothing here changes a model, a threshold, a curriculum definition or an engine
budget: it reads what the campaign produced and applies criteria that were frozen
before it ran.
"""
import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import (campaign, convergence, curriculum, db as studydb,  # noqa: E402
                   remote, rules, templates)

BARS = (0.30, 0.40, 0.50, 0.60)


def campaign_accounting(db):
    rows = db.execute('''SELECT tier, COUNT(*) jobs, SUM(state='done') done,
                                SUM(nodes_budget) budget
                         FROM engine_job GROUP BY tier ORDER BY tier''').fetchall()
    totals = {'jobs': 0, 'done': 0, 'budget': 0}
    detail = []
    for row in rows:
        detail.append({'tier': row['tier'], 'jobs': row['jobs'], 'done': row['done'],
                       'budget': row['budget']})
        totals['jobs'] += row['jobs']
        totals['done'] += row['done']
        totals['budget'] += row['budget']
    nodes = db.execute('SELECT SUM(nodes_actual) FROM engine_job WHERE state=\'done\'').fetchone()[0]
    evals = db.execute('''SELECT COUNT(*), MIN(nodes), MAX(nodes) FROM eval
                          WHERE source='local_stockfish' AND nodes IS NOT NULL''').fetchone()
    return {'by_tier': detail, 'totals': totals, 'nodes_executed': nodes,
            'campaign_evals': evals[0], 'eval_nodes_min': evals[1], 'eval_nodes_max': evals[2]}


def sensitivity_table(db, source='local2200'):
    """Frozen coverage sensitivity: top single answer vs best <=2-answer set."""
    rows = []
    for record in templates.candidate_pool(db, source):
        boards = rules._family_boards(db, record['structure_id'], source)
        for role in sorted({board['role'] for board in boards if board['role']}):
            selected = [board for board in boards if board['role'] == role]
            total = sum(board['weight'] for board in selected) or 1.0
            mass = defaultdict(float)
            for board in selected:
                answers, _top = rules.board_answers(board['dist'], rules.DEFAULTS['answer_share'],
                                                    rules.DEFAULTS['answer_cap'],
                                                    rules.DEFAULTS['min_games'], board['games'])
                if answers:
                    mass[tuple(answers)] += board['weight']
            ordered = sorted(mass.items(), key=lambda pair: -pair[1])
            top1 = ordered[0][1] / total if ordered else 0.0
            best_two_moves = defaultdict(float)
            for board in selected:
                answers, _top = rules.board_answers(board['dist'], rules.DEFAULTS['answer_share'],
                                                    rules.DEFAULTS['answer_cap'],
                                                    rules.DEFAULTS['min_games'], board['games'])
                for uci in answers:
                    best_two_moves[uci] += board['weight']
            best_two = sum(sorted(best_two_moves.values(), reverse=True)[:2]) / total
            rows.append({'structure_id': record['structure_id'], 'role': role,
                         'top_single_pattern_share': top1, 'best_two_move_share': best_two,
                         'patterns': len(ordered)})
    return rows


def render(options):
    db = studydb.connect(options.db, create=False)
    outdir = options.outdir or (ROOT / 'analysis')
    outdir.mkdir(parents=True, exist_ok=True)

    accounting = campaign_accounting(db)
    depth = convergence.depth_distribution(db)
    budget_evals = convergence.load_budget_evals(db)

    # ---------------------------------------------------------- template values
    lines = ['# Template-value experiment (reported before any model change)\n']
    lines.append('Rules are derived from strong play only, scoped to the side to move, and '
                 'only prescriptive rules may earn expected value. Support conditions are '
                 'the frozen 30/40/50/60 bars; nothing below is tuned on the results.\n')
    lines.append('## Rule inventory per frozen support bar\n')
    lines.append('| bar | prescriptive rules | recognition-only rules | families with a '
                 'prescriptive rule | burden of prescriptive rules | recognition coverage |')
    lines.append('|---|---|---|---|---|---|')
    per_bar = {}
    for bar in BARS:
        derived = rules.derive_all(db, settings={'support_floor': bar})
        items = rules.as_items(db, derived)
        prescriptive = [item for item in items if item.get('kind') == 'prescriptive']
        recognition = [item for item in items if item.get('kind') != 'prescriptive']
        families = {item['scope_id'].split(':')[0] for item in prescriptive}
        coverage = curriculum.recognition(db, prescriptive or [{'keys': []}]) if prescriptive else 0.0
        per_bar[bar] = {'rules': derived, 'items': items, 'prescriptive': prescriptive,
                        'recognition_only': recognition, 'families': families,
                        'coverage': coverage}
        lines.append(f'| {bar:.2f} | {len(prescriptive)} | {len(recognition)} | {len(families)} | '
                     f'{sum(i["complexity"]["cost"] for i in prescriptive):.0f} | {coverage:.3f} |')

    lines.append('\n## Why each bar lands where it does (frozen sensitivity table)\n')
    lines.append('`top single pattern` is the flow share of the most common board-answer '
                 'pattern; `best two moves` is the share covered by the two strongest moves. '
                 'A prescriptive rule needs its answer set to hold on the bar: a single-move '
                 'rule on the mass share, a multi-move rule on strict board-for-board matching.\n')
    sensitivity = sensitivity_table(db)
    lines.append('| family | side to move | top single pattern | best two moves | patterns |')
    lines.append('|---|---|---|---|---|')
    for row in sorted(sensitivity, key=lambda r: -r['top_single_pattern_share']):
        lines.append(f"| `{row['structure_id'][:8]}` | {row['role']} | "
                     f"{row['top_single_pattern_share']:.2f} | {row['best_two_move_share']:.2f} | "
                     f"{row['patterns']} |")
    lines.append('\n| bar | scopes whose top single pattern reaches it | scopes whose best '
                 'two moves reach it |')
    lines.append('|---|---|---|')
    for bar in BARS:
        single = sum(1 for row in sensitivity if row['top_single_pattern_share'] >= bar)
        two = sum(1 for row in sensitivity if row['best_two_move_share'] >= bar)
        lines.append(f'| {bar:.2f} | {single} of {len(sensitivity)} | {two} of {len(sensitivity)} |')

    lines.append('\n## Expected value of the prescriptive rules\n')
    lines.append('Value credit requires evaluations at the rule\'s scope positions; boards '
                 'without them are counted, never dropped.\n')
    lines.append('| bar | rule | family | answers | boards | evaluated | EV per board | note |')
    lines.append('|---|---|---|---|---|---|---|---|')
    for bar in BARS:
        estimation = rules.estimate_rule_ev(db, per_bar[bar]['rules'])
        for item in per_bar[bar]['prescriptive']:
            entry = estimation.get(item['rule_id'])
            if not entry:
                continue
            note = entry.get('reason') or ''
            ev = entry.get('ev_per_board')
            lines.append(f"| {bar:.2f} | `{item['rule_id'][:8]}` | "
                         f"`{entry['structure_id'][:8]}` | {' '.join(entry.get('answers') or [])} | "
                         f"{entry['boards']} | {entry['boards_evaluated']} | "
                         f"{('%.4f' % ev) if ev is not None else '—'} | {note} |")

    lines.append('\n## Value per complexity unit by knowledge class\n')
    lines.append('| class | items | positions | value | burden | value per burden |')
    lines.append('|---|---|---|---|---|---|')
    for kind, entry in sorted(campaign.compare_per_cost(db).items()):
        lines.append(f"| {kind} | {entry['items']} | {entry['positions']} | "
                     f"{entry['value']:.5f} | {entry['cost']:.0f} | "
                     f"{('%.6f' % entry['value_per_cost']) if entry['value_per_cost'] else '—'} |")
    (outdir / 'template-value-experiment.md').write_text('\n'.join(lines) + '\n')

    # ------------------------------------------------------------ convergence
    lines = ['# Evaluation convergence across node budgets\n']
    lines.append('## Campaign accounting\n')
    lines.append('| tier | jobs | done | node budget |')
    lines.append('|---|---|---|---|')
    for row in accounting['by_tier']:
        lines.append(f"| {row['tier']} | {row['jobs']} | {row['done']} | {row['budget']:,} |")
    totals = accounting['totals']
    lines.append(f"\nTotal: {totals['jobs']} jobs, {totals['done']} done, "
                 f"{totals['budget']:,} nodes budgeted, "
                 f"{(accounting['nodes_executed'] or 0):,} nodes executed. "
                 f"Campaign evaluations stored: {accounting['campaign_evals']}.\n")

    lines.append('## Achieved depth per tier (metadata, never the stopping rule)\n')
    lines.append('| node budget | positions | mean | median | p10 | p90 | min | max |')
    lines.append('|---|---|---|---|---|---|---|---|')
    for budget in sorted(depth):
        row = depth[budget]
        lines.append(f"| {budget:,} | {row['positions']} | {row['mean']:.2f} | "
                     f"{row['median']} | {row['p10']} | {row['p90']} | {row['min']} | {row['max']} |")

    items = curriculum.build_items(db, options.run_id, criterion='expert')
    answers_by_item = {item['item_id']: item['answers'] for item in items}
    fen_of = {row[0]: row[1] for row in db.execute('SELECT position_key, fen FROM position')}
    items_by_fen = defaultdict(list)
    for item in items:
        for key in item['keys']:
            if key in fen_of:
                items_by_fen[fen_of[key]].append(item['item_id'])

    table = convergence.convergence_table(budget_evals, items_by_fen, answers_by_item)
    lines.append('\n## Stability between budgets\n')
    lines.append('| pair | positions | best move unchanged | MultiPV order identical | mean Kendall tau '
                 '| mean Spearman | mean abs EP change | max abs EP change | taught-answer entries '
                 '| mean taught loss change |')
    lines.append('|---|---|---|---|---|---|---|---|---|---|')
    summaries = {}
    for shallow, deep in convergence.PAIRS:
        rows = table.get((shallow, deep), [])
        summary = convergence.summarise(rows)
        summaries[(shallow, deep)] = (summary, rows)
        if summary['positions'] == 0:
            lines.append(f'| {shallow:,} → {deep:,} | 0 | — | — | — | — | — | — | — | — |')
            continue
        lines.append(
            f"| {shallow:,} → {deep:,} | {summary['positions']} | "
            f"{summary['best_move_unchanged']} ({summary['best_move_unchanged_share']:.1%}) | "
            f"{summary['order_identical']} ({summary['order_identical_share']:.1%}) | "
            f"{('%.3f' % summary['mean_kendall_tau']) if summary['mean_kendall_tau'] is not None else '—'} | "
            f"{('%.3f' % summary['mean_spearman']) if summary['mean_spearman'] is not None else '—'} | "
            f"{summary['mean_abs_best_ep_change']:.4f} | {summary['max_abs_best_ep_change']:.4f} | "
            f"{summary['taught_answer_entries']} | "
            f"{('%.4f' % summary['mean_taught_loss_change']) if summary['mean_taught_loss_change'] is not None else '—'} |")
    if not table.get((25_000_000, 100_000_000)):
        lines.append('\n**25M → 100M has no positions.** The frozen sheet assigns one tier per '
                     'position and the promotion rule deepens a tier-A position by one step, so '
                     'a position is never evaluated at both 25M and 100M. This is a property of '
                     'the approved design, not a gap in the run; producing those pairs would be '
                     'a budget change and is proposed as Phase 2 rather than done.')

    lines.append('\n## Curriculum items whose value changes with the budget\n')
    changed_any = False
    for (shallow, deep), (summary, rows) in summaries.items():
        changes = convergence.item_value_changes(rows)
        material = {item: entry for item, entry in changes.items() if entry['changed']}
        lines.append(f'\n**{shallow:,} → {deep:,}**: {len(changes)} items measured, '
                     f'{len(material)} change a taught-answer value by ≥0.005 EP.')
        if material:
            changed_any = True
            lines.append('\n| item | entries | changed | worst abs change |')
            lines.append('|---|---|---|---|')
            for item_id, entry in sorted(material.items(), key=lambda kv: -kv[1]['worst']):
                lines.append(f"| `{item_id[:8]}` | {entry['entries']} | {entry['changed']} | "
                             f"{entry['worst']:.4f} |")
    if not changed_any:
        lines.append('\nNo item value moved by ≥0.005 EP at any budget pair measured, and no item '
                     'changed classification.')

    lines.append('\n## Positions that could affect the curriculum conclusion\n')
    unstable = []
    for (shallow, deep), (summary, rows) in summaries.items():
        for row in rows:
            if not row['best_same'] or any(abs(v['change']) >= 0.005
                                           for v in (row.get('taught_answer') or {}).values()):
                unstable.append({'fen': row['fen'], 'shallow': shallow, 'deep': deep,
                                 'best_shallow': row['best_shallow'],
                                 'best_deep': row['best_deep'],
                                 'abs_ep_change': abs(row['best_ep_change']),
                                 'items': [i for i, v in (row.get('taught_answer') or {}).items()
                                           if abs(v['change']) >= 0.005]})
    if unstable:
        lines.append(f'{len(unstable)} position(s) either flip the best move or move an item value '
                     f'by ≥0.005 EP:\n')
        lines.append('| fen | pair | best at shallow | best at deep | abs EP change | items affected |')
        lines.append('|---|---|---|---|---|---|')
        for row in sorted(unstable, key=lambda r: -r['abs_ep_change']):
            lines.append(f"| `{row['fen']}` | {row['shallow']:,}→{row['deep']:,} | "
                         f"{row['best_shallow']} | {row['best_deep']} | "
                         f"{row['abs_ep_change']:.4f} | {', '.join(i[:8] for i in row['items'])} |")
        lines.append('\nPhase 2, if you want it: deepen exactly these positions to the next '
                     'frozen tier (and add 25M→100M pairs, which the approved design does not '
                     'produce). Not run.')
    else:
        lines.append('None: no measured position flips its best move or moves an item value by '
                     '≥0.005 EP, so no Phase 2 deepening is indicated by this data.')
    (outdir / 'eval-convergence.md').write_text('\n'.join(lines) + '\n')

    # ------------------------------------------------------------- curricula
    lines = ['# Curriculum frontier, after the campaign\n']
    lines.append('One shared value surface and one shared denominator across all six curricula; '
                 'recognition-only knowledge is charged burden and earns no value.\n')
    lines.append('| curriculum | items | burden | recognition coverage | evaluated positions | '
                 'value retained | recovery | value per burden |')
    lines.append('|---|---|---|---|---|---|---|---|')
    six = curriculum.compare_curricula(db)
    for row in six:
        lines.append(f"| {row['curriculum']} | {row['items']} | {row['burden']:.0f} | "
                     f"{row['recognition_coverage']:.3f} | {row['evaluated_positions']} | "
                     f"{row['value_retained']:.5f} | "
                     f"{('%.3f' % row['recovery_fraction']) if row['recovery_fraction'] is not None else '—'} | "
                     f"{('%.6f' % row['value_per_burden']) if row['value_per_burden'] else '—'} |")
    lines.append('\n## The six curricula at each frozen support bar\n')
    lines.append('| bar | curriculum | items | burden | recognition | evaluated | value retained '
                 '| recovery |')
    lines.append('|---|---|---|---|---|---|---|---|')
    for bar in BARS:
        for row in curriculum.compare_curricula(db, rule_settings={'support_floor': bar}):
            lines.append(f"| {bar:.2f} | {row['curriculum']} | {row['items']} | {row['burden']:.0f} | "
                         f"{row['recognition_coverage']:.3f} | {row['evaluated_positions']} | "
                         f"{row['value_retained']:.5f} | "
                         f"{('%.3f' % row['recovery_fraction']) if row['recovery_fraction'] is not None else '—'} |")
    (outdir / 'curriculum-frontier-v2.md').write_text('\n'.join(lines) + '\n')

    with (outdir / 'eval-convergence-positions.csv').open('w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['fen', 'shallow', 'deep', 'best_same', 'best_shallow', 'best_deep',
                         'kendall_tau', 'spearman', 'best_ep_change', 'order_identical'])
        for (shallow, deep), (_s, rows) in summaries.items():
            for row in rows:
                writer.writerow([row['fen'], row['shallow'], row['deep'], row['best_same'],
                                 row['best_shallow'], row['best_deep'], row['kendall_tau'],
                                 row['spearman'], row['best_ep_change'], row['order_same']])
    print(json.dumps({'template_value': str(outdir / 'template-value-experiment.md'),
                      'convergence': str(outdir / 'eval-convergence.md'),
                      'curricula': str(outdir / 'curriculum-frontier-v2.md'),
                      'pairs': {f'{s}->{d}': len(table.get((s, d), []))
                                for s, d in convergence.PAIRS},
                      'unstable_positions': len(unstable)}, indent=1))
    db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=studydb.DEFAULT_ANALYSIS_DB)
    parser.add_argument('--run-id', type=int, default=6)
    parser.add_argument('--outdir', type=Path)
    args = parser.parse_args()
    render(args)


if __name__ == '__main__':
    main()
