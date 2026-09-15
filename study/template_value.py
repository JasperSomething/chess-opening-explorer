"""Template value: per-board gain distributions and value per burden dimension.

Two questions the mean alone cannot answer:

1. is a prescriptive template broadly useful across its scope, or is its aggregate
   value produced by a handful of boards? Answered by the distribution of per-board
   gains plus a concentration measure (the share of reach-weighted gain contributed
   by the single best board);
2. what does the value cost? Answered per *dimension* of the existing complexity
   vector (exact moves, flexible slots, recognition conditions, exceptions, boards),
   not only per scalar cost, because the burden dimensions are not interchangeable
   for a human learner.

Definitions are frozen: gain at a board is the ordinary-play loss minus the loss of
the rule's taught answer set, both measured against the same baseline best move.
Boards without the data needed are counted as unavailable, never as zero gain.
"""
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import campaign, curriculum, rules  # noqa: E402


def rule_board_gains(db, rule, source='local2200', population='auto'):
    """Per-board gains for one prescriptive rule, with availability accounting."""
    evals = curriculum.load_evals(db)
    reach = curriculum.load_reach(db, source)
    scope = rules.rule_scope(db, rule, source)
    boards = []
    unavailable = {'no_evaluation': 0, 'no_population': 0,
                   'taught_moves_not_evaluated': 0}
    for key in scope:
        _role, fen = curriculum.position_role(db, key)
        scores = evals.get(fen)
        if not scores:
            unavailable['no_evaluation'] += 1
            continue
        dist_strong = curriculum.load_distributions(db, key, source)
        if population == 'auto':
            pop_source, dist_pop, pop_games = curriculum.load_population(db, key)
        else:
            pop_source = population
            dist_pop = curriculum.load_distributions(db, key, population)
            pop_games = sum(db.execute('SELECT games FROM move_source WHERE position_key=? '
                                       'AND source=?', (key, population)).fetchall() or [(0,)])
        if not dist_pop or not dist_strong:
            unavailable['no_population'] += 1
            continue
        best = max(entry['ep_wp'] for entry in scores.values())
        value = campaign.value_of_answers(evals, scores, dist_strong, dist_pop, best,
                                          answers=rule['answers'])
        if value['gain'] is None:
            # the rule's taught moves are not among the evaluated moves: the engine
            # never looked at them, so the board is unknown here, not zero-valued
            unavailable['taught_moves_not_evaluated'] += 1
            continue
        boards.append({'position_key': key.hex(), 'fen': fen,
                       'reach': reach.get(key, {}).get('reach', 0.0),
                       'gain': value['gain'], 'loss_pop': value['loss_pop'],
                       'loss_taught': value['loss_taught'],
                       'population_source': pop_source,
                       'population_games': (pop_games or 0)})
    return {'rule_id': rule['rule_id'], 'structure_id': rule['structure_id'],
            'role': rule['condition'].get('role'), 'answers': rule['answers'],
            'scope_boards': len(scope), 'boards': boards,
            'unavailable': sum(unavailable.values()),
            'unavailable_reasons': unavailable}


def distribution(gains):
    """Distribution of unweighted per-board gains plus concentration of the weighted total."""
    values = sorted(row['gain'] for row in gains)
    if not values:
        return {'boards': 0}
    weighted = sorted((row['reach'] * row['gain'] for row in gains), reverse=True)
    total_weighted = sum(weighted)
    positive = [w for w in weighted if w > 0]
    # a share of a non-positive total is not a meaningful number, so it is withheld
    concentrated = total_weighted > 0
    return {
        'boards': len(values),
        'min': values[0], 'p25': values[len(values) // 4],
        'median': statistics.median(values), 'p75': values[(3 * len(values)) // 4],
        'p90': values[min(len(values) - 1, int(0.9 * len(values)))], 'max': values[-1],
        'mean': statistics.fmean(values),
        'share_positive': sum(1 for v in values if v > 0) / len(values),
        'weighted_total': total_weighted,
        # concentration is measured against the positive gains only: dividing by the
        # net total would let loss-making boards make the top board exceed 100% of it
        'positive_total': sum(positive),
        'top_board_share_of_positive_total': (weighted[0] / sum(positive)
                                              if positive and weighted[0] > 0 else None),
        'top3_share_of_positive_total': (sum(weighted[:3]) / sum(positive)
                                         if positive else None),
        'negative_total': sum(w for w in weighted if w < 0),
    }


def per_burden_dimension(db, rules_list, source='local2200', population='auto'):
    """EV recovered beside each dimension of the learning burden, per rule and pooled."""
    evals = curriculum.load_evals(db)
    rows, pooled = [], {}
    prescriptive = [rule for rule in rules_list
                    if rule['kind'] == 'prescriptive' and rule['answers']]
    items = {item['rule_id']: item for item in rules.as_items(db, prescriptive, source)}
    for rule in prescriptive:
        detail = rule_board_gains(db, rule, source, population)
        stats = distribution(detail['boards'])
        if not stats.get('boards'):
            rows.append({'rule_id': rule['rule_id'], 'structure_id': rule['structure_id'],
                         'ev_available': False, 'boards_evaluated': 0,
                         'boards_unavailable': detail['unavailable']})
            continue
        item = items.get(rule['rule_id'])
        complexity = item['complexity'] if item else {}
        row = {'rule_id': rule['rule_id'], 'structure_id': rule['structure_id'],
               'ev_available': True, 'boards_evaluated': stats['boards'],
               'boards_unavailable': detail['unavailable'],
               'unavailable_reasons': detail['unavailable_reasons'],
               'scope_boards': detail['scope_boards'],
               'ev_weighted_total': stats['weighted_total'],
               'ev_per_evaluated_board': stats['mean'],
               'burden': {key: complexity.get(key) for key in
                          ('exact_moves', 'flexible_slots', 'conditions', 'exceptions',
                           'boards', 'cost')},
               'distribution': stats}
        for key in ('exact_moves', 'flexible_slots', 'conditions', 'exceptions', 'boards', 'cost'):
            dimension = complexity.get(key) or 0
            row[f'ev_per_{key}'] = stats['weighted_total'] / dimension if dimension else None
        rows.append(row)
        for key in ('exact_moves', 'flexible_slots', 'conditions', 'exceptions', 'boards',
                    'cost'):
            entry = pooled.setdefault(key, {'ev': 0.0, 'burden': 0.0})
            entry['ev'] += stats['weighted_total']
            entry['burden'] += complexity.get(key) or 0
    pooled_report = {key: {'ev': value['ev'], 'burden': value['burden'],
                           'ev_per_burden': (value['ev'] / value['burden'])
                           if value['burden'] else None}
                     for key, value in pooled.items()}
    return {'rules': rows, 'pooled_per_dimension': pooled_report}
