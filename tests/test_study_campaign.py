import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from study import campaign  # noqa: E402


class ScoreTests(unittest.TestCase):
    def test_reasons_accumulate_and_are_ordered_by_evidence(self):
        weak = campaign._score({'high_reach': 0.01})
        strong = campaign._score({'high_reach': 0.01, 'behavioural_disagreement': 0.50})
        self.assertLess(weak, strong)

    def test_components_are_capped_so_one_signal_cannot_dominate(self):
        huge = campaign._score({'decision_priority': 1000.0})
        capped = campaign._score({'decision_priority': campaign.THRESHOLDS['decision_priority'] * 3})
        self.assertAlmostEqual(huge, capped)


class InsufficientTests(unittest.TestCase):
    def test_missing_evaluation_is_insufficient(self):
        need, why = campaign._insufficient(None)
        self.assertTrue(need)
        self.assertIn('no evaluation', why)

    def test_shallow_local_evaluation_is_insufficient(self):
        need, why = campaign._insufficient(('local_stockfish', 18, 0, True, 5))
        self.assertTrue(need)
        self.assertIn('depth 18', why)

    def test_deep_cloud_evaluation_is_sufficient(self):
        need, _why = campaign._insufficient(('lichess_cloud', 42, 8661914, True, 5))
        self.assertFalse(need)

    def test_single_pv_evaluation_is_insufficient_even_when_deep(self):
        need, why = campaign._insufficient(('lichess_cloud', 42, 8661914, True, 1))
        self.assertTrue(need)
        self.assertIn('multipv', why)

    def test_missing_wdl_is_insufficient(self):
        need, why = campaign._insufficient(('lichess_cloud', 42, 8661914, False, 5))
        self.assertTrue(need)
        self.assertIn('WDL', why)


class TierTests(unittest.TestCase):
    def rows(self, count=100):
        out = []
        for index in range(count):
            reasons = {'high_reach': 0.03}
            if index < 40:
                reasons['decision_priority'] = 0.004
            if index < 20:
                reasons['mature_template'] = 0.02
            if index < 10:
                reasons['behavioural_disagreement'] = 0.4
            out.append({'position_key': bytes([index % 251, index % 7]), 'reasons': reasons,
                        'reason_count': len(reasons),
                        'reach': 0.03 if index < 30 else 0.001,
                        'score': float(count - index)})
        return out

    def test_everything_starts_at_tier_a(self):
        rows = self.rows()
        campaign._assign_tiers(rows)
        self.assertEqual(len(rows), sum(1 for row in rows if row['tier'] in campaign.NODE_TIERS))

    def test_ceiling_tier_is_small_and_requires_multi_signal_evidence(self):
        rows = self.rows()
        campaign._assign_tiers(rows)
        ceiling = [row for row in rows if row['tier'] == 'C']
        self.assertLessEqual(len(ceiling), 25)
        for row in ceiling:
            self.assertGreaterEqual(row['reason_count'], 3)
            self.assertEqual(row['promotion_watch'], 1)

    def test_deep_tier_is_a_minority(self):
        rows = self.rows()
        campaign._assign_tiers(rows)
        self.assertLess(len([r for r in rows if r['tier'] == 'B']), len(rows) * 0.2)

    def test_plan_budget_totals_match_tier_counts(self):
        rows = self.rows()
        campaign._assign_tiers(rows)
        plan, totals = campaign.plan_budget(rows, nps=505_000.0)
        self.assertEqual(plan['A']['positions'] + plan['B']['positions'] + plan['C']['positions'],
                         len(rows))
        self.assertAlmostEqual(totals['total_nodes'],
                               sum(entry['total_nodes'] for entry in plan.values()))
        self.assertAlmostEqual(totals['engine_hours'],
                               sum(entry['seconds'] for entry in plan.values()) / 3600.0)


class TemplateSampleTests(unittest.TestCase):
    def fake_db(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        db.executescript('''
            CREATE TABLE structure_maturity (structure_id TEXT, source TEXT, boards INTEGER,
                entry_mass REAL, development_level REAL, dwell_mean REAL, retention_3 REAL,
                ply_mean REAL, minors_white REAL, minors_black REAL, castled_white REAL,
                castled_black REAL, centre_left_white REAL, centre_left_black REAL,
                reconvergence REAL, effective_successors REAL, maturity_index REAL);
            CREATE TABLE position (position_key BLOB, structure_id TEXT, role TEXT, fen TEXT);
            CREATE TABLE position_flow (position_key BLOB, source TEXT, reach_flow REAL,
                                        enter_mass REAL);
        ''')
        db.execute("INSERT INTO structure_maturity VALUES('S1','local2200',20,0.10,0.5,3.0,0.4,"
                   "10.0,0.5,0.5,0.3,0.2,1.0,1.0,2.0,2.0,0.1)")
        for index in range(12):
            key = bytes([index, index * 3])
            db.execute('INSERT INTO position VALUES(?,?,?,?)', (key, 'S1', 'white_to_move', 'fen'))
            db.execute('INSERT INTO position_flow VALUES(?,?,?,?)',
                       (key, 'local2200', 0.01 * (index + 1), 0.01 * (index + 1)))
        db.commit()
        return db

    def test_sampling_is_deterministic_and_weighted(self):
        db = self.fake_db()
        first = campaign.template_samples(db, 'local2200', per_template=6, min_boards=4, seed=11)
        second = campaign.template_samples(db, 'local2200', per_template=6, min_boards=4, seed=11)
        different = campaign.template_samples(db, 'local2200', per_template=6, min_boards=4, seed=99)
        self.assertEqual([k for k, _s, _w in first['S1']], [k for k, _s, _w in second['S1']])
        self.assertEqual(len(first['S1']), len(set(k for k, _s, _w in first['S1'])))
        self.assertTrue(all(0.0 <= stratum <= 0.9 for _k, stratum, _w in first['S1']))
        self.assertNotEqual([k for k, _s, _w in first['S1']],
                            [k for k, _s, _w in different['S1']])

    def test_every_sampled_board_has_positive_weight(self):
        db = self.fake_db()
        samples = campaign.template_samples(db, 'local2200', per_template=6, min_boards=4, seed=3)
        self.assertTrue(all(weight >= 0 for _k, _s, weight in samples['S1']))


class AnswerValueTests(unittest.TestCase):
    def scores(self):
        return {'m1': {'ep_wp': 0.60}, 'm2': {'ep_wp': 0.45}, 'm3': {'ep_wp': 0.30}}

    def test_taught_answers_reduce_loss(self):
        # ordinary play splits across m2 and m3; the taught answer is m1
        # both losses are measured against the same external baseline (0.60)
        result = campaign.value_of_answers(None, self.scores(), {'m1': 1.0}, {'m2': 0.5, 'm3': 0.5},
                                           0.60)
        self.assertAlmostEqual(result['loss_pop'], 0.5 * 0.15 + 0.5 * 0.30)
        self.assertAlmostEqual(result['loss_taught'], 0.0)
        self.assertAlmostEqual(result['gain'], result['loss_pop'])

    def test_unrelated_answers_gain_nothing(self):
        result = campaign.value_of_answers(None, self.scores(), {'m3': 1.0}, {'m3': 1.0}, 0.60)
        self.assertAlmostEqual(result['gain'], 0.0)

    def test_missing_evaluations_are_reported_not_assumed(self):
        result = campaign.value_of_answers(None, {'m1': {'ep_wp': 0.6}}, {'m9': 1.0}, {'m9': 1.0},
                                           0.6)
        self.assertIsNone(result['gain'])


if __name__ == '__main__':
    unittest.main()
