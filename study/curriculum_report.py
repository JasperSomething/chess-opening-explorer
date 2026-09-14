"""Render the curriculum-frontier report.

Writes analysis/curriculum-frontier.md plus CSVs. Read-only against the ingestion
databases; writes items and split results into the analysis database.
"""
import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import curriculum, db as studydb, splitting, structure_flow, templates  # noqa: E402

HEADER = """\
# Curriculum frontier (prototype)

Implements STUDY-CURRICULUM.md on current data. No domain widening, no new engine
computation: every number below comes from the existing analysis database.

Two axes are reported and never summed:

* **expected-points axis** — engine-evaluation based, so it exists only where
  evaluations exist (523 cached evaluations over 472 of 29,876 domain positions,
  1.6%);
* **recognition axis** — game mass whose flow reaches the item's scope, engine-free
  and available for every position.

All coverage numbers are game masses computed by absorbing propagation: a game is
counted once, at the first covered position. Summing per-position reach (the first
prototype's mistake) counts a game once per ply and produced 9.44 for a quantity
bounded by 1.
"""


def item_rows(db, items, values):
    rows = []
    for item in items:
        valued = [key for key in item['keys'] if key in values]
        rows.append({
            'item_id': item['item_id'], 'type': item['type'], 'label': item['label'],
            'scope_kind': item['scope_kind'], 'positions': item['positions'],
            'coverage_flow': item['coverage'],
            'answers': ' '.join(item['answers']),
            'exact_moves': item['complexity']['exact_moves'],
            'flexible_slots': item['complexity']['flexible_slots'],
            'conditions': item['complexity']['conditions'],
            'exceptions': item['complexity']['exceptions'],
            'cost': item['complexity']['cost'],
            'compression_ratio': item['complexity']['compression_ratio'],
            'valued_positions': len(valued),
            'circular': item.get('circular', False),
        })
    return rows


