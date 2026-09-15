"""Tier-A promotions, exactly as predefined before the campaign ran.

The rule (STUDY-EVAL-CAMPAIGN.md §2, frozen before execution):

* promote to **B** when the Tier-A ranking is threshold-adjacent — the second-best
  move is within 0.03 EP of the best;
* promote to **C** when two independent moves are within 0.015 EP of the best *and*
  the position carries at least three reason classes, in which case C replaces B
  (a position is deepened once, not twice).

Nothing here is tuned on observed results: the thresholds are module constants,
the node budgets come from `campaign.NODE_TIERS`, MultiPV comes from
`campaign.MULTIPV`, and the engine version string travels unchanged so the job
identities stay the same as the shipped sheet.

Promotion decisions are made from the *measured Tier-A evaluations*, which is what
"promote only when deeper analysis could materially change the result" means in
practice: a Tier-A ranking that is already decisive is never re-run.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import campaign, metrics  # noqa: E402

SECOND_MOVE_GAP_EP = 0.03      # tier A -> B: second-best within this of the best
THIRD_MOVE_GAP_EP = 0.015      # tier A -> C: third-best within this of the best
THIRD_REASON_FLOOR = 3         # tier A -> C: reason classes required


def pv_ep(pv):
    """Expected points of one PV entry, in the mover's point of view."""
    if pv.get('wdl'):
        w, d, l = pv['wdl']
        if (w + d + l) > 0:
            return (w + d / 2) / (w + d + l)
    return metrics.ep_from_score(cp=pv.get('cp'), mate=pv.get('mate'))


def rank_pvs(result):
    """[(uci, ep)] sorted best-first, dropping entries without an EP."""
    ranked = []
    for pv in (result or {}).get('pvs') or []:
        ep = pv_ep(pv)
        if ep is not None and pv.get('uci'):
            ranked.append((pv['uci'], ep))
    ranked.sort(key=lambda pair: -pair[1])
    return ranked


def decide(result, reason_count):
    """Return ('C'|'B'|None, evidence dict) for one Tier-A evaluation."""
    ranked = rank_pvs(result)
    if len(ranked) < 2:
        return None, {'reason': 'fewer than two evaluated moves at tier A',
                      'ranked': len(ranked)}
    best = ranked[0][1]
    gap_second = best - ranked[1][1]
    gap_third = best - ranked[2][1] if len(ranked) >= 3 else None
    evidence = {'best_uci': ranked[0][0], 'best_ep': best,
                'gap_second_ep': gap_second, 'gap_third_ep': gap_third,
                'reasons': reason_count, 'evaluated_moves': len(ranked)}
    if (gap_third is not None and gap_third <= THIRD_MOVE_GAP_EP
            and (reason_count or 0) >= THIRD_REASON_FLOOR):
        return 'C', evidence
    if gap_second <= SECOND_MOVE_GAP_EP:
        return 'B', evidence
    return None, evidence


def load_records(paths):
    """Usable Tier-A records only; failure-path lines are counted, never guessed at."""
    usable, skipped = {}, 0
    for path in paths:
        for line in open(path, encoding='utf-8'):
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            result = record.get('result')
            if not result or not result.get('pvs'):
                skipped += 1
                continue
            usable[record['job_id']] = result
    return usable, skipped


def candidate_meta(db):
    """{fen: (tier, reason_count, promotion_watch)} from the approved sheet."""
    meta = {}
    for row in db.execute('''SELECT fen, tier, reason_count, promotion_watch
                             FROM evaluation_candidate WHERE selected=1'''):
        meta[row['fen']] = (row['tier'], row['reason_count'], row['promotion_watch'])
    return meta


def promotion_jobs(records, meta, engine_version, job_to_fen, existing=None,
                   campaign_name='phase1'):
    """Build the promotion bundle: at most one deeper tier per position.

    `records` is {job_id: tier-A result}, `job_to_fen` maps job id to FEN (the
    result records carry no FEN), and `existing` is the set of job ids already in
    the ledger, so a deeper evaluation that already exists is never duplicated.
    """
    from study import remote as remote_module
    existing = existing or set()
    jobs, decisions = [], []
    for job_id, result in records.items():
        fen = job_to_fen.get(job_id)
        info = meta.get(fen) if fen else None
        if fen is None:
            decisions.append({'job_id': job_id, 'fen': None, 'tier': None, 'decision': None,
                              'reason': 'job id not in the planned ledger'})
            continue
        if info is None:
            decisions.append({'job_id': job_id, 'fen': fen, 'tier': None,
                              'decision': None, 'reason': 'not in the approved sheet'})
            continue
        tier, reason_count, watch = info
        if tier != 'A' or not watch:
            decisions.append({'job_id': job_id, 'fen': fen, 'tier': tier,
                              'decision': None,
                              'reason': 'not a tier-A promotion-watch position'})
            continue
        target, evidence = decide(result, reason_count)
        row = {'job_id': job_id, 'fen': fen, 'tier': tier, 'decision': target}
        row.update(evidence)
        if target is None or target == 'A':
            decisions.append(row)
            continue
        nodes = campaign.NODE_TIERS[target]
        new_id = remote_module.job_id_for(fen, campaign.MULTIPV, nodes, engine_version)
        row['promoted_job_id'] = new_id
        if new_id in existing:
            row['decision'] = None
            row['reason'] = 'deeper evaluation already present'
            decisions.append(row)
            continue
        jobs.append({'job_id': new_id, 'fen': fen, 'multipv': campaign.MULTIPV,
                     'nodes_budget': nodes, 'engine_version': engine_version,
                     'tier': target, 'campaign': campaign_name, 'attempts': 0})
        decisions.append(row)
    return jobs, decisions


def write_bundle(jobs, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        for job in jobs:
            handle.write(json.dumps(job) + '\n')
    return {'jobs': len(jobs), 'path': str(path),
            'nodes': sum(job['nodes_budget'] for job in jobs)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', type=Path, nargs='+', required=True)
    parser.add_argument('--db', type=Path, required=True,
                        help='analysis database (read-only read of the approved sheet)')
    parser.add_argument('--engine-version', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--decisions', type=Path)
    args = parser.parse_args()

    from study import db as studydb
    db = studydb.connect(args.db, create=False)
    meta = candidate_meta(db)
    job_to_fen, ledger = {}, set()
    try:
        for row in db.execute('SELECT job_id, fen FROM engine_job'):
            job_to_fen[row[0]] = row[1]
            ledger.add(row[0])
    except Exception:
        print('note: no engine_job ledger in the target database; promotions need the '
              'planned ledger to resolve job ids to FENs', file=sys.stderr)
    db.close()

    records, skipped = load_records(args.results)
    jobs, decisions = promotion_jobs(records, meta, args.engine_version, job_to_fen,
                                     existing=ledger)
    report = write_bundle(jobs, args.out)
    counts = {}
    for row in decisions:
        counts[str(row.get('decision'))] = counts.get(str(row.get('decision')), 0) + 1
    report.update({'tier_a_records': len(records), 'skipped_failure_path': skipped,
                   'decisions': counts, 'engine_version': args.engine_version,
                   'budgets': {tier: campaign.NODE_TIERS[tier] for tier in ('B', 'C')},
                   'multipv': campaign.MULTIPV})
    if args.decisions:
        with open(args.decisions, 'w', encoding='utf-8') as handle:
            for row in decisions:
                handle.write(json.dumps(row) + '\n')
    print(json.dumps(report, indent=1))


if __name__ == '__main__':
    main()
