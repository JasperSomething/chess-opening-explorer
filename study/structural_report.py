"""Render the structural-flow / maturity report (tasks A-E).

Writes analysis/structural-flow.md plus CSVs. Read-only against the analysis
database; no engine computation.
"""
import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import db as studydb, structure_flow, templates  # noqa: E402

CONCEPTS = """\
Three concepts are used, operationally defined so the data can decide the members:

* **structural state** — one exact pawn placement (a family). Games pass through a
  sequence of these, and pawn moves are irreversible, so the family graph is a DAG.
* **development corridor** — a high-flow family whose boards are still undeveloped:
  its characteristic feature is that invariant placements (pieces on home squares,
  castling rights intact) are consequences of *low ply*, not of strategy. Measured
  here as high entering mass with a low development level.
* **mature template** — a family that is both sufficiently developed (absolute
  development level) and sufficiently persistent (dwell, retention) that its piece
  placement, pawn commitments and outgoing transformations carry information.
  Membership is decided by those measured values, never by chess knowledge about
  the Scandinavian.
"""


def load_context(db, source='local2200'):
    positions, moves = structure_flow.load_graph_inputs(db)
    edges, summary = structure_flow.transition_edges(positions, moves)
    leak = {s: m['leak_mass'] for s, m in summary.items()}
    entry = db.execute('SELECT structure_id FROM position WHERE is_entry=1').fetchone()[0]
    pool = templates.candidate_pool(db, source)
    return positions, moves, edges, summary, leak, entry, pool


