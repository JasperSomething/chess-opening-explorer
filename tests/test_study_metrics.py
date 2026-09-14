import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from study import metrics  # noqa: E402


class ExpectedPointsTests(unittest.TestCase):
    def test_cp_curve(self):
        self.assertAlmostEqual(metrics.ep_from_cp(0), 0.5)
        self.assertAlmostEqual(metrics.ep_from_cp(400), 10 / 11)
        self.assertLess(metrics.ep_from_cp(-100), 0.5)
        for cp in (-2000, -300, 0, 300, 2000):
            self.assertTrue(0.0 <= metrics.ep_from_cp(cp) <= 1.0)

    def test_wdl_ep(self):
        self.assertAlmostEqual(metrics.ep_from_wdl(0, 0, 10), 0.0)
        self.assertAlmostEqual(metrics.ep_from_wdl(10, 0, 0), 1.0)
        self.assertAlmostEqual(metrics.ep_from_wdl(0, 10, 0), 0.5)
        self.assertIsNone(metrics.ep_from_wdl(0, 0, 0))

    def test_prefers_wdl_then_mate_then_cp(self):
        self.assertAlmostEqual(metrics.ep_from_score(cp=0, wdl=(10, 0, 0)), 1.0)
        self.assertAlmostEqual(metrics.ep_from_score(cp=-500, mate=3), 1.0)
        self.assertAlmostEqual(metrics.ep_from_score(mate=-2), 0.0)
        self.assertAlmostEqual(metrics.ep_from_score(cp=0), 0.5)
        self.assertIsNone(metrics.ep_from_score())


class PerspectiveTests(unittest.TestCase):
    """Eval sign handling: the engine scores the side to move, we need the mover's view."""

    def test_flip_ep_is_involutive(self):
        for ep in (0.0, 0.25, 0.5, 1.0):
            self.assertAlmostEqual(metrics.flip_ep(metrics.flip_ep(ep)), ep)

    def test_child_wdl_to_parent_mover(self):
        # Child (opponent to move) is lost for the opponent: parent mover gets 1.0.
        self.assertAlmostEqual(metrics.flip_wdl((0, 0, 1000)), 1.0)
        # Child is lost for the mover: parent mover gets 0.0.
        self.assertAlmostEqual(metrics.flip_wdl((1000, 0, 0)), 0.0)
        self.assertAlmostEqual(metrics.flip_wdl((0, 1000, 0)), 0.5)
        # Consistency with explicit negation of a child POV EP.
        for wdl in ((300, 400, 300), (100, 800, 100), (900, 50, 50)):
            self.assertAlmostEqual(
                metrics.flip_wdl(wdl),
                1.0 - metrics.ep_from_wdl(*wdl), places=12)

    def test_mate_sign_negation(self):
        # Mate for the side to move in the child == mate against the mover.
        self.assertAlmostEqual(metrics.flip_ep(metrics.ep_from_mate(2)), 0.0)
        self.assertAlmostEqual(metrics.flip_ep(metrics.ep_from_mate(-2)), 1.0)


class DistributionTests(unittest.TestCase):
    def test_normalise_and_renormalise(self):
        dist, total = metrics.normalise({'a': 3, 'b': 1})
        self.assertEqual(total, 4)
        self.assertAlmostEqual(dist['a'], 0.75)
        renorm, mass = metrics.renormalise({'a': 0.5, 'b': 0.25})
        self.assertAlmostEqual(mass, 0.75)
        self.assertAlmostEqual(renorm['a'], 2 / 3)

    def test_filter_moves_keeps_share_or_games(self):
        dist = {'a': 0.60, 'b': 0.30, 'c': 0.006, 'd': 0.0001}
        counts = {'a': 600, 'b': 300, 'c': 6, 'd': 1}
        kept, dropped = metrics.filter_moves(dist, counts=counts)
        self.assertEqual(set(kept), {'a', 'b', 'c'})   # c kept by share, not by games
        self.assertAlmostEqual(dropped, 0.0001)

        # A move can also be kept on games alone even when its share is tiny.
        kept, dropped = metrics.filter_moves({'x': 0.0001}, counts={'x': 5})
        self.assertEqual(set(kept), {'x'})
        self.assertAlmostEqual(dropped, 0.0)

        # Rare in both senses -> dropped and reported as uncovered mass.
        kept, dropped = metrics.filter_moves({'y': 0.001}, counts={'y': 1})
        self.assertEqual(kept, {})
        self.assertAlmostEqual(dropped, 0.001)


