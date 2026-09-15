"""Evaluation convergence across node budgets, and the item-level consequences.

Answers, for every position evaluated at more than one node budget:

* best-move stability — is the top move the same?
* MultiPV ordering stability — exact order match, Kendall tau, Spearman rho;
* EP/WDL changes — per-move expected points and the WDL vector of the top move;
* taught-answer regret changes — the loss of the answer set the curriculum teaches;
* whether any curriculum item's value or classification changes.

Depth is reported as a distribution per tier and never used as a stopping rule;
the search budget is nodes.

Structural note that the report must state rather than hide: the frozen sheet
assigns one tier per position and the promotion rule deepens tier-A positions by
one step, so 5M->25M and 5M->100M pairs exist by construction while 25M->100M
pairs do not. Any such comparison is reported as unavailable with the reason,
not silently omitted.
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import metrics  # noqa: E402

BUDGETS = (5_000_000, 25_000_000, 100_000_000)
PAIRS = ((5_000_000, 25_000_000), (25_000_000, 100_000_000), (5_000_000, 100_000_000))


def load_budget_evals(db, source='local_stockfish'):
    """{fen: {nodes_budget: [pvs]}} from the cached evaluations of the campaign."""
    out = defaultdict(dict)
    for row in db.execute('''SELECT fen, nodes, pvs_json FROM eval
                             WHERE source=? AND nodes IS NOT NULL''', (source,)):
        nodes = row['nodes']
        key = _snap_budget(nodes)
        if key is None:
            continue
        out[row['fen']].setdefault(key, json.loads(row['pvs_json']))
    return dict(out)


def _snap_budget(nodes):
    """Snap a reported node count to the budget that produced it (engines overshoot)."""
    best, distance = None, None
    for budget in BUDGETS:
        gap = abs(nodes - budget) / budget
        if gap <= 0.02 and (distance is None or gap < distance):
            best, distance = budget, gap
    return best


def pv_ep(pv):
    if pv.get('wdl'):
        w, d, l = pv['wdl']
        if (w + d + l) > 0:
            return (w + d / 2) / (w + d + l)
    return metrics.ep_from_score(cp=pv.get('cp'), mate=pv.get('mate'))


def ranked(pvs):
    rows = [(pv['uci'], pv_ep(pv), tuple(pv['wdl']) if pv.get('wdl') else None)
            for pv in pvs if pv.get('uci')]
    rows = [row for row in rows if row[1] is not None]
    rows.sort(key=lambda row: -row[1])
    return rows


def kendall_tau(order_a, order_b):
    """Rank correlation on the shared moves of two orderings, in [-1, 1]."""
    shared = [uci for uci in order_a if uci in order_b]
    if len(shared) < 2:
        return None
    rank_a = {uci: index for index, uci in enumerate(order_a)}
    rank_b = {uci: index for index, uci in enumerate(order_b)}
    concordant = discordant = 0
    for i in range(len(shared)):
        for j in range(i + 1, len(shared)):
            a = rank_a[shared[i]] - rank_a[shared[j]]
            b = rank_b[shared[i]] - rank_b[shared[j]]
            if a * b > 0:
                concordant += 1
            elif a * b < 0:
                discordant += 1
    total = concordant + discordant
    return (concordant - discordant) / total if total else None


def spearman(items):
    """Spearman rho from [(value_a, value_b)] pairs."""
    n = len(items)
    if n < 3:
        return None
    mean_a = sum(a for a, _b in items) / n
    mean_b = sum(b for _a, b in items) / n
    cov = sum((a - mean_a) * (b - mean_b) for a, b in items)
    var_a = sum((a - mean_a) ** 2 for a, _b in items) ** 0.5
    var_b = sum((b - mean_b) ** 2 for _a, b in items) ** 0.5
    return cov / (var_a * var_b) if var_a and var_b else None


def compare_pair(shallow_pvs, deep_pvs):
    """Stability metrics between the same position at two node budgets."""
    a, b = ranked(shallow_pvs), ranked(deep_pvs)
    if not a or not b:
        return None
    order_a = [row[0] for row in a]
    order_b = [row[0] for row in b]
    shared = [uci for uci in order_a if uci in order_b]
    eps_a = {row[0]: row[1] for row in a}
    eps_b = {row[0]: row[1] for row in b}
    pair_eps = [(eps_a[uci], eps_b[uci]) for uci in shared]
    return {
        'best_same': order_a[0] == order_b[0],
        'best_shallow': order_a[0], 'best_deep': order_b[0],
        'order_same': order_a == order_b,
        'order_shared_same': [u for u in order_a if u in order_b] ==
                             [u for u in order_b if u in order_a],
        'kendall_tau': kendall_tau(order_a, order_b),
        'spearman': spearman(pair_eps),
        'best_ep_shallow': a[0][1], 'best_ep_deep': b[0][1],
        'best_ep_change': b[0][1] - a[0][1],
        'wdl_shallow': a[0][2], 'wdl_deep': b[0][2],
        'shared_moves': len(shared),
        'top_move_entered': order_b[0] not in order_a,
        'per_move_ep_change': {uci: eps_b[uci] - eps_a[uci] for uci in shared},
    }


def taught_answer_loss(pvs, answers):
    """Loss of the answer set the curriculum teaches, measured at one budget."""
    rows = ranked(pvs)
    if not rows:
        return None
    best = rows[0][1]
    eps = {row[0]: row[1] for row in rows}
    usable = {uci: eps[uci] for uci in answers if uci in eps}
    if not usable:
        return None
    normalised, _total = metrics.renormalise({uci: 1.0 for uci in usable})
    return sum(prob * max(0.0, best - eps[uci]) for uci, prob in normalised.items())


def convergence_table(budget_evals, items_by_position=None, answers_by_item=None,
                      value_delta=0.005):
    """Per-position stability rows plus the item-level consequences."""
    positions = {}
    for fen, by_budget in budget_evals.items():
        for shallow, deep in PAIRS:
            if shallow in by_budget and deep in by_budget:
                row = compare_pair(by_budget[shallow], by_budget[deep])
                if row is None:
                    continue
                row.update({'fen': fen, 'shallow': shallow, 'deep': deep})
                if items_by_position and answers_by_item:
                    covering = items_by_position.get(fen, [])
                    losses = {}
                    for item_id in covering:
                        answers = answers_by_item.get(item_id) or []
                        if not answers:
                            continue
                        loss_s = taught_answer_loss(by_budget[shallow], answers)
                        loss_d = taught_answer_loss(by_budget[deep], answers)
                        if loss_s is None or loss_d is None:
                            continue
                        losses[item_id] = {'loss_shallow': loss_s, 'loss_deep': loss_d,
                                           'change': loss_d - loss_s}
                    row['taught_answer'] = losses
                positions.setdefault((shallow, deep), []).append(row)
    return positions


def summarise(rows):
    """Aggregate stability for one budget pair."""
    if not rows:
        return {'positions': 0}
    total = len(rows)
    taus = [row['kendall_tau'] for row in rows if row['kendall_tau'] is not None]
    rhos = [row['spearman'] for row in rows if row['spearman'] is not None]
    changes = [abs(row['best_ep_change']) for row in rows]
    taught = [entry['change'] for row in rows
              for entry in (row.get('taught_answer') or {}).values()]
    verdict_changes = sum(1 for row in rows
                          if 0 < abs(row['best_ep_change']) and not row['best_same'])
    return {
        'positions': total,
        'best_move_unchanged': sum(1 for row in rows if row['best_same']),
        'best_move_unchanged_share': sum(1 for row in rows if row['best_same']) / total,
        'order_identical': sum(1 for row in rows if row['order_same']),
        'order_identical_share': sum(1 for row in rows if row['order_same']) / total,
        'mean_kendall_tau': sum(taus) / len(taus) if taus else None,
        'mean_spearman': sum(rhos) / len(rhos) if rhos else None,
        'mean_abs_best_ep_change': sum(changes) / len(changes),
        'max_abs_best_ep_change': max(changes),
        'positions_with_best_move_change': verdict_changes,
        'taught_answer_entries': len(taught),
        'mean_taught_loss_change': sum(taught) / len(taught) if taught else None,
        'max_abs_taught_loss_change': max((abs(v) for v in taught), default=None),
        'top_move_entered': sum(1 for row in rows if row['top_move_entered']),
    }


def item_value_changes(rows, threshold=0.005):
    """Items whose taught-answer value or classification changes with the budget."""
    out = defaultdict(lambda: {'entries': 0, 'changed': 0, 'worst': 0.0, 'positions': []})
    for row in rows:
        for item_id, entry in (row.get('taught_answer') or {}).items():
            bucket = out[item_id]
            bucket['entries'] += 1
            if abs(entry['change']) >= threshold:
                bucket['changed'] += 1
                bucket['worst'] = max(bucket['worst'], abs(entry['change']))
                bucket['positions'].append({'fen': row['fen'], 'change': entry['change'],
                                            'shallow': row['shallow'], 'deep': row['deep']})
    return dict(out)


def depth_distribution(db, source='local_stockfish'):
    """Achieved depth per tier, as a distribution. Depth is metadata, not a rule."""
    out = defaultdict(list)
    for row in db.execute('''SELECT e.fen, e.nodes, e.depth FROM eval e
                             WHERE e.source=? AND e.nodes IS NOT NULL''', (source,)):
        budget = _snap_budget(row['nodes'])
        if budget is None:
            continue
        out[budget].append(row['depth'])
    report = {}
    for budget, depths in out.items():
        depths.sort()
        n = len(depths)
        report[budget] = {
            'positions': n, 'mean': sum(depths) / n, 'median': depths[n // 2],
            'p10': depths[int(n * 0.1)], 'p90': depths[int(n * 0.9)],
            'min': depths[0], 'max': depths[-1],
        }
    return report
