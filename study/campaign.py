"""Bounded, information-targeted evaluation campaign.

Three jobs:

1. `build_candidates` — rank the positions whose evaluation would most change the
   curriculum model, keeping every reason for selection as its own column.
2. `plan_budget` — node budget and runtime per tier from the measured engine rate.
3. `estimate_template_ev` — value template knowledge against exact decision nodes
   per unit of learning burden (the research question of this phase).

Node budgets, not depth targets. Nothing in this module starts an engine run; the
runner (`run_campaign`) is gated behind an explicit confirmation flag and a node
ceiling so a campaign cannot be launched by accident.
"""
import argparse
import json
import math
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

import chess
import chess.engine

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import (curriculum, db as studydb, evals as evals_module, metrics,  # noqa: E402
                   structure_flow, templates)

NODE_TIERS = {'A': 5_000_000, 'B': 25_000_000, 'C': 100_000_000}
MULTIPV = 5

REASONS = ('high_reach', 'mature_template', 'behavioural_disagreement',
           'decision_priority', 'deviation_priority', 'split_involvement',
           'frontier_sensitivity')

SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluation_candidate (
    position_key       BLOB PRIMARY KEY,
    fen                TEXT NOT NULL,
    role               TEXT NOT NULL,
    structure_id       TEXT NOT NULL,
    reach              REAL NOT NULL,
    ply                INTEGER NOT NULL,
    reasons_json       TEXT NOT NULL,      -- {reason: component value}
    reason_count       INTEGER NOT NULL,
    score              REAL NOT NULL,      -- declared convenience, not ground truth
    tier               TEXT NOT NULL,      -- A | B | C
    promotion_watch    INTEGER NOT NULL,   -- 1 = re-examine after tier A
    template_id        TEXT,
    has_eval           INTEGER NOT NULL,
    best_eval_source   TEXT,
    best_eval_depth    INTEGER,
    best_eval_nodes    INTEGER,
    has_wdl            INTEGER NOT NULL,
    needs_eval         INTEGER NOT NULL,
    selected           INTEGER NOT NULL,
    campaign           TEXT NOT NULL DEFAULT 'phase1'
);
CREATE TABLE IF NOT EXISTS evaluation_plan (
    tier           TEXT PRIMARY KEY,
    positions      INTEGER NOT NULL,
    nodes_per      INTEGER NOT NULL,
    multipv        INTEGER NOT NULL,
    total_nodes    INTEGER NOT NULL,
    seconds        REAL NOT NULL,
    rationale      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS template_sample (
    structure_id  TEXT NOT NULL,
    position_key  BLOB NOT NULL,
    stratum       REAL NOT NULL,        -- quantile of reach the board falls in
    weight        REAL NOT NULL,        -- flow reach used for the flow-weighted estimate
    PRIMARY KEY (structure_id, position_key)
);
"""

THRESHOLDS = {
    'reach': 0.002,                  # 0.2% of domain games pass through the position
    'js_bits': 0.10,                 # expert vs ordinary distribution divergence
    'decision_priority': 0.0005,     # stored study priority (reach x gap)
    'deviation_priority': 0.0005,
    'frontier_items': 8,             # items in the current frontier counted as sensitive
}


def ensure_schema(db):
    db.executescript(SCHEMA)
    db.commit()


# ------------------------------------------------------------------ candidates
def _best_eval(db):
    """{position_key: (source, depth, nodes, has_wdl, multipv)} best cached evaluation."""
    out = {}
    for row in db.execute('''SELECT p.position_key, e.source, e.depth, e.nodes, e.multipv, e.pvs_json
                             FROM position p JOIN eval e ON e.fen = p.fen'''):
        has_wdl = any(pv.get('wdl') for pv in json.loads(row['pvs_json']))
        rank = (0 if row['source'] == 'lichess_cloud' else 1, -(row['depth'] or 0),
                -(row['multipv'] or 0))
        current = out.get(row['position_key'])
        if current and current[0] <= rank:
            continue
        out[row['position_key']] = (rank, row['source'], row['depth'], row['nodes'],
                                    has_wdl, row['multipv'])
    return {key: value[1:] for key, value in out.items()}


def _insufficient(meta, min_depth=20):
    """An evaluation is insufficient if it is shallow, single-PV, or has no WDL."""
    if meta is None:
        return True, 'no evaluation'
    source, depth, nodes, has_wdl, multipv = meta
    if not has_wdl:
        return True, 'no WDL in cached evaluation'
    if (multipv or 0) < MULTIPV:
        return True, f'multipv {multipv} < {MULTIPV}'
    if source != 'lichess_cloud' and (depth or 0) < min_depth:
        return True, f'depth {depth} below {min_depth} and not a cloud evaluation'
    return False, ''


def _frontier_scope(db, run_id=6, source='local2200'):
    items = curriculum.build_items(db, source, run_id, criterion='expert')
    evals = curriculum.load_evals(db)
    values = curriculum.position_values(db, evals, items)
    _rows, chosen, _best = curriculum.frontier(items, values, k_max=THRESHOLDS['frontier_items'])
    scope = defaultdict(list)
    for item in chosen:
        for key in item['keys']:
            scope[key].append(item['item_id'])
    return scope, items


def build_candidates(db, run_id=6, source='local2200', limit=1200, verbose=True):
    """Score every position for information value; keep the top `limit`."""
    ensure_schema(db)
    positions, moves = structure_flow.load_graph_inputs(db)
    reach = curriculum.load_reach(db, source)
    cached = _best_eval(db)
    pool = templates.candidate_pool(db, source)
    template_of = {}
    for row in pool:
        for key in curriculum.family_positions(db, row['structure_id']):
            template_of[key] = row['structure_id']
    split_families = {row['structure_id'] for row in db.execute(
        "SELECT DISTINCT structure_id FROM template_split WHERE decision='split'")}
    decision = {row['position_key']: row['study_priority_wp'] for row in db.execute(
        'SELECT position_key, study_priority_wp FROM metric WHERE run_id=?', (run_id,))}
    deviation = {row['position_key']: row['deviation_priority'] for row in db.execute(
        '''SELECT position_key, MAX(deviation_priority) deviation_priority FROM deviation
           WHERE run_id=? GROUP BY position_key''', (run_id,))}
    frontier_scope, _items = _frontier_scope(db, run_id, source)
    fens = {row[0]: row[1] for row in db.execute('SELECT position_key, fen FROM position')}

    rows = []
    for key, meta in positions.items():
        reasons = {}
        if reach.get(key, {}).get('reach', 0.0) >= THRESHOLDS['reach']:
            reasons['high_reach'] = reach[key]['reach']
        if key in template_of:
            reasons['mature_template'] = reach.get(key, {}).get('reach', 0.0)
        dist_expert = curriculum.load_distributions(db, key, source)
        dist_pop = curriculum.load_distributions(db, key, 'lichess')
        if dist_expert and dist_pop:
            js, overlap = metrics.js_divergence(dist_expert, dist_pop)
            top_expert = max(dist_expert, key=dist_expert.get)
            top_pop = max(dist_pop, key=dist_pop.get)
            if js is not None and (js >= THRESHOLDS['js_bits'] or top_expert != top_pop):
                reasons['behavioural_disagreement'] = js
        if (decision.get(key) or 0.0) >= THRESHOLDS['decision_priority']:
            reasons['decision_priority'] = decision[key]
        if (deviation.get(key) or 0.0) >= THRESHOLDS['deviation_priority']:
            reasons['deviation_priority'] = deviation[key]
        structure = meta['structure']
        if structure in split_families:
            reasons['split_involvement'] = reach.get(key, {}).get('reach', 0.0)
        if key in frontier_scope:
            reasons['frontier_sensitivity'] = float(len(frontier_scope[key]))
        if not reasons:
            continue
        needs, why = _insufficient(cached.get(key))
        if not needs:
            continue
        score = _score(reasons)
        rows.append({
            'position_key': key, 'fen': fens.get(key), 'role': meta['role'], 'structure_id': structure,
            'reach': reach.get(key, {}).get('reach', 0.0), 'ply': meta['ply'],
            'reasons': reasons, 'reason_count': len(reasons), 'score': score,
            'template_id': template_of.get(key), 'needs_reason': why,
            'cached': cached.get(key),
        })
    rows.sort(key=lambda r: (-r['score'], -r['reach'], r['position_key']))
    selected = rows[:limit]
    _assign_tiers(selected)
    for row in selected:
        meta = row['cached']
        row['has_eval'] = 1 if meta else 0
        row['best_eval_source'] = meta[0] if meta else None
        row['best_eval_depth'] = meta[1] if meta else None
        row['best_eval_nodes'] = meta[2] if meta else None
        row['has_wdl'] = 1 if (meta and meta[3]) else 0
    if verbose:
        print(f'candidates with at least one reason and an insufficient evaluation: {len(rows)}; '
              f'selected {len(selected)}')
    return selected, rows


def _score(reasons):
    """Declared convenience score: sum of components normalised by their thresholds.

    The components stay in `reasons_json`; this number exists only to order the
    sheet and is never treated as ground truth.
    """
    weights = {'high_reach': 1.0, 'mature_template': 1.5, 'behavioural_disagreement': 2.0,
               'decision_priority': 2.0, 'deviation_priority': 1.5, 'split_involvement': 1.5,
               'frontier_sensitivity': 2.0}
    total = 0.0
    for reason, value in reasons.items():
        if reason in ('behavioural_disagreement',):
            total += weights[reason] * min(3.0, value / THRESHOLDS['js_bits'])
        elif reason in ('decision_priority', 'deviation_priority'):
            total += weights[reason] * min(3.0, value / THRESHOLDS[reason])
        elif reason == 'frontier_sensitivity':
            total += weights[reason] * min(2.0, value / 2.0)
        else:
            total += weights[reason]
    return total


def _assign_tiers(rows):
    """Tier A for everything; B/C only where deeper search could change the ranking.

    Promotion is decided by the *measured* tier-A result (a near-tie between the
    best move and the move the curriculum teaches, or a threshold-adjacent value),
    so every promoted position is marked `promotion_watch` here and the runner
    re-tests it after tier A. Tier C is reserved for the very top of the sheet.
    """
    for index, row in enumerate(rows):
        row['tier'] = 'A'
        row['promotion_watch'] = 1 if (
            row['reason_count'] >= 2
            and ('decision_priority' in row['reasons'] or 'mature_template' in row['reasons']
                 or 'behavioural_disagreement' in row['reasons'])
            and row['reach'] >= 0.002) else 0
    # tie-breaking rule for the ceiling tier: strongest signals, highest reach
    ceiling = [row for row in rows if row['promotion_watch'] and row['reason_count'] >= 3][:25]
    for row in ceiling:
        row['tier'] = 'C'
    deep = [row for row in rows[:int(len(rows) * 0.12)] if row['promotion_watch'] and row['tier'] == 'A']
    for row in deep:
        row['tier'] = 'B'


def persist_candidates(db, rows, campaign='phase1'):
    ensure_schema(db)
    for row in rows:
        db.execute('''INSERT OR REPLACE INTO evaluation_candidate(
            position_key, fen, role, structure_id, reach, ply, reasons_json, reason_count, score,
            tier, promotion_watch, template_id, has_eval, best_eval_source, best_eval_depth,
            best_eval_nodes, has_wdl, needs_eval, selected, campaign)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (row['position_key'], row['fen'], row['role'], row['structure_id'], row['reach'],
             row['ply'], json.dumps(row['reasons']), row['reason_count'], row['score'],
             row['tier'], row['promotion_watch'], row['template_id'], row.get('has_eval', 0),
             row.get('best_eval_source'), row.get('best_eval_depth'), row.get('best_eval_nodes'),
             row.get('has_wdl', 0), 1, 1, campaign))
    db.commit()
    return db.execute('SELECT COUNT(*) FROM evaluation_candidate').fetchone()[0]


# ---------------------------------------------------------------------- budget
def plan_budget(rows, nps=505_000.0, hash_mb=64):
    plan = {}
    for tier, nodes in NODE_TIERS.items():
        count = sum(1 for row in rows if row['tier'] == tier)
        seconds = count * nodes / nps
        plan[tier] = {'positions': count, 'nodes_per': nodes, 'multipv': MULTIPV,
                      'total_nodes': count * nodes, 'seconds': seconds,
                      'rationale': {
                          'A': 'baseline for every selected position',
                          'B': 'promoted: measured tier-A result is threshold-adjacent',
                          'C': 'promoted: strongest multi-signal positions only'}[tier]}
    wall_seconds = sum(entry['seconds'] for entry in plan.values())
    return plan, {'total_nodes': sum(e['total_nodes'] for e in plan.values()),
                  'engine_seconds': wall_seconds,
                  'engine_hours': wall_seconds / 3600.0,
                  'nps_measured': nps, 'threads': 1, 'hash_mb': hash_mb}


def persist_plan(db, plan):
    ensure_schema(db)
    for tier, entry in plan.items():
        db.execute('''INSERT OR REPLACE INTO evaluation_plan(tier, positions, nodes_per, multipv,
                                                              total_nodes, seconds, rationale)
                      VALUES(?,?,?,?,?,?,?)''',
                   (tier, entry['positions'], entry['nodes_per'], entry['multipv'],
                    entry['total_nodes'], entry['seconds'], entry['rationale']))
    db.commit()


# ------------------------------------------------------------- template samples
def template_samples(db, source='local2200', per_template=30, min_boards=20, seed=1337):
    """Flow-weighted stratified sample of boards per mature template.

    Boards are stratified by reach quantile so a sample covers both the common and
    the rare configurations of the family, and drawn with probability proportional
    to reach inside each stratum. Deterministic for a given seed.
    """
    ensure_schema(db)
    rng = random.Random(seed)
    reach = curriculum.load_reach(db, source)
    out = {}
    for row in templates.candidate_pool(db, source):
        keys = curriculum.family_positions(db, row['structure_id'])
        weighted = sorted(((reach.get(key, {}).get('reach', 0.0), key) for key in keys),
                          key=lambda pair: -pair[0])
        if not weighted:
            continue
        size = max(min_boards, min(per_template, len(weighted)))
        strata = defaultdict(list)
        for rank, (weight, key) in enumerate(weighted):
            strata[min(9, rank * 10 // max(1, len(weighted)))].append((weight, key))
        chosen = []
        for stratum in sorted(strata):
            bucket = strata[stratum]
            take = max(1, size // max(1, len(strata)))
            total = sum(weight for weight, _key in bucket) or 1.0
            for _ in range(take):
                pick = rng.random() * total
                running = 0.0
                for weight, key in bucket:
                    running += weight
                    if running >= pick:
                        chosen.append((stratum / 10.0, weight, key))
                        break
        seen = {}
        for stratum, weight, key in chosen:
            seen.setdefault(key, (stratum, weight))
        out[row['structure_id']] = [(key, stratum, weight) for key, (stratum, weight) in seen.items()]
        for key, stratum, weight in out[row['structure_id']]:
            db.execute('''INSERT OR REPLACE INTO template_sample(structure_id, position_key,
                                                                 stratum, weight)
                          VALUES(?,?,?,?)''', (row['structure_id'], key, stratum, weight))
    db.commit()
    return out


# --------------------------------------------------------------- template value
def value_of_answers(evals, scores, dist_answers, dist_pop, best, answers=None):
    """EP loss of the ordinary distribution and of a taught answer set, at one board.

    Both losses are measured against the same baseline `best` (the best evaluated
    move at this position), never against a best computed inside each distribution:
    using different baselines would manufacture a gain out of the comparison itself.
    """
    pop_moves = {u: p for u, p in dist_pop.items() if u in scores and p > 0}
    if not pop_moves or best is None:
        return {'loss_pop': None, 'loss_taught': None, 'gain': None, 'mass_covered': 0.0}
    normalised_pop, total_pop = metrics.renormalise(pop_moves)
    loss_pop = sum(prob * max(0.0, best - scores[uci]['ep_wp'])
                   for uci, prob in normalised_pop.items())
    taught = {u: 1.0 for u in answers if u in scores} if isinstance(answers, (set, list, tuple)) \
        else {u: dist_answers.get(u, 0.0) for u in scores if u in dist_answers}
    if not taught or sum(taught.values()) <= 0:
        return {'loss_pop': loss_pop, 'loss_taught': None, 'gain': None,
                'mass_covered': total_pop}
    normalised, _total = metrics.renormalise(taught)
    loss_taught = sum(prob * max(0.0, best - scores[uci]['ep_wp'])
                      for uci, prob in normalised.items())
    return {'loss_pop': loss_pop, 'loss_taught': loss_taught,
            'gain': loss_pop - loss_taught, 'mass_covered': total_pop}


def estimate_template_ev(db, samples, source='local2200', population='auto'):
    """Flow-weighted EV of knowing each template, from cached evaluations only.

    EV per board = ordinary-play regret minus the regret of the moves the template
    teaches. Flow-weighted across the sample, then extrapolated to the template's
    whole scope using the scope's flow mass. Boards without evaluations are counted
    and reported, never silently dropped.
    """
    evals = curriculum.load_evals(db)
    report = {}
    for structure_id, boards in samples.items():
        weights, gains, missing, covered = [], [], 0, 0.0
        for key, _stratum, weight in boards:
            _role, fen = curriculum.position_role(db, key)
            scores = evals.get(fen)
            dist_pop = (curriculum.load_population(db, key)[1] if population == 'auto'
                        else curriculum.load_distributions(db, key, population))
            dist_answers = curriculum.load_distributions(db, key, source)
            if not scores or not dist_pop or not dist_answers:
                missing += 1
                continue
            # the template's taught answers: the strong moves at this board
            answers = set(curriculum._answers_from_distribution(dist_answers, min_share=0.35, cap=3))
            best = max((scores[u]['ep_wp'] for u in scores), default=None)
            if best is None:
                missing += 1
                continue
            value = value_of_answers(evals, scores, dist_answers, dist_pop, best)
            if not value or value['gain'] is None:
                missing += 1
                continue
            weights.append(weight)
            gains.append(weight * value['gain'])
            covered += weight
        ev_per_board = (sum(gains) / sum(weights)) if weights else None
        report[structure_id] = {'boards_sampled': len(boards), 'boards_evaluated': len(weights),
                                'boards_missing_evaluations': missing,
                                'ev_per_board': ev_per_board,
                                'flow_weight_evaluated': covered}
    return report


def compare_per_cost(db, run_id=6, source='local2200'):
    """Value per complexity unit: templates vs exact decision nodes vs deviations."""
    items = curriculum.build_items(db, source, run_id, criterion='expert')
    evals = curriculum.load_evals(db)
    values = curriculum.position_values(db, evals, items)
    _rows, chosen, _best = curriculum.frontier(items, values, k_max=60)
    buckets = defaultdict(lambda: {'value': 0.0, 'cost': 0.0, 'items': 0,
                                   'boards_unavailable': 0})
    for item in chosen:
        gain = 0.0
        for key in item['keys']:
            entry = values.get(key)
            if not entry:
                continue
            # a per-item value of None means "unknown", never zero: charging it as 0.0
            # would quietly claim a known-zero gain where the data is simply absent
            candidate = entry['per_item'].get(item['item_id'])
            if candidate is None:
                buckets[item['type']]['boards_unavailable'] += 1
                continue
            gain += entry['reach'] * candidate
        bucket = buckets[item['type']]
        bucket['value'] += gain
        bucket['cost'] += item['complexity']['cost']
        bucket['items'] += 1
    out = {}
    for kind, bucket in buckets.items():
        out[kind] = {'items': bucket['items'], 'value': bucket['value'], 'cost': bucket['cost'],
                     'value_per_cost': bucket['value'] / bucket['cost'] if bucket['cost'] else None,
                     'positions': sum(i['positions'] for i in chosen if i['type'] == kind)}
    return out


# ---------------------------------------------------------------------- runner
def analyse_at_nodes(binary, board, nodes, multipv=MULTIPV, engine=None):
    """One engine call at a node limit. Returns the stored-evaluation shape."""
    engine = engine or chess.engine.SimpleEngine.popen_uci(binary)
    engine.configure({'Threads': 1, 'Hash': 64, 'UCI_ShowWDL': True})
    started = time.time()
    infos = engine.analyse(board, chess.engine.Limit(nodes=nodes), multipv=multipv)
    if isinstance(infos, dict):
        infos = [infos]
    elapsed = time.time() - started
    pvs = []
    for info in infos:
        line = info.get('pv') or []
        if not line:
            continue
        score = info['score'].relative
        wdl = None
        if hasattr(info['score'], 'wdl'):
            try:
                w = info['score'].pov(board.turn).wdl(model='sf')
                wdl = (w.wins, w.draws, w.losses)
            except Exception:
                wdl = None
        pvs.append({'uci': line[0].uci(), 'line': [m.uci() for m in line],
                    'cp': None if score.is_mate() else score.score(),
                    'mate': score.score() if score.is_mate() else None, 'wdl': wdl})
    return {'source': 'local_stockfish', 'engine': engine.id.get('name', 'stockfish'),
            'depth': infos[0].get('depth') if infos else None,
            'nodes': infos[0].get('nodes') if infos else None,
            'time_ms': int(elapsed * 1000), 'pov': 'side_to_move', 'pvs': pvs}


def run_campaign(db, binary, node_ceiling, campaign='phase1', confirm=False, verbose=True):
    """Execute the campaign. Refuses to run without confirm and a node ceiling."""
    if not confirm:
        raise SystemExit('refusing to start an engine run without --confirm')
    ensure_schema(db)
    spent, done = 0, 0
    rows = db.execute('''SELECT position_key, fen FROM evaluation_candidate
                         WHERE selected=1 AND campaign=? ORDER BY
                         CASE tier WHEN 'A' THEN 0 WHEN 'B' THEN 1 ELSE 2 END, -score''',
                      (campaign,)).fetchall()
    engine = chess.engine.SimpleEngine.popen_uci(binary)
    store = evals_module.EvalStore(db, cloud_threshold=30)
    for key, fen in rows:
        tier = db.execute('SELECT tier FROM evaluation_candidate WHERE position_key=?',
                          (key,)).fetchone()[0]
        nodes = NODE_TIERS[tier]
        if spent + nodes > node_ceiling:
            break
        board = chess.Board(fen + ' 0 1')
        result = analyse_at_nodes(binary, board, nodes, engine=engine)
        store.store(fen, MULTIPV, result)
        spent += result['nodes'] or nodes
        done += 1
        if verbose:
            print(f'{done}: {key.hex()[:12]} tier {tier} nodes {result["nodes"]:,} '
                  f'({result["time_ms"] / 1000:.1f}s) spent {spent:,}')
    engine.quit()
    return {'evaluations': done, 'nodes_spent': spent}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=studydb.DEFAULT_ANALYSIS_DB)
    parser.add_argument('--run-id', type=int, default=6)
    parser.add_argument('--source', default='local2200')
    parser.add_argument('--limit', type=int, default=1200)
    parser.add_argument('--nps', type=float, default=505_000.0)
    parser.add_argument('--samples', type=int, default=30)
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--confirm', action='store_true')
    parser.add_argument('--binary', default='/home/jasper/.local/opt/stockfish/stockfish/'
                                           'stockfish-linux-x86-64-universal')
    parser.add_argument('--node-ceiling', type=int, default=0)
    args = parser.parse_args()
    db = studydb.connect(args.db, create=False)
    if args.confirm:
        print(json.dumps(run_campaign(db, args.binary, args.node_ceiling), indent=1))
        return
    selected, all_rows = build_candidates(db, args.run_id, args.source, args.limit)
    plan, totals = plan_budget(selected, nps=args.nps)
    samples = template_samples(db, args.source, args.samples, seed=1337)
    ev = estimate_template_ev(db, samples, args.source)
    ranked = sorted(all_rows, key=lambda r: -r['score'])
    rank_of = {row['position_key']: index + 1 for index, row in enumerate(ranked)}
    order = sorted(selected, key=lambda r: -r['score'])
    print(json.dumps({
        'candidates_with_reasons': len(all_rows),
        'selected': len(selected),
        'selected_by_tier': {t: sum(1 for r in selected if r['tier'] == t) for t in NODE_TIERS},
        'selected_truncated_at': max(rank_of[r['position_key']] for r in selected),
        'score_of_last_selected': order[-1]['score'] if order else None,
        'reasons_histogram': {reason: sum(1 for r in selected if reason in r['reasons'])
                              for reason in REASONS},
        'template_samples': {k: len(v) for k, v in samples.items()},
        'template_ev': ev,
        'plan': plan, 'totals': totals,
    }, indent=1))
    if args.write:
        print('persisted candidates:', persist_candidates(db, selected))
        persist_plan(db, plan)
    db.close()


if __name__ == '__main__':
    main()
