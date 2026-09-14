"""Scandinavian (1.e4 d5) proof-of-concept study run.

Staged, budgeted pipeline:

  stage 0  build the domain subgraph (read-only against the ingestion DBs)
  stage 1  cheap filtering on game frequency and data support (no engine)
  stage 2  evaluations: Lichess cloud first (depth >= threshold), local Stockfish
           only for positions the cloud cannot cover, under an explicit budget
  stage 3  metrics: expected regret for both distributions, knowledge gap,
           study priority, Jensen-Shannon as a descriptive companion
  stage 4  opponent deviations at Black-to-move nodes
  stage 5  structural compression baseline and attractor detection
  report   inspectable top-N tables (text + CSV + JSON) written to analysis/

Usage:
  python3 study/run_scandinavian.py --engine ~/.local/opt/stockfish/stockfish/stockfish-linux-x86-64-universal
"""
import argparse
import csv
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

import chess

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import db as studydb           # noqa: E402
from study import domain, evals, flow, metrics  # noqa: E402
from study.structures import render_pawns  # noqa: E402

DEFAULT_ENGINE = str(Path.home() / '.local/opt/stockfish/stockfish/stockfish-linux-x86-64-universal')
EXPERT_PREFERENCE = ('local2200', 'masters')       # strong-player distributions we trust
LICHESS_SOURCE = 'lichess'


# --------------------------------------------------------------------- queries
def load_sources(db, key):
    return {row['source']: dict(row) for row in
            db.execute('SELECT * FROM position_source WHERE position_key=?', (key,))}


def load_flow(db, key, source):
    row = db.execute('SELECT * FROM position_flow WHERE position_key=? AND source=?',
                     (key, source)).fetchone()
    return dict(row) if row else None


def load_moves(db, key, source):
    return {row['uci']: dict(row) for row in
            db.execute('SELECT * FROM move_source WHERE position_key=? AND source=?', (key, source))}


def pick_expert(sources):
    """Highest-priority expert distribution with enough games, or None."""
    for name in EXPERT_PREFERENCE:
        row = sources.get(name)
        if row and row['games'] and row['games'] >= metrics.MIN_EXPERT_GAMES:
            return name
    return None


# --------------------------------------------------------------------- report
def write_report(outdir, payload, rows):
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / 'summary.json').write_text(json.dumps(payload, indent=1))
    with (outdir / 'top20.csv').open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else [])
        writer.writeheader()
        writer.writerows(rows)
    return outdir