class RegretTests(unittest.TestCase):
    def test_expected_regret_matches_hand_calculation(self):
        dist = {'a': 0.5, 'b': 0.5}
        evals = {'a': {'ep_wp': 1.0}, 'b': {'ep_wp': 0.5}}
        r = metrics.expected_regret(dist, evals, method='wp')
        # 0.5 * max(0, 1.0 - 1.0) + 0.5 * max(0, 1.0 - 0.5) = 0.25
        self.assertAlmostEqual(r.regret, 0.25)
        self.assertEqual(r.best_uci, 'a')
        self.assertAlmostEqual(r.mass_covered, 1.0)
        self.assertEqual(r.n_moves, 2)

    def test_regret_is_zero_when_only_best_move_is_played(self):
        dist = {'a': 1.0, 'b': 0.0}
        evals = {'a': {'ep_wp': 0.7}, 'b': {'ep_wp': 0.2}}
        self.assertAlmostEqual(metrics.expected_regret(dist, evals, method='wp').regret, 0.0)

    def test_regret_ignores_moves_without_evals_and_reports_coverage(self):
        dist = {'a': 0.5, 'b': 0.3, 'c': 0.2}
        evals = {'a': {'ep_wp': 0.9}, 'b': {'ep_wp': 0.4}}
        r = metrics.expected_regret(dist, evals, method='wp')
        self.assertAlmostEqual(r.mass_covered, 0.8)
        # renormalised over the evaluated mass: 0.5/0.8 * 0 + 0.3/0.8 * 0.5
        self.assertAlmostEqual(r.regret, 0.1875)

    def test_method_selection_uses_the_right_field(self):
        evals = {'a': {'ep_wp': 1.0, 'ep_cp': 0.9}, 'b': {'ep_wp': 0.5, 'ep_cp': 0.9}}
        self.assertAlmostEqual(metrics.expected_regret({'a': .5, 'b': .5}, evals, 'wp').regret, 0.25)
        self.assertAlmostEqual(metrics.expected_regret({'a': .5, 'b': .5}, evals, 'cp').regret, 0.0)

    def test_no_evaluable_moves_returns_none(self):
        r = metrics.expected_regret({'a': 1.0}, {}, method='wp')
        self.assertIsNone(r.regret)
        self.assertEqual(r.mass_covered, 0.0)


class GapAndPriorityTests(unittest.TestCase):
    def test_knowledge_gap_sign(self):
        self.assertAlmostEqual(metrics.knowledge_gap(0.25, 0.05), 0.2)
        self.assertAlmostEqual(metrics.knowledge_gap(0.05, 0.25), -0.2)
        self.assertIsNone(metrics.knowledge_gap(None, 0.1))

    def test_study_priority_is_product_and_none_safe(self):
        self.assertAlmostEqual(metrics.study_priority(0.1, 0.2), 0.02)
        self.assertIsNone(metrics.study_priority(None, 0.2))
        self.assertIsNone(metrics.study_priority(0.1, None))

    def test_js_divergence_bounds_and_symmetry(self):
        p = {'a': 0.5, 'b': 0.5}
        self.assertAlmostEqual(metrics.js_divergence(p, p)[0], 0.0)
        js, overlap = metrics.js_divergence({'a': 1.0}, {'b': 1.0})
        self.assertAlmostEqual(js, 1.0)
        self.assertAlmostEqual(overlap, 0.0)
        js_ab, _ = metrics.js_divergence({'a': 0.9, 'b': 0.1}, {'a': 0.2, 'b': 0.8})
        js_ba, _ = metrics.js_divergence({'a': 0.2, 'b': 0.8}, {'a': 0.9, 'b': 0.1})
        self.assertAlmostEqual(js_ab, js_ba, places=12)
        self.assertTrue(0.0 <= js_ab <= 1.0)
        self.assertIsNone(metrics.js_divergence({}, p)[0])

    def test_divergent_but_equivalent_moves_yield_no_priority(self):
        """Identical evals with very different distributions must not score as study value."""
        dist_lichess = {'a': 0.9, 'b': 0.1}
        dist_expert = {'a': 0.2, 'b': 0.8}
        evals = {'a': {'ep_wp': 0.5}, 'b': {'ep_wp': 0.5}}
        r_l = metrics.expected_regret(dist_lichess, evals, 'wp')
        r_e = metrics.expected_regret(dist_expert, evals, 'wp')
        gap = metrics.knowledge_gap(r_l.regret, r_e.regret)
        js, _ = metrics.js_divergence(dist_lichess, dist_expert)
        self.assertAlmostEqual(r_l.regret, 0.0)
        self.assertAlmostEqual(r_e.regret, 0.0)
        self.assertAlmostEqual(gap, 0.0)
        self.assertGreater(js, 0.3)  # divergence is material, study value is zero
        self.assertAlmostEqual(metrics.study_priority(0.5, gap), 0.0)


class DeviationTests(unittest.TestCase):
    def test_classification_boundaries(self):
        self.assertEqual(metrics.classify_deviation(0.0), 'playable')
        self.assertEqual(metrics.classify_deviation(0.02), 'playable')
        self.assertEqual(metrics.classify_deviation(0.0201), 'inaccurate')
        self.assertEqual(metrics.classify_deviation(0.10), 'inaccurate')
        self.assertEqual(metrics.classify_deviation(0.1001), 'punishable')
        self.assertEqual(metrics.classify_deviation(None), 'unknown')

    def test_deviation_priority(self):
        self.assertAlmostEqual(metrics.deviation_priority(0.3, 0.05), 0.015)
        self.assertAlmostEqual(metrics.deviation_priority(0.3, -0.5), 0.0)
        self.assertIsNone(metrics.deviation_priority(None, 0.1))

    def test_wilson_interval_safeguards(self):
        self.assertEqual(metrics.wilson_interval(0, 0), (0.0, 1.0))
        low, high = metrics.wilson_interval(1, 10)
        self.assertLess(low, 0.1)
        self.assertGreater(high, 0.1)
        low, high = metrics.wilson_interval(1000, 1000)
        self.assertGreater(low, 0.99)
        self.assertLessEqual(high, 1.0)
        # A single game cannot distinguish 0% from 60%.
        low, high = metrics.wilson_interval(1, 1)
        self.assertGreater(high - low, 0.5)


if __name__ == '__main__':
    unittest.main()