def render(db, run_id=6, source='local2200', outdir=None, motif_families=3, motif_samples=5000):
    out, add = [], None

    def add(line=''):
        out.append(line)

    add(HEADER)

    # ------------------------------------------------------------- inventory
    for criterion in ('metric', 'expert'):
        items = curriculum.build_items(db, source, run_id, criterion=criterion)
        evals = curriculum.load_evals(db)
        values = curriculum.position_values(db, evals, items)
        baseline = curriculum.baseline(values)
        rows = item_rows(db, items, values)
        add(f'\n## Curriculum items (selection criterion: `{criterion}`)\n')
        add(f'* items: **{len(items)}** — ' + ', '.join(
            f'{kind} {sum(1 for i in items if i["type"] == kind)}'
            for kind in ('orientation', 'template', 'decision', 'exception')))
        add(f'* total complexity cost: **{sum(i["complexity"]["cost"] for i in items):.0f}**')
        add(f'* recognition coverage of all items together: '
            f'**{curriculum.recognition(db, items, source):.4f}** of game mass')
        add(f'* positions covered: {len({k for i in items for k in i["keys"]})}')
        add(f'* positions with evaluations in that scope: {len(values)}')
        add(f'* baseline expected loss of ordinary play on the valued sample: '
            f'**{baseline["lost_population"]:.5f}**; strong-play residual '
            f'{baseline["lost_expert"]:.5f}')
        if criterion == 'metric':
            add(f'* flagged circular (selected by a score that already used the ordinary '
                f'population): {sum(1 for i in items if i.get("circular"))} of {len(items)}')
        add('')
        add('| type | label | scope | positions | coverage | answers | moves | slots | conditions | '
            'exceptions | cost | positions/cost | valued |')
        add('|---|---|---|---|---|---|---|---|---|---|---|---|---|')
        for row in sorted(rows, key=lambda r: (r['type'], -r['coverage_flow'])):
            add(f"| {row['type']} | {row['label'][:30]} | {row['scope_kind']} | {row['positions']} | "
                f"{row['coverage_flow']:.4f} | {row['answers'][:24]} | {row['exact_moves']} | "
                f"{row['flexible_slots']} | {row['conditions']} | {row['exceptions']} | "
                f"{row['cost']:.0f} | {row['compression_ratio']:.1f} | {row['valued_positions']} |")

        # ------------------------------------------------------------- frontier
        frontier_rows, chosen, best = curriculum.frontier(items, values, k_max=40)
        add(f'\n### Frontier (greedy value per unit cost, overlap accounted)\n')
        if not frontier_rows:
            add('_No item in this set earns a positive marginal value on the evaluated '
                'sample. The value axis is empty here, which is a statement about '
                'evaluation coverage, not about the items._')
        else:
            add('| step | item | type | cost | cumulative cost | marginal value | value retained | '
                'coverage | circular |')
            add('|---|---|---|---|---|---|---|---|---|')
            for row in frontier_rows:
                add(f"| {row['step']} | {row['label'][:26]} | {row['type']} | {row['cost']:.0f} | "
                    f"{row['cumulative_cost']:.0f} | {row['marginal_value']:+.5f} | "
                    f"{row['value_retained']:.5f} | {row['coverage_flow']:.4f} | {row['circular']} |")
            add('')
            add(f'Retained at the end of the frontier: **{frontier_rows[-1]["value_retained"]:.5f}** '
                f'of {baseline["lost_population"]:.5f} lost by ordinary play '
                f'({frontier_rows[-1]["value_retained"] / baseline["lost_population"]:.1%}). '
                f'The strong-play residual is {baseline["lost_expert"]:.5f}, so the gap that '
                f'knowledge could in principle close is bounded by it.')
        if criterion == 'metric':
            add('\nItems at this criterion were ranked by the stored study priority, which was '
                'computed from both move distributions; valuing them on the ordinary population '
                'is therefore circular and the frontier above is illustrative of the machinery, '
                'not evidence. The `expert` criterion below removes that circularity.')
        else:
            add('\nUnder this criterion items were built from the strong-play source only, so the '
                'ordinary population is untouched by selection and the frontier is a genuine '
                'cross-population transfer measurement on the evaluated positions.')

        if criterion == 'expert':
            behaviour = curriculum.behavioural_reach(db, items, population='lichess', source=source)
            mass = behaviour['taught_mass'] + behaviour['deviation_mass']
            add(f"\nBehavioural axis (engine-free, ordinary play only): over "
                f"{behaviour['positions']} covered positions, the ordinary population already "
                f"plays the taught answers in "
                f"{behaviour['taught_mass'] / mass:.1%} of the reach-weighted mass and would have "
                f"to change in {behaviour['deviation_mass'] / mass:.1%}. This axis needs no engine "
                f"and covers every position, so it is the one that can be stated at domain scale.")

    # --------------------------------------------------------------- splitting
    add('\n## Template splitting (does a feature change future behaviour?)\n')
    pool = templates.candidate_pool(db, source)
    positions, moves = structure_flow.load_graph_inputs(db)
    split_rows = []
    for row in pool:
        split_rows.extend(splitting.evaluate_family(db, row['structure_id'], source,
                                                    positions=positions, moves=moves))
    splitting.persist(db, split_rows)
    add(f'Tested {len(split_rows)} family × feature × target combinations over '
        f'{len(pool)} mature families. Convention: `near` = the next strong move at the '
        f"family's boards; `far` = the pawn structure reached within 3 plies. A split "
        f'requires ≥0.10 bits, p < 0.05, every group ≥5% of mass and ≥5 boards.\n')
    add('| family | feature | target | MI (bits) | p | groups | min group share | decision |')
    add('|---|---|---|---|---|---|---|---|')
    for row in sorted(split_rows, key=lambda r: -r['mi_bits'])[:24]:
        add(f"| `{row['structure_id'][:8]}` | {row['feature']} | {row['target']} | "
            f"{row['mi_bits']:.3f} | {row['p_value']:.3f} | {row['groups']} | "
            f"{row['min_group_share']:.2f} | {row['decision']} |")
    splits = [r for r in split_rows if r['decision'] == 'split']
    add(f'\nSplits accepted: **{len(splits)}** — ' + (', '.join(
        f"`{r['structure_id'][:8]}` by {r['feature']} ({r['target']}, {r['mi_bits']:.2f} bits)"
        for r in splits) if splits else 'none'))
    add('\nKnown limitation, stated rather than hidden: the permutation test permutes boards, '
        'but boards inside one family are visited by the same games in sequence, so they are not '
        'independent samples and the p-values are optimistic. A game-level test needs the '
        'per-game trace index (STUDY-CURRICULUM.md section 7, item 1) and is not attempted here.')

    # ------------------------------------------------------------------ motifs
    add('\n## Motif mining\n')
    try:
        from study import motifs
    except ImportError:
        add('_study/motifs.py not present._')
    else:
        for row in pool[:motif_families]:
            found = motifs.mine_family(db, row['structure_id'], source=source, n=motif_samples)
            summary = motifs.motif_summary(found, top=8) if hasattr(motifs, 'motif_summary') else []
            add(f"### `{row['structure_id'][:8]}` (mass {row['entry_mass']:.3f})\n")
            if not found:
                add('_no motif above the support floor._\n')
                continue
            add('| motif | size | support | conditional frequency | order flexibility |')
            add('|---|---|---|---|---|')
            for motif in summary:
                add(f"| {motif.get('description', '')} | {motif.get('size', '')} | "
                    f"{motif.get('support', 0):.3f} | {motif.get('conditional_frequency', 0):.3f} | "
                    f"{motif.get('order_flexibility', 0):.3f} |")
            add('')

    # ------------------------------------------------------------- validation
    add('\n## Validation status\n')
    add('* **Within-corpus game-level split: not possible with the data on disk.** The '
        'databases store aggregate counts only (`position, uci, white/draws/black, '
        'white2200/draws2200/black2200`); there are no game identifiers and no per-game '
        'traces, so a random split would put the same games on both sides. Not attempted.')
    add('* **Cross-population transfer: used here.** Items selected from the strong-play '
        'source, valued on the ordinary population. Genuinely out-of-sample for the '
        'behavioural target, with the known confounds (online vs OTB, rating mix, time '
        'control).')
    add('* **Eval coverage is the binding constraint on the value axis**: 472 of 29,876 '
        'positions (1.58%) have any cached evaluation, and only a subset of those also have '
        'ordinary-play shares.')
    add('* **Minimum additional data**: (1) a per-game domain trace table '
        '(`domain_game_trace(game_id, split, ratings, date, result, source, path_json)`) '
        'from one read-only replay of `data/LumbrasGigaBase_OTB_Complete.pgn` (8.1 GB), '
        'scheduled after the running import finishes; (2) evaluations on the positions '
        'carrying the bulk of the flow mass — the top few hundred families — which is a '
        'bounded local budget at depth 18 with no cloud use.')

    outdir = outdir or (ROOT / 'analysis')
    outdir.mkdir(parents=True, exist_ok=True)
    target = outdir / 'curriculum-frontier.md'
    target.write_text('\n'.join(out) + '\n')
    with (outdir / 'curriculum-items.csv').open('w', newline='') as handle:
        rows = item_rows(db, curriculum.build_items(db, source, run_id, criterion='expert'),
                         curriculum.position_values(
                             db, curriculum.load_evals(db),
                             curriculum.build_items(db, source, run_id, criterion='expert')))
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    with (outdir / 'template-splits.csv').open('w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['structure_id', 'feature', 'target', 'mi_bits', 'p_value', 'groups',
                         'min_group_share', 'min_group_boards', 'decision'])
        for row in sorted(split_rows, key=lambda r: -r['mi_bits']):
            writer.writerow([row['structure_id'], row['feature'], row['target'],
                             round(row['mi_bits'], 4), round(row['p_value'], 4), row['groups'],
                             round(row['min_group_share'], 4), row['min_group_boards'],
                             row['decision']])
    items = curriculum.build_items(db, source, run_id, criterion='expert')
    persisted = curriculum.persist(db, run_id, items)
    print(json.dumps({'report': str(target), 'items_persisted': persisted,
                      'splits': len(splits), 'splits_tested': len(split_rows)}, indent=1))
    return {'target': str(target), 'items': len(items), 'splits': len(splits)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=studydb.DEFAULT_ANALYSIS_DB)
    parser.add_argument('--run-id', type=int, default=6)
    parser.add_argument('--source', default='local2200')
    parser.add_argument('--outdir', type=Path)
    parser.add_argument('--motif-families', type=int, default=3)
    parser.add_argument('--motif-samples', type=int, default=5000)
    args = parser.parse_args()
    db = studydb.connect(args.db, create=False)
    render(db, args.run_id, args.source, args.outdir, args.motif_families, args.motif_samples)
    db.close()


if __name__ == '__main__':
    main()
