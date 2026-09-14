"""Structural flow graph: how game mass moves between pawn-structure families.

Research task A, plus the coverage machinery needed for the compression frontier
(task E). Everything is family-conditioned: mass is propagated from the domain
entry along empirical move shares, never taken from raw position counts.

Nodes are exact pawn structures. Edges are *observed structural transitions*: a
move played inside a family whose resulting board belongs to a different family.
Because pawn moves are irreversible, this graph is a DAG and a game crosses each
edge at most once, so the mass on an edge is a probability, not a count of visits.

For each source family the entering mass is propagated through the family's own
boards (as in `attractors.exit_time_stats`); the mass that exits is distributed to
destination families by the share of the move that caused the exit. This keeps one
count per game per transition, which naive per-position summation would inflate.
"""
import json
from collections import defaultdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import db as studydb  # noqa: E402


def load_graph_inputs(db, source='local2200'):
    """Positions (with family and flow), and the move edges between them."""
    positions = {}
    for row in db.execute('''SELECT p.position_key, p.ply, p.structure_id, p.role,
                                    f.reach_flow, f.enter_mass, f.path_count, f.n_parents_total
                             FROM position p
                             JOIN position_flow f ON f.position_key = p.position_key AND f.source = ?''',
                          (source,)):
        positions[row['position_key']] = {'ply': row['ply'], 'structure': row['structure_id'],
                                          'role': row['role'], 'reach': row['reach_flow'] or 0.0,
                                          'enter': row['enter_mass'] or 0.0,
                                          'paths': row['path_count'] or 0.0}
    moves = defaultdict(list)
    for row in db.execute('SELECT parent_key, child_key, uci, games FROM provenance WHERE source=?',
                          (source,)):
        moves[row['parent_key']].append((row['child_key'], row['uci'], row['games']))
    return positions, moves


def transition_edges(positions, moves, source='local2200'):
    """One edge per (source family, destination family, move), mass conserved.

    Returns (edges, structures) where edges is a list of dicts and structures holds
    per-family aggregates used by the report.
    """
    by_structure = defaultdict(list)
    for key, meta in positions.items():
        by_structure[meta['structure']].append(key)

    edges = {}
    structures = {}
    for structure, members in by_structure.items():
        member_set = set(members)
        entry_mass = sum(positions[k]['enter'] for k in members)
        plies = [positions[k]['ply'] for k in members]

        # propagate the family's entering mass through its own boards
        current = {k: positions[k]['enter'] for k in members if positions[k]['enter'] > 0}
        step, exit_total, exit_plies = 0, 0.0, []
        leaked = 0.0
        exits = defaultdict(float)
        while current and step < 40:
            nxt = defaultdict(float)
            for key, mass in current.items():
                outgoing = moves.get(key, [])
                total = sum(g for _c, _u, g in outgoing)
                if not total:
                    exit_total += mass                      # dead end inside the domain
                    leaked += mass
                    continue
                for child, uci, games in outgoing:
                    share = games / total
                    child_meta = positions.get(child)
                    if child_meta is None:
                        exit_total += mass * share          # leaves the domain
                        leaked += mass * share
                        continue
                    if child_meta['structure'] == structure:
                        nxt[child] += mass * share
                    else:
                        exits[(child_meta['structure'], uci)] += mass * share
                        exit_total += mass * share
                        exit_plies.append((mass * share, positions[key]['ply']))
            current = dict(nxt)
            step += 1

        structures[structure] = {
            'boards': len(members), 'entry_mass': entry_mass,
            'ply_min': min(plies), 'ply_max': max(plies), 'ply_mean': sum(plies) / len(plies),
            'exit_mass': exit_total, 'leak_mass': leaked,
            'mean_exit_ply': (sum(p * m for m, p in exit_plies) / sum(m for m, _p in exit_plies))
            if exit_plies else None,
            'distinct_parents': None, 'incoming_edges': None,
        }
        for (destination, uci), mass in exits.items():
            edges[(structure, destination, uci)] = mass

    return edges, structures


