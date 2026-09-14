"""Structural attractors: coverage, path diversity and persistence per structure.

Research task 4 asks whether a small number of structural states dominate the
family — states reached by many different move orders that also *persist* for a
while, so that learning one representative board is worth more than learning many
concrete lines.

Definitions:

    enter_mass(P)   = flow(P) − Σ_{parents inside S} flow(parent) · share(parent→P)
                      the probability mass that first enters the structure at P
    D(P)            = 1 + Σ_{children inside S} share(P→child) · D(child)
                      expected number of consecutive plies spent inside S
                      (computed backwards over plies; a position with no child
                      inside S has D = 1)
    persistence(S)  = Σ_P enter_mass(P) · D(P) / Σ_P enter_mass(P)
    coverage_lower  = max_P flow(P)          (a game at P must have passed S)
    coverage_upper  = min(1, Σ_P flow(P))    (interval: games visiting two
                      positions of S cannot be deduplicated without per-game data)
    attractor_score = coverage_lower · persistence_mean · (1 + log10(1 + paths))

Every factor is stored in `components_json`; the score is a reporting device, not
a verdict.
"""
import json
import math
import time
from collections import defaultdict


def exit_time_stats(members, moves, entry_masses, max_steps=20):
    """Game-level dwell distribution inside one structure.

    Mass is pushed along the structure's own move shares; a move whose child lies
    outside the structure leaves it. This yields, per unit of entering mass, the
    number of consecutive plies spent inside — the quantity a learner cares about.

    (An earlier version estimated dwell per position as an expected value and
    averaged over flow, which over-weights deep boards and reported 7.06 plies for
    the queen-out family where the game-level answer is 3.13. This simulator is
    now the single definition used by structure_stat and the family report.)
    """
    members = set(members)
    current = {key: mass for key, mass in entry_masses.items() if key in members and mass > 0}
    entry_mass = sum(current.values())
    if not entry_mass:
        return {'entry_mass': 0.0, 'mean_dwell': None, 'median_dwell': None,
                'exit_first_ply': None, 'exit_within_two': None, 'retention_3': None,
                'curve': [], 'exits': []}
    exits, curve = [], []
    remaining = entry_mass
    for _step in range(max_steps):
        nxt, left = defaultdict(float), 0.0
        for key, mass in current.items():
            edges = moves.get(key, [])
            total = sum(g for _c, _u, g in edges)
            if not total:
                left += mass
                continue
            for child, _uci, games in edges:
                share = games / total
                if child in members:
                    nxt[child] += mass * share
                else:
                    left += mass * share
        exits.append(left)
        remaining -= left
        curve.append(remaining)
        current = dict(nxt)
        if not current:
            # Pad so every retention(k) and exit@k is defined: mass that already
            # left cannot return, so the remaining terms are genuinely zero.
            while len(exits) < max_steps:
                exits.append(0.0)
                curve.append(0.0)
            break
    total_exits = sum(exits) or 1.0
    cumulative, median = 0.0, None
    for step, mass in enumerate(exits):
        cumulative += mass
        if median is None and cumulative >= total_exits / 2:
            median = step + 1
    return {'entry_mass': entry_mass, 'mean_dwell': sum((s + 1) * m for s, m in enumerate(exits)) / total_exits,
            'median_dwell': median, 'exits': exits, 'curve': curve,
            'exit_first_ply': exits[0] / total_exits if exits else None,
            'exit_within_two': (exits[0] + exits[1]) / total_exits if len(exits) > 1 else None,
            'retention_3': (curve[2] / entry_mass) if len(curve) > 2 and entry_mass else None}


