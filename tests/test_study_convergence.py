import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from study import convergence, promote  # noqa: E402


def pv(uci, cp=None, wdl=None):
    return {'uci': uci, 'cp': cp, 'wdl': wdl}


def result(*pvs):
    return {'pvs': list(pvs), 'depth': 20, 'nodes': 5_000_000}


class PromoteDecisionTests(unittest.TestCase):
    def test_ep_comes_from_wdl_when_present(self):
        self.assertAlmostEqual(promote.pv_ep(pv('a', wdl=(60, 20, 20))), 0.70)

    def test_ep_falls_back_to_centipawns(self):
        self.assertAlmostEqual(promote.pv_ep(pv('a', cp=0)), 0.5, places=3)

    def test_a_decisive_ranking_is_not_promoted(self):
        target, evidence = promote.decide(result(pv('a', wdl=(90, 10, 0)),
                                                 pv('b', wdl=(50, 50, 0))), reason_count=5)
        self.assertIsNone(target)
        self.assertGreater(evidence['gap_second_ep'], promote.SECOND_MOVE_GAP_EP)

    def test_a_near_tie_promotes_to_b(self):
        target, evidence = promote.decide(result(pv('a', wdl=(60, 20, 20)),
                                                 pv('b', wdl=(59, 20, 21))), reason_count=2)
        self.assertEqual(target, 'B')
        self.assertLessEqual(evidence['gap_second_ep'], promote.SECOND_MOVE_GAP_EP)

    def test_three_way_tie_with_enough_reasons_promotes_to_c(self):
        target, _evidence = promote.decide(result(pv('a', wdl=(60, 20, 20)),
                                                  pv('b', wdl=(60, 19, 21)),
                                                  pv('c', wdl=(60, 18, 22))),
                                           reason_count=3)
        self.assertEqual(target, 'C')

    def test_three_way_tie_without_enough_reasons_stops_at_b(self):
        target, _evidence = promote.decide(result(pv('a', wdl=(60, 20, 20)),
                                                  pv('b', wdl=(60, 19, 21)),
                                                  pv('c', wdl=(60, 18, 22))),
                                           reason_count=2)
        self.assertEqual(target, 'B')

    def test_a_single_evaluated_move_cannot_be_promoted(self):
        target, evidence = promote.decide(result(pv('a', wdl=(60, 20, 20))), reason_count=5)
        self.assertIsNone(target)
        self.assertIn('fewer than two', evidence['reason'])


class PromoteBundleTests(unittest.TestCase):
    def meta(self):
        return {'fen-a': ('A', 3, 1), 'fen-b': ('A', 2, 1), 'fen-c': ('B', 4, 0),
                'fen-d': ('A', 3, 0)}

    def records(self):
        tie = result(pv('x', wdl=(60, 20, 20)), pv('y', wdl=(59, 20, 21)))
        return {'job-a': tie, 'job-b': tie, 'job-c': tie, 'job-d': tie}

    def job_to_fen(self):
        return {'job-a': 'fen-a', 'job-b': 'fen-b', 'job-c': 'fen-c', 'job-d': 'fen-d'}

    def test_only_tier_a_watch_positions_promote(self):
        jobs, decisions = promote.promotion_jobs(self.records(), self.meta(), 'v',
                                                 self.job_to_fen())
        promoted_fens = {job['fen'] for job in jobs}
        self.assertEqual(promoted_fens, {'fen-a', 'fen-b'})   # c is tier B, d is not watched
        for job in jobs:
            self.assertEqual(job['nodes_budget'], 25_000_000)  # tier B budget, unchanged
            self.assertEqual(job['multipv'], 5)
            self.assertEqual(job['engine_version'], 'v')

    def test_an_existing_deeper_evaluation_is_never_duplicated(self):
        jobs, decisions = promote.promotion_jobs(self.records(), self.meta(), 'v',
                                                 self.job_to_fen())
        existing = {job['job_id'] for job in jobs}
        again, _decisions = promote.promotion_jobs(self.records(), self.meta(), 'v',
                                                   self.job_to_fen(), existing=existing)
        self.assertEqual(again, [])
        self.assertTrue(any(row.get('reason') == 'deeper evaluation already present'
                            for row in _decisions))

    def test_bundle_round_trips_into_a_ledger(self):
        jobs, _decisions = promote.promotion_jobs(self.records(), self.meta(), 'v',
                                                  self.job_to_fen())
        bundle = Path('/tmp/promote-test-bundle.jsonl')
        report = promote.write_bundle(jobs, bundle)
        self.assertEqual(report['jobs'], len(jobs))
        lines = [json.loads(line) for line in open(bundle)]
        self.assertEqual(len(lines), len(jobs))
        required = {'job_id', 'fen', 'multipv', 'nodes_budget', 'engine_version', 'tier'}
        for line in lines:
            self.assertTrue(required.issubset(line))


