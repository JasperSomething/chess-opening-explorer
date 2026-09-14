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
        if flow.get(key, 0.0) > 0:
            enter_mass[structure] += max(0.0, flow[key] - from_inside)

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
    for structure_id, entry in stats.items():
        mass_total = enter_mass.get(structure_id, 0.0)
        members = [key for key, meta in positions.items() if meta['structure'] == structure_id]
        if mass_total > 0:
            persistence_mean = sum(flow.get(k, 0.0) * dwell.get(k, 1.0) for k in members) / mass_total
        else:
            persistence_mean = None
        weighted = sorted(((flow.get(k, 0.0), dwell.get(k, 1.0)) for k in members), key=lambda t: t[1])
        flow_total = sum(mass for mass, _d in weighted)
        target, running, persistence_median = flow_total / 2 if flow_total else 0.0, 0.0, None
        for mass, dwell_value in weighted:
            running += mass
            if persistence_median is None and running >= target:
                persistence_median = dwell_value
        coverage_lower = entry['coverage_lower']
        coverage_upper = min(1.0, entry['coverage_sum'])
        score = None
        if coverage_lower and persistence_mean:
            score = coverage_lower * persistence_mean * (1 + math.log10(1 + entry['paths']))
        components = {'coverage_lower': coverage_lower, 'coverage_upper': coverage_upper,
                      'peak_reach': coverage_lower, 'enter_mass': mass_total,
                      'persistence_mean_plies': persistence_mean,
                      'persistence_median_plies': persistence_median,
                      'max_paths': entry['paths'], 'max_parents': entry['parents'],
                      'n_edges_in': len(edges_in.get(structure_id, ())),
                      'n_development_signatures': len(entry['signatures'])}
        db.execute('''INSERT OR REPLACE INTO structure_stat(structure_id, source, run_id,
                        n_positions, n_edges_in, n_parents, coverage_lower, coverage_upper,
                        peak_games, path_count, persistence_median_plies, persistence_mean_plies,
                        n_development_signatures, attractor_score, components_json)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (structure_id, source, run_id, entry['positions'],
                    len(edges_in.get(structure_id, ())), entry['parents'], coverage_lower,
                    coverage_upper, int(coverage_lower * 1000), entry['paths'], persistence_median,
                    persistence_mean, len(entry['signatures']),
                    score, json.dumps(components)))
        rows += 1
    db.commit()
    if verbose:
        print(f'attractor stats written for {rows} structures (source {source})')
    return {'structures': rows}