def compute(db, source, run_id, verbose=True):
    positions = {}
    for row in db.execute('SELECT position_key, ply, structure_id, dev_status, pieces_json '
                          'FROM position'):
        positions[row['position_key']] = {'ply': row['ply'], 'structure': row['structure_id'],
                                          'dev_status': row['dev_status'],
                                          'pieces': row['pieces_json']}
    flow = {row['position_key']: (row['reach_flow'] or 0.0)
            for row in db.execute('SELECT position_key, reach_flow FROM position_flow WHERE source=?',
                                  (source,))}
    # Mass that first enters a position's structure here (persisted by flow.compute).
    per_position_enter = {row['position_key']: (row['enter_mass'] or 0.0)
                          for row in db.execute('SELECT position_key, enter_mass FROM position_flow '
                                                'WHERE source=?', (source,))}
    paths = {row['position_key']: (row['path_count'] or 0.0)
             for row in db.execute('SELECT position_key, path_count FROM position_flow WHERE source=?',
                                   (source,))}
    parent_total = {row['position_key']: (row['n_parents_total'] or 0)
                    for row in db.execute('SELECT position_key, n_parents_total FROM position_flow '
                                          'WHERE source=?', (source,))}
    moves = defaultdict(list)
    for row in db.execute('SELECT parent_key, child_key, uci, games FROM provenance WHERE source=?',
                          (source,)):
        moves[row['parent_key']].append((row['child_key'], row['uci'], row['games']))

    by_ply = defaultdict(list)
    for key, meta in positions.items():
        by_ply[meta['ply']].append(key)

    # Expected plies remaining inside the same structure, backwards over plies.
    dwell = {}
    for ply in sorted(by_ply, reverse=True):
        for key in by_ply[ply]:
            structure = positions[key]['structure']
            edges = moves.get(key, [])
            total = sum(g for _c, _u, g in edges)
            stay = 0.0
            if total:
                for child, _uci, games in edges:
                    meta = positions.get(child)
                    if meta and meta['structure'] == structure and meta['ply'] > ply:
                        stay += (games / total) * dwell.get(child, 1.0)
            dwell[key] = 1.0 + stay

    # Mass that first enters each structure, and the edge/parent diversity.
    edges_in = defaultdict(set)
    parents = defaultdict(set)
    enter_mass = defaultdict(float)
    for key, meta in positions.items():
        structure = meta['structure']
        from_inside = 0.0
        edges = moves.get(key, [])
        total = sum(g for _c, _u, g in edges)
        for child, uci, games in edges:
            child_meta = positions.get(child)
            if child_meta is None or child_meta['ply'] <= meta['ply']:
                continue
            edges_in[child_meta['structure']].add((key, uci))
            parents[child_meta['structure']].add(key)
            if total and child_meta['structure'] == structure and flow.get(key, 0.0):
                from_inside += flow[key] * (games / total)
        enter_mass[structure] += per_position_enter.get(key, 0.0)

    stats = defaultdict(lambda: {'positions': 0, 'coverage_lower': 0.0, 'coverage_sum': 0.0,
                                 'paths': 0.0, 'parents': 0, 'signatures': set()})
    for key, meta in positions.items():
        entry = stats[meta['structure']]
        entry['positions'] += 1
        entry['coverage_lower'] = max(entry['coverage_lower'], flow.get(key, 0.0))
        entry['coverage_sum'] += flow.get(key, 0.0)
        entry['paths'] = max(entry['paths'], paths.get(key, 0.0))
        entry['parents'] = max(entry['parents'], parent_total.get(key, 0))
        entry['signatures'].add((meta['dev_status'], meta['pieces']))

    rows = 0
    by_structure = defaultdict(list)
    for key, meta in positions.items():
        by_structure[meta['structure']].append(key)
    for structure_id, entry in stats.items():
        mass_total = enter_mass.get(structure_id, 0.0)
        members = by_structure[structure_id]
        dwell_stats = exit_time_stats(members, moves, per_position_enter)
        persistence_mean = dwell_stats['mean_dwell']
        persistence_median = dwell_stats['median_dwell']
        coverage_lower = entry['coverage_lower']
        coverage_upper = min(1.0, entry['coverage_sum'])
        score = None
        if coverage_lower and persistence_mean:
            score = coverage_lower * persistence_mean * (1 + math.log10(1 + entry['paths']))
        components = {'coverage_lower': coverage_lower, 'coverage_upper': coverage_upper,
                      'peak_reach': coverage_lower, 'enter_mass': mass_total,
                      'persistence_mean_plies': persistence_mean,
                      'persistence_median_plies': persistence_median,
                      'exit_share_first_ply': dwell_stats['exit_first_ply'],
                      'exit_share_within_two': dwell_stats['exit_within_two'],
                      'retention_3': dwell_stats['retention_3'],
                      'max_paths': entry['paths'],
                      'max_parents_per_position': entry['parents'],
                      'distinct_parents_family': len(parents.get(structure_id, ())),
                      'n_edges_in': len(edges_in.get(structure_id, ())),
                      'n_development_signatures': len(entry['signatures'])}
        db.execute('''INSERT OR REPLACE INTO structure_stat(structure_id, source, run_id,
                        n_positions, n_edges_in, n_parents, coverage_lower, coverage_upper,
                        peak_games, path_count, persistence_median_plies, persistence_mean_plies,
                        n_development_signatures, attractor_score, components_json)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (structure_id, source, run_id, entry['positions'],
                    len(edges_in.get(structure_id, ())), len(parents.get(structure_id, ())),
                    coverage_lower,
                    coverage_upper, int(coverage_lower * 1000), entry['paths'], persistence_median,
                    persistence_mean, len(entry['signatures']),
                    score, json.dumps(components)))
        rows += 1
    db.commit()
    if verbose:
        print(f'attractor stats written for {rows} structures (source {source})')
    return {'structures': rows}