class ConvergenceMetricTests(unittest.TestCase):
    def test_budget_snapping_accepts_engine_overshoot_only(self):
        self.assertEqual(convergence._snap_budget(5_000_157), 5_000_000)
        self.assertEqual(convergence._snap_budget(25_100_000), 25_000_000)
        self.assertEqual(convergence._snap_budget(100_400_000), 100_000_000)
        self.assertIsNone(convergence._snap_budget(3_000_000))
        self.assertIsNone(convergence._snap_budget(50_000_000))

    def test_kendall_tau_of_identical_and_reversed_orders(self):
        self.assertAlmostEqual(convergence.kendall_tau(['a', 'b', 'c'], ['a', 'b', 'c']), 1.0)
        self.assertAlmostEqual(convergence.kendall_tau(['a', 'b', 'c'], ['c', 'b', 'a']), -1.0)

    def test_identical_evaluations_are_perfectly_stable(self):
        same = [pv('a', wdl=(60, 20, 20)), pv('b', wdl=(50, 40, 10)), pv('c', wdl=(40, 40, 20))]
        row = convergence.compare_pair(same, same)
        self.assertTrue(row['best_same'])
        self.assertTrue(row['order_same'])
        self.assertAlmostEqual(row['kendall_tau'], 1.0)
        self.assertAlmostEqual(row['best_ep_change'], 0.0)

    def test_a_flipped_best_move_is_detected(self):
        shallow = [pv('a', wdl=(55, 30, 15)), pv('b', wdl=(54, 30, 16))]
        deep = [pv('b', wdl=(58, 30, 12)), pv('a', wdl=(54, 30, 16))]
        row = convergence.compare_pair(shallow, deep)
        self.assertFalse(row['best_same'])
        self.assertEqual(row['best_deep'], 'b')
        self.assertGreater(abs(row['best_ep_change']), 0)

    def test_taught_answer_loss_is_zero_when_the_answer_is_best(self):
        pvs = [pv('a', wdl=(60, 20, 20)), pv('b', wdl=(50, 30, 20))]
        self.assertAlmostEqual(convergence.taught_answer_loss(pvs, ['a']), 0.0)

    def test_taught_answer_loss_measures_a_wrong_answer(self):
        pvs = [pv('a', wdl=(60, 20, 20)), pv('b', wdl=(40, 20, 40))]
        loss = convergence.taught_answer_loss(pvs, ['b'])
        self.assertAlmostEqual(loss, 0.70 - 0.50, places=3)

    def test_summary_reports_shares_and_means(self):
        same = [pv('a', wdl=(60, 20, 20)), pv('b', wdl=(50, 40, 10))]
        flipped = [pv('b', wdl=(58, 30, 12)), pv('a', wdl=(50, 40, 10))]
        rows = [convergence.compare_pair(same, same), convergence.compare_pair(flipped, same)]
        summary = convergence.summarise(rows)
        self.assertEqual(summary['positions'], 2)
        self.assertAlmostEqual(summary['best_move_unchanged_share'], 0.5)

    def test_item_value_changes_flags_only_material_moves(self):
        rows = [
            {'fen': 'f1', 'shallow': 5_000_000, 'deep': 25_000_000,
             'taught_answer': {'i1': {'change': 0.001}, 'i2': {'change': 0.02}}},
            {'fen': 'f2', 'shallow': 5_000_000, 'deep': 25_000_000,
             'taught_answer': {'i1': {'change': 0.004}}},
        ]
        changed = convergence.item_value_changes(rows, threshold=0.005)
        # stable items are reported too, with changed = 0: silence is not evidence
        self.assertEqual(changed['i1']['entries'], 2)
        self.assertEqual(changed['i1']['changed'], 0)
        self.assertEqual(changed['i2']['changed'], 1)
        self.assertAlmostEqual(changed['i2']['worst'], 0.02)

    def test_25m_to_100m_pairs_are_reported_as_unavailable_not_invented(self):
        budget_evals = {'fen1': {5_000_000: [pv('a', wdl=(60, 20, 20))],
                                 25_000_000: [pv('a', wdl=(61, 20, 19))]}}
        table = convergence.convergence_table(budget_evals)
        self.assertIn((5_000_000, 25_000_000), table)
        self.assertNotIn((25_000_000, 100_000_000), table)


if __name__ == '__main__':
    unittest.main()