def spearman(pairs):
    """Rank correlation for the cp-vs-WDL ranking sanity check."""
    n = len(pairs)
    if n < 3:
        return None
    def ranks(values):
        order = sorted(range(n), key=lambda i: values[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out
    a, b = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return None if den == 0 else num / den


# ---------------------------------------------------------------------- stages
def stage_filter(db, args):
    """Cheap filtering: positions with real support in an expert source and in Lichess."""
    rows = db.execute('''SELECT p.position_key, p.fen, p.role, p.ply, p.structure_id, p.dev_status
                         FROM position p ORDER BY p.ply''').fetchall()
    candidates = []
    for row in rows:
        sources = load_sources(db, row['position_key'])
        expert = pick_expert(sources)
        lichess = sources.get(LICHESS_SOURCE)
        if not expert or not lichess or not lichess['games']:
            continue
        if lichess['games'] < args.min_lichess_games:
            continue
        expert_moves = load_moves(db, row['position_key'], expert)
        lichess_moves = load_moves(db, row['position_key'], LICHESS_SOURCE)
        if len(expert_moves) < 2 or len(lichess_moves) < 2:
            continue
        flow_row = load_flow(db, row['position_key'], expert) or {}
        candidates.append({'key': row['position_key'], 'fen': row['fen'], 'role': row['role'],
                           'ply': row['ply'], 'structure_id': row['structure_id'],
                           'dev_status': row['dev_status'],
                           'expert': expert, 'expert_moves': expert_moves,
                           'lichess_moves': lichess_moves, 'sources': sources,
                           'reach_flow': flow_row.get('reach_flow'),
                           'count_ratio': flow_row.get('count_ratio'),
                           'flow_flags': flow_row.get('flags') or '',
                           'n_parents_total': flow_row.get('n_parents_total'),
                           'path_count': flow_row.get('path_count')})
    candidates.sort(key=lambda c: -(c['sources'][c['expert']]['games']))
    return candidates[:args.max_candidates]


def stage_evaluate(db, candidates, store, args):
    """Resolve engine values for every move that either distribution actually plays."""
    evaluated = []
    for index, cand in enumerate(candidates, 1):
        board = chess.Board(cand['fen'] + ' 0 1')
        expert_dist = {u: m['share'] for u, m in cand['expert_moves'].items()}
        lichess_dist = {u: m['share'] for u, m in cand['lichess_moves'].items()}
        expert_counts = {u: m['games'] for u, m in cand['expert_moves'].items()}
        lichess_counts = {u: m['games'] for u, m in cand['lichess_moves'].items()}
        kept_e, dropped_e = metrics.filter_moves(expert_dist, counts=expert_counts)
        kept_l, dropped_l = metrics.filter_moves(lichess_dist, counts=lichess_counts)
        needed = sorted(set(kept_e) | set(kept_l), key=lambda u: -(kept_l.get(u, 0) + kept_e.get(u, 0)))
        # Engine budget guard: evaluate only the moves that carry real mass, and
        # keep the discarded tail visible through the coverage numbers.
        if len(needed) > args.max_moves_per_node:
            needed = needed[:args.max_moves_per_node]
        move_evals, _parent = store.resolve_moves(board, needed, multipv=args.multipv,
                                                  allow_local=not args.no_local)
        usable = {u: v for u, v in move_evals.items() if v.ep_cp is not None}
        if len(usable) < 2:
            continue
        cand.update({'board': board, 'expert_dist': expert_dist, 'lichess_dist': lichess_dist,
                     'expert_dropped': dropped_e, 'lichess_dropped': dropped_l,
                     'move_evals': usable, 'needed': needed})
        evaluated.append(cand)
        if args.verbose and index % 25 == 0:
            print(f'  evaluated {index}/{len(candidates)} '
                  f'(cloud {store.budget.cloud_requests}, local {store.budget.local_searches})')
    return evaluated


def stage_metrics(db, run_id, candidates, args):
    results = []
    for cand in candidates:
        evals_cp = {u: {'ep_cp': v.ep_cp, 'ep_wp': v.ep_wp, 'mate': v.mate}
                    for u, v in cand['move_evals'].items()}
        r_l = metrics.expected_regret(cand['lichess_dist'], evals_cp, method='cp')
        r_e = metrics.expected_regret(cand['expert_dist'], evals_cp, method='cp')
        gap = metrics.knowledge_gap(r_l.regret, r_e.regret)
        js, overlap = metrics.js_divergence(cand['lichess_dist'], cand['expert_dist'])
        sources = cand['sources']
        expert_row, lichess_row = sources[cand['expert']], sources[LICHESS_SOURCE]
        reach = cand.get('reach_flow')
        if reach is None:
            reach = expert_row['reach_prob']
        coverage = min(r_l.mass_covered, r_e.mass_covered)
        confidence = 'low'
        if expert_row['games'] >= 300 and lichess_row['games'] >= 5000 and coverage >= 0.90:
            confidence = 'high'
        elif expert_row['games'] >= 100 and lichess_row['games'] >= 1000 and coverage >= 0.80:
            confidence = 'medium'
        flags = []
        if coverage < metrics.MIN_EVAL_COVERAGE:
            flags.append('low_eval_coverage')
        if cand['expert'] == 'masters':
            flags.append('expert=masters_fallback')
        if any(v.source == 'lichess_cloud' for v in cand['move_evals'].values()):
            flags.append('cloud_eval_only_cp')
        if cand['expert_dropped'] > 0.05 or cand['lichess_dropped'] > 0.05:
            flags.append('dropped_rare_moves')
        if 'fed_from_outside_family' in (cand.get('flow_flags') or ''):
            flags.append('fed_from_outside_family')
        if 'mass_leaves_domain' in (cand.get('flow_flags') or ''):
            flags.append('mass_leaves_domain')
        priority = metrics.study_priority(reach, gap)
        results.append({**cand, 'r_l': r_l, 'r_e': r_e, 'gap': gap, 'js': js, 'overlap': overlap,
                        'reach': reach, 'coverage': coverage, 'confidence': confidence,
                        'flags': flags, 'priority': priority,
                        'expert_games': expert_row['games'], 'lichess_games': lichess_row['games']})
        db.execute('''INSERT OR REPLACE INTO metric(run_id, position_key, role, expert_source,
                        eval_source, eval_depth, eval_coverage, n_moves, lichess_games, expert_games,
                        regret_lichess_wp, regret_expert_wp, gap_wp, regret_lichess_cp,
                        regret_expert_cp, gap_cp, js_divergence, reach_prob, reach_source,
                        entry_games, study_priority_wp, study_priority_cp, confidence, flags)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (run_id, cand['key'], cand['role'], cand['expert'],
                    'mixed' if len({v.source for v in cand['move_evals'].values()}) > 1
                    else next(iter(cand['move_evals'].values())).source,
                    min((v.depth or 0) for v in cand['move_evals'].values()) or None,
                    coverage, len(cand['move_evals']), lichess_row['games'], expert_row['games'],
                    None, None, None, r_l.regret, r_e.regret, gap, js, reach, cand['expert'],
                    expert_row['entry_games'], None, priority, confidence, ','.join(flags)))
    db.commit()
    return results


def stage_deviations(db, run_id, results, store, args):
    """Opponent (Black) decisions only: probability of deviation x objective concession."""
    rows = []
    for cand in results:
        if cand['role'] != 'black_to_move':
            continue                      # the opponent moves at Black-to-move nodes
        expert_moves = cand['expert_moves']
        top = max(expert_moves.items(), key=lambda kv: kv[1]['share'])[0]
        main = {u for u, m in expert_moves.items() if m['share'] >= args.main_share or u == top}
        evals_cp = {u: {'ep_cp': v.ep_cp} for u, v in cand['move_evals'].items()}
        best = max((v for v in evals_cp.values() if v['ep_cp'] is not None),
                   key=lambda v: v['ep_cp'], default=None)
        if best is None:
            continue
        ep_best = best['ep_cp']
        for uci, move in expert_moves.items():
            prob = move['share']
            if uci in main:
                klass = 'main'
            value = cand['move_evals'].get(uci)
            concession = None if value is None or value.ep_cp is None else max(0.0, ep_best - value.ep_cp)
            if uci not in main:
                klass = metrics.classify_deviation(concession)
            if prob < args.min_deviation_prob and klass != 'main':
                continue
            best_response = best_response_san = best_response_ep = None
            if uci not in main:
                child = cand['board'].copy(stack=False)
                child.push_uci(uci)
                child_eval = store.get(child, multipv=1, allow_local=not args.no_local)
                if child_eval and child_eval['pvs']:
                    pv = child_eval['pvs'][0]
                    best_response = pv['uci']
                    best_response_san = child.san(chess.Move.from_uci(pv['uci']))
                    ep_child = metrics.ep_from_score(cp=pv.get('cp'), mate=pv.get('mate'), wdl=pv.get('wdl'))
                    best_response_ep = metrics.flip_ep(ep_child)
            priority = metrics.deviation_priority(prob, concession) if klass != 'main' else None
            rows.append({'run_id': run_id, 'key': cand['key'], 'fen': cand['fen'], 'uci': uci,
                         'san': move['san'], 'mover': 'black', 'prob_lichess': None,
                         'prob_expert': prob, 'prob_used': prob, 'prob_source': cand['expert'],
                         'ep_best': ep_best, 'ep_move': None if value is None else value.ep_cp,
                         'concession_wp': None, 'concession_cp': concession, 'class': klass,
                         'best_response_uci': best_response, 'best_response_san': best_response_san,
                         'best_response_ep': best_response_ep,
                         'empirical_top_reply_uci': None,
                         'deviation_priority': priority,
                         'flags': ','.join(cand['flags'])})
            db.execute('''INSERT OR REPLACE INTO deviation(run_id, position_key, uci, san, mover,
                            prob_lichess, prob_expert, prob_used, prob_source, ep_best, ep_move,
                            concession_wp, concession_cp, class, best_response_uci, best_response_san,
                            best_response_ep, empirical_top_reply_uci, deviation_priority, flags)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (run_id, cand['key'], uci, move['san'], 'black', None, prob, prob,
                        cand['expert'], ep_best, None if value is None else value.ep_cp, None,
                        concession, klass, best_response, best_response_san, best_response_ep,
                        None, priority, ','.join(cand['flags'])))
    db.commit()
    rows.sort(key=lambda r: -(r['deviation_priority'] or 0))
    return rows


def stage_structures(db, run_id, results, args):
    """Compression baseline: exact positions vs exact pawn structures, no clustering."""
    struct_positions = defaultdict(list)
    for row in db.execute('SELECT position_key, ply, structure_id, dev_status, pieces_json FROM position'):
        struct_positions[row['structure_id']].append(dict(row))
    # mass per position from the strongest available expert source
    mass = {}
    for row in db.execute('''SELECT position_key, source, games, entry_games, reach_prob
                             FROM position_source WHERE source IN ('local2200','masters','lichess')'''):
        current = mass.get(row['position_key'])
        if current is None or (row['source'] == 'local2200'):
            mass[row['position_key']] = row['reach_prob'] or 0.0
    edges = defaultdict(list)
    for row in db.execute('SELECT child_key, parent_key, uci, games FROM provenance WHERE source=?',
                          ('local2200',)):
        edges[row['parent_key']].append((row['child_key'], row['uci'], row['games']))

    stats = []
    for structure_id, positions in struct_positions.items():
        reaches = [mass.get(p['position_key'], 0.0) for p in positions]
        lower = max(reaches) if reaches else 0.0
        upper = min(1.0, sum(reaches))
        parents = set()
        for p in positions:
            for row in db.execute('SELECT DISTINCT parent_key FROM provenance WHERE child_key=?',
                                  (p['position_key'],)):
                parents.add(row['parent_key'])
        dev_signatures = len({(p['dev_status'], p['pieces_json']) for p in positions})
        stats.append({'structure_id': structure_id, 'n_positions': len(positions),
                      'coverage_lower': lower, 'coverage_upper': upper,
                      'peak_games': max((mass.get(p['position_key'], 0.0) for p in positions),
                                        default=0.0),
                      'n_parents': len(parents), 'n_development_signatures': dev_signatures,
                      'positions': positions})
    total_lower = sum(s['coverage_lower'] for s in stats) or 1.0
    for s in stats:
        s['share_of_coverage'] = s['coverage_lower'] / total_lower
    stats.sort(key=lambda s: -s['coverage_lower'])
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=studydb.DEFAULT_ANALYSIS_DB)
    parser.add_argument('--engine', default=DEFAULT_ENGINE)
    parser.add_argument('--outdir', type=Path)
    parser.add_argument('--skip-domain', action='store_true')
    parser.add_argument('--no-local', action='store_true', help='cloud evaluations only')
    parser.add_argument('--max-candidates', type=int, default=400)
    parser.add_argument('--min-lichess-games', type=int, default=metrics.MIN_LICHESS_GAMES)
    parser.add_argument('--max-cloud', type=int, default=2500)
    parser.add_argument('--max-moves-per-node', type=int, default=6)
    parser.add_argument('--max-local', type=int, default=120)
    parser.add_argument('--depth', type=int, default=18)
    parser.add_argument('--multipv', type=int, default=5)
    parser.add_argument('--cloud-depth', type=int, default=30)
    parser.add_argument('--cloud-delay', type=float, default=0.6)
    parser.add_argument('--main-share', type=float, default=0.10)
    parser.add_argument('--min-deviation-prob', type=float, default=0.02)
    parser.add_argument('--wdl-top', type=int, default=20)
    parser.add_argument('--top', type=int, default=20)
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()

    db = studydb.connect(args.db)
    run_id = studydb.start_run(db, 'scandinavian-1e4-d5', vars(args),
                               notes='proof-of-concept study run')
    started = time.time()
    print(f'run_id={run_id} analysis db={args.db}')

    if not args.skip_domain:
        print('stage 0: building domain')
        summary = domain.build(db, run_id)
        print('  ', json.dumps(summary))
        studydb.set_meta(db, 'domain_summary', summary)
    print('stage 0b: reachability flow')
    flow_summary = {source: flow.compute(db, source) for source in ('local2200', 'lichess')}
    print('  ', json.dumps(flow_summary))
    studydb.set_meta(db, 'flow_summary', flow_summary)

    budget = evals.EvalBudget(cloud_max=args.max_cloud, local_max=args.max_local,
                              local_depth=args.depth, local_multipv=args.multipv)
    cloud = evals.CloudClient(delay=args.cloud_delay, budget=budget)
    local = None if args.no_local else evals.LocalClient(args.engine, budget=budget)
    store = evals.EvalStore(db, cloud=cloud, local=local, cloud_threshold=args.cloud_depth,
                            budget=budget)

    print('stage 1: filtering candidates')
    candidates = stage_filter(db, args)
    print(f'  {len(candidates)} candidates with expert + Lichess support')

    print('stage 2: evaluations (cloud first)')
    evaluated = stage_evaluate(db, candidates, store, args)
    print(f'  {len(evaluated)} positions with >=2 evaluated moves; '
          f'cloud={budget.cloud_requests} local={budget.local_searches}')

    print('stage 3: metrics')
    results = stage_metrics(db, run_id, evaluated, args)

    print('stage 4: opponent deviations')
    deviations = stage_deviations(db, run_id, results, store, args)

    print('stage 5: structures')
    structures_stats = stage_structures(db, run_id, results, args)

    # ---- WDL check on the top candidates (local multi-PV gives WDL for all moves at once)
    wdl_pairs = []
    if local is not None and not args.no_local:
      try:
        top = sorted([r for r in results if r['priority']], key=lambda r: -r['priority'])
        for cand in top[:args.wdl_top]:
            local_result = local.analyse(cand['board'], multipv=min(len(cand['needed']), 8),
                                         depth=args.depth)
            if not local_result:
                break
            store.store(evals.canonical_fen(cand['board']), min(len(cand['needed']), 8), local_result)
            evals_wp = {}
            for pv in local_result['pvs']:
                evals_wp[pv['uci']] = {'ep_wp': metrics.ep_from_score(cp=pv.get('cp'), mate=pv.get('mate'),
                                                                      wdl=pv.get('wdl')), 'ep_cp': None,
                                       'mate': pv.get('mate')}
            r_l = metrics.expected_regret(cand['lichess_dist'], evals_wp, method='wp')
            r_e = metrics.expected_regret(cand['expert_dist'], evals_wp, method='wp')
            gap_wp = metrics.knowledge_gap(r_l.regret, r_e.regret)
            reach = cand['reach'] or 0.0
            priority_wp = metrics.study_priority(reach, gap_wp)
            wdl_pairs.append((cand['priority'], priority_wp or 0.0))
            db.execute('''UPDATE metric SET regret_lichess_wp=?, regret_expert_wp=?, gap_wp=?,
                            study_priority_wp=? WHERE run_id=? AND position_key=?''',
                       (r_l.regret, r_e.regret, gap_wp, priority_wp, run_id, cand['key']))
            cand['gap_wp'] = gap_wp
            cand['priority_wp'] = priority_wp
        db.commit()
        local.close()
      except Exception as error:      # never lose the report to a WDL-stage failure
        print(f'  WDL stage failed: {type(error).__name__}: {error}')
        if local is not None:
            try:
                local.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ report
    ranked = sorted([r for r in results if r['priority'] is not None], key=lambda r: -r['priority'])
    white_ranked = [r for r in ranked if r['role'] == 'white_to_move']
    stamp = time.strftime('%Y%m%d-%H%M%S')
    outdir = args.outdir or (ROOT / 'analysis' / f'scandinavian-{stamp}')

    table = []
    for rank, cand in enumerate(white_ranked[:args.top], 1):
        table.append({
            'rank': rank, 'ply': cand['ply'], 'fen': cand['fen'], 'repertoire': cand['fen'],
            'study_priority_cp': round(cand['priority'], 5),
            'study_priority_wp': round(cand.get('priority_wp') or 0.0, 5),
            'reach_flow': round(cand['reach'] or 0.0, 5),
            'count_ratio': None if cand.get('count_ratio') is None else round(cand['count_ratio'], 3),
            'n_parents_total': cand.get('n_parents_total'),
            'paths': None if cand.get('path_count') is None else int(cand['path_count']),
            'expert_games': cand['expert_games'], 'expert_source': cand['expert'],
            'lichess_games': cand['lichess_games'],
            'gap_cp': round(cand['gap'], 5), 'gap_wp': round(cand.get('gap_wp') or 0.0, 5),
            'regret_lichess_cp': round(cand['r_l'].regret, 5),
            'regret_expert_cp': round(cand['r_e'].regret, 5),
            'js_divergence': None if cand['js'] is None else round(cand['js'], 4),
            'overlap': None if cand['overlap'] is None else round(cand['overlap'], 3),
            'best_engine_move': cand['r_l'].best_uci,
            'eval_coverage': round(cand['coverage'], 3),
            'confidence': cand['confidence'], 'flags': ' '.join(cand['flags']),
        })

    structure_pawns = {row['structure_id']: (int(row['white_pawns'], 16), int(row['black_pawns'], 16))
                       for row in db.execute('SELECT structure_id, white_pawns, black_pawns FROM structure')}
    payload = {
        'run_id': run_id, 'domain': 'scandinavian-1e4-d5',
        'generated': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'candidates': len(candidates), 'evaluated_positions': len(results),
        'white_to_move_ranked': len(white_ranked),
        'cloud_requests': budget.cloud_requests, 'local_searches': budget.local_searches,
        'eval_stats': store.stats,
        'rank_correlation_cp_vs_wp': spearman(wdl_pairs),
        'structures': {
            'exact_positions_in_domain': db.execute('SELECT COUNT(*) FROM position').fetchone()[0],
            'exact_pawn_structures_in_domain':
                db.execute('SELECT COUNT(*) FROM structure').fetchone()[0],
            'structure_coverage_top10': [
                {'structure_id': s['structure_id'], 'n_positions': s['n_positions'],
                 'coverage_lower': round(s['coverage_lower'], 4),
                 'coverage_upper': round(s['coverage_upper'], 4),
                 'n_parents': s['n_parents'],
                 'development_signatures': s['n_development_signatures'],
                 'pawns': render_pawns(*structure_pawns.get(s['structure_id'], (0, 0)))}
                for s in structures_stats[:10]]},
        'top_deviations': [{'fen': d['fen'], 'san': d['san'], 'class': d['class'],
                            'prob_expert': round(d['prob_used'], 4),
                            'concession_cp': None if d['concession_cp'] is None else round(d['concession_cp'], 2),
                            'best_response': d['best_response_san'],
                            'deviation_priority': None if not d['deviation_priority'] else round(d['deviation_priority'], 5)}
                           for d in deviations[:20]],
        'elapsed_seconds': round(time.time() - started, 1),
    }
    write_report(outdir, payload, table)
    dev_rows = [{k: v for k, v in d.items() if k != 'key'} for d in deviations[:40]]
    if dev_rows:
        with (outdir / 'deviations.csv').open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(dev_rows[0].keys()))
            writer.writeheader()
            writer.writerows(dev_rows)
    struct_rows = [{'structure_id': s['structure_id'], 'n_positions': s['n_positions'],
                    'coverage_lower': round(s['coverage_lower'], 5),
                    'coverage_upper': round(s['coverage_upper'], 5),
                    'n_parents': s['n_parents'],
                    'development_signatures': s['n_development_signatures']}
                   for s in structures_stats]
    with (outdir / 'structures.csv').open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(struct_rows[0].keys()))
        writer.writeheader()
        writer.writerows(struct_rows)
    studydb.finish_run(db, run_id)

    print(f'\nreport written to {outdir}')
    print(f'cloud requests={budget.cloud_requests} local searches={budget.local_searches} '
          f'elapsed={payload["elapsed_seconds"]}s rank_corr(cp,wp)={payload["rank_correlation_cp_vs_wp"]}')
    db.close()


if __name__ == '__main__':
    main()
