import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from study import db as studydb, template_value  # noqa: E402


class DistributionTests(unittest.TestCase):
    """The distribution of board-level gains is the answer to 'broadly useful or carried
    by a handful of boards?', so its arithmetic has to be exactly right."""

    def _gains(self, pairs):
        return [{'gain': gain, 'reach': reach} for gain, reach in pairs]

    def test_the_distribution_reports_the_shape_not_only_the_mean(self):
        gains = self._gains([(g, 0.1) for g in (-0.01, 0.0, 0.01, 0.02, 0.05, 0.09)])
        stats = template_value.distribution(gains)
        self.assertEqual(stats['boards'], 6)
        self.assertAlmostEqual(stats['min'], -0.01)
        self.assertAlmostEqual(stats['max'], 0.09)
        self.assertAlmostEqual(stats['median'], 0.015)
        self.assertAlmostEqual(stats['mean'], (-0.01 + 0 + 0.01 + 0.02 + 0.05 + 0.09) / 6)
        self.assertAlmostEqual(stats['share_positive'], 4 / 6)

    def test_a_single_dominant_board_is_visible_in_the_concentration(self):
        # one board carries almost all the gain: the template is not broadly useful
        gains = self._gains([(0.001, 0.1)] * 9 + [(1.0, 0.1)])
        stats = template_value.distribution(gains)
        self.assertGreater(stats['top_board_share_of_positive_total'], 0.98)
        self.assertGreater(stats['top3_share_of_positive_total'], 0.98)

    def test_broadly_useful_gains_do_not_concentrate(self):
        # equal gains on every board: no board dominates, so no board reaches 1/n of the gains
        gains = self._gains([(0.02, 0.05)] * 10)
        stats = template_value.distribution(gains)
        self.assertAlmostEqual(stats['top_board_share_of_positive_total'], 0.1)
        self.assertAlmostEqual(stats['top3_share_of_positive_total'], 0.3)
        self.assertAlmostEqual(stats['share_positive'], 1.0)

    def test_concentration_stays_bounded_when_losing_boards_offset_the_total(self):
        # the bug this guards: dividing by the net total let the top board exceed 100% of it,
        # because the losing boards shrink the denominator
        gains = self._gains([(0.10, 1.0), (-0.09, 1.0), (-0.09, 1.0)])
        stats = template_value.distribution(gains)
        self.assertLessEqual(stats['top_board_share_of_positive_total'], 1.0)
        self.assertAlmostEqual(stats['top_board_share_of_positive_total'], 1.0)
        self.assertAlmostEqual(stats['negative_total'], -0.18)

    def test_a_rule_whose_taught_moves_are_never_evaluated_is_unavailable(self):
        # no evaluated board may ever be reported as zero gain
        stats = template_value.distribution([])
        self.assertEqual(stats['boards'], 0)

    def test_reach_weighting_changes_the_total_but_not_the_unweighted_shape(self):
        plain = self._gains([(0.1, 1.0), (0.05, 1.0)])
        weighted = self._gains([(0.1, 1.0), (0.05, 1e-9)])
        self.assertAlmostEqual(template_value.distribution(plain)['median'],
                               template_value.distribution(weighted)['median'])
        self.assertGreater(template_value.distribution(plain)['weighted_total'],
                           template_value.distribution(weighted)['weighted_total'])

    def test_negative_gains_are_kept_not_clipped(self):
        # a taught answer set that does worse than ordinary play must not read as zero
        stats = template_value.distribution(self._gains([(-0.03, 0.2), (0.01, 0.2)]))
        self.assertAlmostEqual(stats['min'], -0.03)
        self.assertAlmostEqual(stats['weighted_total'], (-0.03 + 0.01) * 0.2)

    def test_an_empty_scope_reports_no_boards_rather_than_a_zero_gain(self):
        stats = template_value.distribution([])
        self.assertEqual(stats, {'boards': 0})

    def test_a_template_with_no_data_is_unavailable_not_zero_valued(self):
        path = Path(tempfile.mkdtemp()) / 'analysis.sqlite'
        db = studydb.connect(path, create=True)
        rule = {'rule_id': 'r1', 'structure_id': 'deadbeef', 'kind': 'prescriptive',
                'answers': ['e2e4'], 'condition': {'role': 'white_to_move'},
                'coverage': 1.0, 'n_exceptions': 0}
        report = template_value.per_burden_dimension(db, [rule])
        db.close()
        row = report['rules'][0]
        self.assertFalse(row['ev_available'])
        self.assertEqual(row['boards_evaluated'], 0)
        self.assertNotIn('ev_weighted_total', row)     # unavailable, never 0.0


if __name__ == '__main__':
    unittest.main()
