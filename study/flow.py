"""Reachability over the domain graph (transposition-aware).

Why this exists: a position's raw game count is not a probability inside an
opening family. 1.e4 d5 2.d4 e6 reaches the same board as 1.e4 e6 2.d4 d5, so
the position's count includes games that never played the Scandinavian move
order, and the naive ratio games(position)/games(entry) can exceed 1.

The flow DP walks the family graph from the entry and distributes one unit of
mass along each move's empirical share:

    flow(entry) = 1
    flow(child) += flow(parent) * share(move | parent)

with first-visit semantics: a game counts once per position, so contributions
arriving after the position's first (minimum-ply) visit are recorded separately
as `ignored_back_mass` instead of being added. Mass whose child lies outside the
domain is recorded as `leakage`.

`count_ratio` is stored alongside `reach_flow`; when the two disagree the
position is fed partly from outside the family, which is a signal in its own
right (a transposition attractor, see research task 4).
"""
import math
from collections import defaultdict

PATH_CAP = 1e15


def compute(db, source, verbose=True):
    positions = {row['position_key']: {'ply': row['min_ply'] if row['min_ply'] is not None else row['ply'],
                                       'role': row['role'], 'is_entry': row['is_entry'],
                                       'structure': row['structure_id']}
                 for row in db.execute('SELECT position_key, ply, min_ply, role, is_entry, '
                                       'structure_id FROM position')}
    moves = defaultdict(list)
    for row in db.execute('''SELECT parent_key, child_key, uci, games FROM provenance WHERE source=?''',
                          (source,)):
        moves[row['parent_key']].append((row['child_key'], row['uci'], row['games']))

    # Distinct parents per child: those whose edge is accepted into the flow
    # (arrives at the child's minimum ply) and all parents seen in provenance.
    parents_contributing = defaultdict(set)
    parents_total = defaultdict(set)
    for row in db.execute('SELECT child_key, parent_key, source FROM provenance'):
        if row['source'] != source:
            continue
        parents_total[row['child_key']].add(row['parent_key'])
        parent = row['parent_key']
        if parent in positions and positions[parent]['ply'] < positions.get(
                row['child_key'], {'ply': 0})['ply']:
            parents_contributing[row['child_key']].add(parent)

    by_ply = defaultdict(list)
    for key, meta in positions.items():
        by_ply[meta['ply']].append(key)

    flow = defaultdict(float)
    paths = defaultdict(float)
    ignored = defaultdict(float)
    leakage = defaultdict(float)
    enter_mass = defaultdict(float)
    for key, meta in positions.items():
        if meta['is_entry']:
            flow[key] = 1.0
            paths[key] = 1.0
            # A game that starts inside the family enters its structure at the entry.
            enter_mass[key] = 1.0

    for ply in sorted(by_ply):
        for parent in by_ply[ply]:
            share_total = sum(g for _c, _u, g in moves.get(parent, []))
            if not share_total or not flow[parent]:
                continue
            for child, _uci, games in moves[parent]:
                share = games / share_total
                if child not in positions:
                    leakage[parent] += share
                    continue
                if positions[child]['ply'] <= ply:
                    # Arrives only after the child's first visit: a transposition
                    # that does not add to the child's per-game count.
                    ignored[child] += flow[parent] * share
                    continue
                flow[child] += flow[parent] * share
                paths[child] = min(PATH_CAP, paths[child] + paths[parent])
                if positions[parent]['structure'] != positions[child]['structure']:
                    # This mass first enters the child's structure at the child.
                    enter_mass[child] += flow[parent] * share

    count_ratio = {}
    for row in db.execute('SELECT position_key, reach_prob FROM position_source WHERE source=?',
                          (source,)):
        count_ratio[row['position_key']] = row['reach_prob']

    rows = 0
    for key in positions:
        ratio = count_ratio.get(key)
        reach = flow.get(key, 0.0)
        flags = []
        if ratio is not None and reach > 0 and ratio > reach * 1.5 and ratio - reach > 0.02:
            flags.append('fed_from_outside_family')
        if ignored.get(key):
            flags.append('late_transposition_arrivals')
        if leakage.get(key):
            flags.append('mass_leaves_domain')
        if not reach:
            flags.append('unreachable_in_flow')
        db.execute('''INSERT OR REPLACE INTO position_flow(position_key, source, reach_flow,
                        count_ratio, enter_mass, leakage, ignored_back_mass, n_parents_contributing,
                        n_parents_total, path_count, min_ply, flags)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (key, source, reach, ratio, enter_mass.get(key, 0.0), leakage.get(key, 0.0),
                    ignored.get(key, 0.0),
                    len(parents_contributing.get(key, ())), len(parents_total.get(key, ())),
                    paths.get(key, 0.0), positions[key]['ply'], ','.join(flags)))
        rows += 1
    db.commit()
    if verbose:
        print(f'flow computed for {rows} positions using source {source}')
    return {'source': source, 'positions': rows,
            'fed_from_outside': sum(1 for row in db.execute(
                'SELECT 1 FROM position_flow WHERE source=? AND flags LIKE "%fed_from_outside%"',
                (source,))),
            'with_paths': sum(1 for row in db.execute(
                'SELECT 1 FROM position_flow WHERE source=? AND path_count>1', (source,)))}