def attach_convergence(db, source, structures, positions):
    """Distinct parents and incoming edges per family, straight from provenance."""
    structure_of = {key: meta['structure'] for key, meta in positions.items()}
    parents = defaultdict(set)
    incoming = defaultdict(set)
    for row in db.execute('SELECT child_key, parent_key, uci FROM provenance WHERE source=?',
                          (source,)):
        child_structure = structure_of.get(row['child_key'])
        if child_structure is None:
            continue
        parents[child_structure].add(row['parent_key'])
        incoming[child_structure].add((row['parent_key'], row['uci']))
    for structure, meta in structures.items():
        meta['distinct_parents'] = len(parents.get(structure, ()))
        meta['incoming_edges'] = len(incoming.get(structure, ()))
    return structures


def write_edges(db, edges, structures, source, min_mass=0.0):
    db.execute('DELETE FROM structure_edge WHERE source=?', (source,))
    written = 0
    for (a, b, uci), mass in sorted(edges.items(), key=lambda kv: -kv[1]):
        if mass < min_mass:
            continue
        db.execute('''INSERT INTO structure_edge(source_key, destination_key, uci, source, mass,
                        from_boards, from_entry_mass, from_mean_ply)
                      VALUES(?,?,?,?,?,?,?,?)''',
                   (a, b, uci, source, mass,
                    structures.get(a, {}).get('boards'),
                    structures.get(a, {}).get('entry_mass'),
                    structures.get(a, {}).get('ply_mean')))
        written += 1
    db.commit()
    return written


def main_trajectories(edges, entry_structure, top=12, initial_mass=1.0):
    """Highest-mass structural trajectories (chains through the transition graph).

    Masses are conditional on the start family when initial_mass is 1.0; pass the
    family's entering mass to read them as absolute game probabilities.
    """
    outgoing = defaultdict(list)
    for (a, b, uci), mass in edges.items():
        outgoing[a].append((b, uci, mass))
    for a in outgoing:
        outgoing[a].sort(key=lambda t: -t[2])

    trajectories = []

    def walk(structure, mass, path, seen, depth):
        if depth > 8 or not outgoing.get(structure):
            trajectories.append((mass, tuple(path)))
            return
        total = sum(m for _b, _u, m in outgoing[structure])
        if not total:
            trajectories.append((mass, tuple(path)))
            return
        extended = False
        for b, uci, m in outgoing[structure][:2]:       # follow up to two branches
            if b in seen:
                continue
            extended = True
            walk(b, mass * (m / total), path + [b], seen | {b}, depth + 1)
        if not extended:
            trajectories.append((mass, tuple(path)))

    walk(entry_structure, initial_mass, [entry_structure], {entry_structure}, 0)
    trajectories.sort(key=lambda t: -t[0])
    merged = {}
    for mass, path in trajectories:
        merged[path] = max(merged.get(path, 0.0), mass)
    rows = sorted(((list(path), mass) for path, mass in merged.items()), key=lambda t: -t[1])
    return rows[:top]


def coverage_of_set(edges, entry_structure, selected, structures=None, leak=None):
    """Family-conditioned mass that reaches at least one selected family.

    Absorbing propagation on the structural DAG: when mass arrives at a selected
    family it is counted and removed. Because the graph is a DAG and a game crosses
    each edge once, the result is a game probability, and overlapping selections
    automatically earn only their marginal coverage — five near-identical families
    cannot fake five times the knowledge.
    """
    selected = set(selected)
    outgoing = defaultdict(list)
    for (a, b, uci), mass in edges.items():
        outgoing[a].append((b, mass))
    leak = leak or {}
    totals = {a: sum(m for _b, m in v) + leak.get(a, 0.0) for a, v in outgoing.items()}

    order = []
    depth = {entry_structure: 0}
    stack = [entry_structure]
    while stack:
        node = stack.pop()
        order.append(node)
        for b, _m in outgoing.get(node, []):
            if b not in depth:
                depth[b] = depth[node] + 1
                stack.append(b)
    order.sort(key=lambda node: depth[node])

    mass_at = defaultdict(float)
    mass_at[entry_structure] = 1.0
    covered = 0.0
    for node in order:
        arriving = mass_at.get(node, 0.0)
        if not arriving:
            continue
        if node in selected:
            covered += arriving
            continue                                   # absorbed
        total = totals.get(node, 0.0)
        if not total:
            continue
        for destination, mass in outgoing.get(node, []):
            mass_at[destination] += arriving * (mass / total)
    return covered, mass_at