def render(db, source='local2200', outdir=None):
    positions, moves, edges, summary, leak, entry, pool = load_context(db, source)
    pool_ids = [row['structure_id'] for row in pool]
    out = []
    add = out.append
    add('# Scandinavian structural flow and maturity (tasks A-E)\n')
    add(CONCEPTS)

    # ---------------------------------------------------------------- task A
    add('\n## A. Structural flow graph\n')
    add(f'* families (exact pawn structures) with any flow: **{len(summary)}**')
    add(f'* observed structural transitions: **{len(edges)}** '
        f'(of which {sum(1 for m in edges.values() if m >= 0.0005)} carry ≥0.0005 mass)')
    add(f'* families with entering mass ≥0.02: **{sum(1 for m in summary.values() if m["entry_mass"] >= 0.02)}**; '
        f'≥0.05: {sum(1 for m in summary.values() if m["entry_mass"] >= 0.05)}')
    add(f'* entry family `{entry}` (after 1.e4 d5): entry mass 1.0 by definition\n')
    add('Main channels (transition mass, i.e. family-conditioned probability of that '
        'structural change):\n')
    add('| source | destination | move | mass | shares of source exit | from boards | from ply |')
    add('|---|---|---|---|---|---|---|')
    for (a, b, uci), mass in sorted(edges.items(), key=lambda kv: -kv[1])[:12]:
        meta = summary.get(a, {})
        exit_mass = meta.get('exit_mass')
        share = f'{mass / exit_mass:.1%}' if exit_mass else '—'
        ply = f"{meta['ply_mean']:.1f}" if meta.get('ply_mean') else '—'
        add(f"| `{a}` | `{b}` | {uci} | {mass:.4f} | {share} | {meta.get('boards', '—')} | {ply} |")

    add('\nHighest-mass absolute trajectories from the entry (`*` marks a mature '
        'candidate):\n')
    add('| mass | trajectory |')
    add('|---|---|')
    for path, mass in structure_flow.main_trajectories(edges, entry, top=8, initial_mass=1.0):
        marks = ['*' if node in pool_ids else '' for node in path]
        add(f"| {mass:.4f} | " + ' → '.join(f'`{n[:8]}`{m}' for n, m in zip(path, marks)) + ' |')

    add('\nMass that leaves the analysed domain per family (the domain stops at '
        'ply 34 or below 10 expert games, so this is a data boundary, not a chess '
        'result):\n')
    add('| family | entry mass | leak mass | leak share |')
    add('|---|---|---|---|')
    for structure, mass in sorted(leak.items(), key=lambda kv: -kv[1])[:6]:
        entry_mass = summary[structure]['entry_mass'] or 1.0
        add(f'| `{structure}` | {entry_mass:.4f} | {mass:.4f} | {mass / entry_mass:.1%} |')

    # ---------------------------------------------------------------- task B
    add('\n## B. Maturity measure\n')
    add('Components are reported separately and never only as a composite. '
        '`dev level` is the mean of the absolute strategic components (minors '
        'developed both sides, castling both sides, central pawns left home both '
        'sides). `residual` is the depth-adjusted z-score inside the family\'s ply '
        'bucket — a different question ("ahead of schedule"), which alone is '
        'misleading: the queen-out corridor scores +0.83 on it while standing at '
        '0.21/0.04 minors developed and no castling, because at its ply almost no '
        'family has developed anything.\n')
    rows = db.execute('''SELECT * FROM structure_maturity WHERE source=?
                         AND entry_mass >= 0.02 ORDER BY entry_mass DESC''', (source,)).fetchall()
    add('| family | boards | mass | ply(visits) | minors W/B | castled W/B | centre left W/B | '
        'dwell | retention(3) | reconvergence | eff. successors | dev level | residual |')
    add('|---|---|---|---|---|---|---|---|---|---|---|---|---|')
    for row in rows:
        add(f"| `{row['structure_id'][:8]}` | {row['boards']} | {row['entry_mass']:.3f} | "
            f"{row['ply_mean']:.1f} | {row['minors_white']:.2f}/{row['minors_black']:.2f} | "
            f"{row['castled_white']:.2f}/{row['castled_black']:.2f} | "
            f"{row['centre_left_white']:.2f}/{row['centre_left_black']:.2f} | "
            f"{row['dwell_mean']:.2f} | {row['retention_3']:.2f} | {row['reconvergence']:.2f} | "
            f"{row['effective_successors']:.2f} | {row['development_level']:.3f} | "
            f"{row['maturity_index']:+.2f} |")

    # ---------------------------------------------------------------- task C
    add('\n## C. Candidate mature templates\n')
    add(f'Candidate pool: entering mass ≥0.02, development level ≥0.35, dwell ≥2.0 '
        f'plies, ≥15 boards → **{len(pool)} families**. Thresholds are stated so they '
        f'can be argued with; the frontier in section E decides how many are worth '
        f'learning.\n')
    for row in pool:
        descriptor = templates.descriptors(db, row['structure_id'], source)
        if not descriptor:
            continue
        add(f"### `{row['structure_id'][:8]}` — mass {descriptor['entry_mass']:.3f}, "
            f"{descriptor['boards']} boards, dwell {descriptor['mean_dwell']:.2f}, "
            f"retention(3) {descriptor['retention_3']:.2f}, dev level {row['development_level']:.3f}\n")
        add('<pre>' + descriptor['pawn_diagram'] + '</pre>\n')
        add(f"* material {descriptor['material']}; white pawns `{descriptor['white_pawns']}`, "
            f"black pawns `{descriptor['black_pawns']}`")
        if descriptor['stats']:
            stats = descriptor['stats']
            add(f"* incoming move-order diversity: {stats['n_edges_in']} distinct incoming edges "
                f"from {stats['n_parents']} distinct parent boards; peak coverage "
                f"{stats['coverage_lower']:.3f}")
        add('* occupancy (share of board-visits): ' + '; '.join(
            f"{name}: " + ', '.join(f'{sq} {p:.0%}' for sq, p, _m in table)
            for name, table in descriptor['occupancy'].items()))
        add('* castling: White ' + ', '.join(f'{k} {v:.0%}' for k, v in
                                             sorted(descriptor['castling_white'].items(),
                                                    key=lambda kv: -kv[1]))
            + '; Black ' + ', '.join(f'{k} {v:.0%}' for k, v in
                                     sorted(descriptor['castling_black'].items(),
                                            key=lambda kv: -kv[1])))
        add('* development state: ' + ', '.join(f'{k} {v:.0%}' for k, v in
                                                sorted(descriptor['development'].items(),
                                                       key=lambda kv: -kv[1])))
        add('* flexible slots: ' + ('; '.join(
            f"{slot['piece']} in " + '/'.join(f'{sq} {p:.0%}' for sq, p in slot['squares'])
            for slot in descriptor['flexible_slots']) or 'none'))
        add('')

    # ---------------------------------------------------------------- task D
    add('\n## D. Future transformations (objective, next 6-12 plies)\n')
    for row in pool[:4]:
        seeds = {r['position_key']: r['enter_mass'] for r in db.execute(
            '''SELECT position_key, enter_mass FROM position_flow WHERE source=?
               AND enter_mass > 0 AND position_key IN
               (SELECT position_key FROM position WHERE structure_id=?)''',
            (source, row['structure_id']))}
        if not seeds:
            continue
        result = templates.forward_transformations(db, seeds, source=source)
        add(f"### `{row['structure_id'][:8]}`\n")
        for window, data in result['windows'].items():
            add(f"**plies {window[0]}–{window[1]}** (shares are of all moves in that "
                f"window; flow mass {data['total']:.4f}):\n")
            add('| transformation | share |')
            add('|---|---|')
            for name, mass in data['moves'].most_common(10):
                add(f'| {name} | {mass / data["total"]:.1%} |')
            if data['destinations']:
                add('\nrecurring destination squares: ' + ', '.join(
                    f'{name} {mass / data["total"]:.1%}'
                    for name, mass in data['destinations'].most_common(6)))
            if data['pawn_departures']:
                add('pawn departures by square: ' + ', '.join(
                    f'{square} {mass / data["total"]:.1%}'
                    for square, mass in data['pawn_departures'].most_common(6)))
            add('')

    # ---------------------------------------------------------------- task E
    add('\n## E. Compression frontier\n')
    frontier_rows, selected, covered, _, _, _ = templates.frontier(db, source=source, k_max=10)
    covered_all, _mass = structure_flow.coverage_of_set(edges, entry, pool_ids, leak=leak)
    add(f'* frontier ceiling (all {len(pool)} mature candidates together): '
        f'**{covered_all:.4f}** of family-conditioned mass')
    add('* greedy selection maximises marginal coverage; a candidate that adds no new '
        'mass is skipped, so near-identical boards cannot inflate the count\n')
    add('| k | family | cumulative coverage | marginal | entry mass | dev level | dwell |')
    add('|---|---|---|---|---|---|---|')
    for row in frontier_rows:
        add(f"| {row['k']} | `{row['structure_id'][:8]}` | {row['coverage']:.4f} | "
            f"{row['marginal']:+.4f} | {row['entry_mass']:.3f} | {row['development_level']:.3f} | "
            f"{row['dwell']:.2f} |")
    unselected = [row['structure_id'] for row in pool if row['structure_id'] not in selected]
    selected_ids = set(selected)
    marginal_only = templates.frontier(db, source=source, k_max=10, min_pawn_distance=0,
                                       pool=pool)[0]
    if unselected:
        add('\nThe remaining candidates, split by *why* they are not in the frontier '
            '(own mass vs what they would actually add):\n')
        add('| family | entry mass | own coverage | nearest selected (pawn squares away) '
            '| marginal next to the selection | verdict |')
        add('|---|---|---|---|---|---|')
        for structure in unselected:
            own, _m = structure_flow.coverage_of_set(edges, entry, [structure], leak=leak)
            distances = {s: (templates.pawn_distance(db, structure, s) or 99)
                         for s in selected_ids}
            nearest = min(distances, key=distances.get)
            with_it, _m2 = structure_flow.coverage_of_set(edges, entry, selected + [structure],
                                                         leak=leak)
            gained = with_it - covered
            verdict = ('near-duplicate of `%s`, suppressed' % nearest[:8]
                       if distances[nearest] < 3 else
                       ('fully subsumed by the selection' if gained < 5e-4
                        else 'would add %.4f but was passed over' % gained))
            add(f'| `{structure[:8]}` | {summary.get(structure, {}).get("entry_mass", 0.0):.3f} '
                f'| {own:.4f} | `{nearest[:8]}` ({distances[nearest]}) | {gained:+.4f} | {verdict} |')
    add('\nFrontier without duplicate suppression, for contrast:\n')
    add('| k | family | cumulative coverage | marginal |')
    add('|---|---|---|---|')
    for row in marginal_only:
        add(f"| {row['k']} | `{row['structure_id'][:8]}` | {row['coverage']:.4f} | "
            f"{row['marginal']:+.4f} |")
    add(f'\nSelected families differ by ≥3 pawn squares '
        f'(pairwise distances: {json.dumps({f"{a[:8]}-{b[:8]}": d for (a, b), d in templates.similarity_matrix(db, selected).items()})})')

    mass_only = [dict(r) for r in db.execute(
        '''SELECT structure_id, entry_mass FROM structure_maturity WHERE source=?
           AND entry_mass >= 0.02 ORDER BY entry_mass DESC''', (source,))]
    best = None
    for row in mass_only:
        cov, _ = structure_flow.coverage_of_set(edges, entry, [row['structure_id']], leak=leak)
        if best is None or cov > best[1]:
            best = (row['structure_id'], cov)
    add(f'\nFor contrast, selecting purely by mass picks `{best[0][:8]}` first with '
        f'coverage {best[1]:.4f} — the entry corridor, which covers every game and '
        f'teaches nothing. Coverage alone is therefore not a usable objective; the '
        f'frontier above is computed over mature candidates only, and the ceiling is '
        f'what the boundary of the analysed domain allows.')

    outdir = outdir or (ROOT / 'analysis')
    outdir.mkdir(parents=True, exist_ok=True)
    target = outdir / 'structural-flow.md'
    target.write_text('\n'.join(out) + '\n')
    with (outdir / 'structural-flow-frontier.csv').open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(frontier_rows[0].keys()))
        writer.writeheader()
        writer.writerows(frontier_rows)
    with (outdir / 'structural-flow-maturity.csv').open('w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['structure_id', 'boards', 'entry_mass', 'ply_mean_visits', 'minors_white',
                         'minors_black', 'castled_white', 'castled_black', 'centre_left_white',
                         'centre_left_black', 'dwell_mean', 'retention_3', 'reconvergence',
                         'effective_successors', 'development_level', 'maturity_residual'])
        for row in rows:
            writer.writerow([row['structure_id'], row['boards'], round(row['entry_mass'], 5),
                             round(row['ply_mean'], 2), round(row['minors_white'], 3),
                             round(row['minors_black'], 3), round(row['castled_white'], 3),
                             round(row['castled_black'], 3), round(row['centre_left_white'], 3),
                             round(row['centre_left_black'], 3), round(row['dwell_mean'], 3),
                             round(row['retention_3'], 3), round(row['reconvergence'], 3),
                             round(row['effective_successors'], 3),
                             round(row['development_level'], 4),
                             round(row['maturity_index'], 3)])
    print(f'report written to {target}')
    return {'target': str(target), 'families': len(summary), 'transitions': len(edges),
            'pool': len(pool), 'frontier': len(frontier_rows), 'ceiling': covered_all}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=studydb.DEFAULT_ANALYSIS_DB)
    parser.add_argument('--source', default='local2200')
    parser.add_argument('--outdir', type=Path)
    args = parser.parse_args()
    db = studydb.connect(args.db, create=False)
    print(json.dumps(render(db, args.source, args.outdir), indent=1))
    db.close()


if __name__ == '__main__':
    main()
