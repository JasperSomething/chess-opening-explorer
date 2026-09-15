import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from study import curriculum, splitting  # noqa: E402


class ComplexityTests(unittest.TestCase):
    def test_weighted_cost_and_compression_ratio(self):
        item = {'positions': 560, 'exact_moves': 1, 'boards': 1, 'flexible_slots': 2,
                'conditions': 3, 'n_exceptions': 4, 'move_orders': 6}
        proxies = curriculum.complexity(item)
        # 1 move + 2 slots + (3-1) conditions + 4 exceptions + 1 board = 10
        self.assertAlmostEqual(proxies['cost'], 10.0)
        self.assertAlmostEqual(proxies['compression_ratio'], 56.0)
        for key in ('exact_moves', 'boards', 'flexible_slots', 'conditions', 'exceptions',
                    'positions', 'move_orders'):
            self.assertIn(key, proxies)

    def test_a_single_fact_is_the_cheapest_item(self):
        cheap = curriculum.complexity({'positions': 1, 'exact_moves': 1, 'boards': 0,
                                       'flexible_slots': 0, 'conditions': 1, 'n_exceptions': 0})
        rich = curriculum.complexity({'positions': 560, 'exact_moves': 1, 'boards': 1,
                                      'flexible_slots': 2, 'conditions': 1, 'n_exceptions': 0})
        self.assertLess(cheap['cost'], rich['cost'])


class UnionCoverageTests(unittest.TestCase):
    class FakeDb:
        def __init__(self, entry):
            self.entry = entry

        def execute(self, _sql, _params=()):
            return [{'position_key': self.entry}]

    def graph(self):
        positions = {'E': {'ply': 0, 'structure': 'S0'}, 'A': {'ply': 1, 'structure': 'S1'},
                     'B': {'ply': 1, 'structure': 'S2'}, 'C': {'ply': 2, 'structure': 'S3'},
                     'D': {'ply': 3, 'structure': 'S4'}}
        moves = {'E': [('A', 'x', 50), ('B', 'y', 50)],
                 'A': [('C', 'x', 100)], 'B': [('C', 'y', 100)], 'C': [('D', 'x', 100)]}
        return positions, moves

    def coverage(self, keys):
        positions, moves = self.graph()
        return curriculum.position_union_coverage(None, keys, positions=positions, moves=moves,
                                                  entry='E')

    def test_a_game_is_counted_once_not_once_per_ply(self):
        # C is reached by every game, so covering only C covers everything: 1.0, not 2.0
        self.assertAlmostEqual(self.coverage(['C']), 1.0)
        self.assertAlmostEqual(self.coverage(['A']), 0.5)
        self.assertAlmostEqual(self.coverage(['A', 'B']), 1.0)
        self.assertAlmostEqual(self.coverage(['E']), 1.0)
        self.assertAlmostEqual(self.coverage(['D']), 1.0)

    def test_nothing_covered_is_zero(self):
        self.assertAlmostEqual(self.coverage([]), 0.0)


class FrontierTests(unittest.TestCase):
    def items(self):
        def make(item_id, keys, answers=('m1',), kind='decision'):
            item = {'item_id': item_id, 'type': kind, 'label': item_id, 'keys': keys,
                    'answers': list(answers), 'coverage': 0.5, 'circular': False}
            item['complexity'] = curriculum.complexity({'positions': len(keys), 'exact_moves': 1,
                                                        'boards': 0, 'flexible_slots': 0,
                                                        'conditions': 1, 'n_exceptions': 0})
            return item
        return [make('a', ['p1', 'p2']), make('b', ['p1']), make('c', ['p3'])]

    def values(self):
        return {
            'p1': {'reach': 0.4, 'loss_pop': 0.10, 'per_item': {'a': 0.02, 'b': 0.02}},
            'p2': {'reach': 0.1, 'loss_pop': 0.10, 'per_item': {'a': 0.02}},
            'p3': {'reach': 0.5, 'loss_pop': 0.10, 'per_item': {'c': 0.03}},
        }

    def test_overlap_is_a_best_item_not_a_sum(self):
        """Value is the reduction of the ordinary-play loss, so taught losses are
        compared against `loss_pop` and the best item wins the position."""
        rows, chosen, best = curriculum.frontier(self.items(), self.values(), k_max=10)
        by_id = {row['item_id']: row for row in rows}
        self.assertIn('a', by_id)
        # item b teaches the same answer as a at p1, so once a is in it earns nothing
        self.assertNotIn('b', by_id)
        # a: 0.4*(0.10-0.02) + 0.1*(0.10-0.02) = 0.040
        self.assertAlmostEqual(by_id['a']['marginal_value'], 0.040)
        # c: 0.5*(0.10-0.03) = 0.035
        self.assertAlmostEqual(by_id['c']['marginal_value'], 0.035)

    def test_an_item_without_answers_earns_nothing(self):
        """Recognition knowledge costs burden and must never earn value: a missing
        answer set is unknown (None), not a taught loss of zero (perfect play)."""
        items = self.items()
        recognition = {'item_id': 'r', 'type': 'orientation', 'label': 'r', 'keys': ['p1'],
                       'answers': [], 'coverage': 0.5}
        recognition['complexity'] = curriculum.complexity({'positions': 1, 'boards': 1,
                                                          'conditions': 1, 'exact_moves': 0})
        values = self.values()
        values['p1']['per_item']['r'] = None
        rows, chosen, _achieved = curriculum.frontier(items + [recognition], values, k_max=10)
        self.assertNotIn('r', {row['item_id'] for row in rows})

    def test_retained_value_is_monotone_and_capped_by_the_baseline(self):
        rows, chosen, best = curriculum.frontier(self.items(), self.values(), k_max=10)
        retained = [row['value_retained'] for row in rows]
        self.assertEqual(retained, sorted(retained))
        baseline = curriculum.baseline(self.values())
        self.assertLessEqual(retained[-1], baseline['lost_population'] + 1e-9)
        self.assertAlmostEqual(retained[-1], 0.075)     # a and c together

    def test_greedy_prefers_the_larger_loss_reduction(self):
        rows, _chosen, _best = curriculum.frontier(self.items(), self.values(), k_max=10)
        self.assertEqual(rows[0]['item_id'], 'a')       # 0.040 beats 0.035


class EntropyTests(unittest.TestCase):
    def test_entropy_of_a_fair_coin(self):
        self.assertAlmostEqual(splitting.entropy({'x': 1.0, 'y': 1.0}), 1.0)

    def test_mi_is_zero_when_the_feature_is_noise(self):
        rng = random.Random(0)
        samples = [(f'k{i}', 1.0, {'label': rng.choice(('a', 'b')), 'noise': rng.choice((0, 1))})
                   for i in range(400)]
        target = lambda key, meta: {'move1': 1.0 / 3, 'move2': 1.0 / 3, 'move3': 1.0 / 3}
        mi, _report = splitting.conditional_mi(samples, lambda meta: meta['noise'], target)
        self.assertLess(mi, 0.05)

    def test_mi_recovers_a_planted_dependency(self):
        samples = []
        for i in range(200):
            group = 'a' if i % 2 == 0 else 'b'
            samples.append((f'k{i}', 1.0, {'label': group}))
        target = lambda key, meta: {'left': 1.0} if meta['label'] == 'a' else {'right': 1.0}
        mi, _report = splitting.conditional_mi(samples, lambda meta: meta['label'], target,
                                               min_boards=1)
        # a feature that separates the target completely earns exactly H(Y) = 1 bit
        self.assertAlmostEqual(mi, 1.0)
        target_coin = lambda key, meta: {'flip': 0.5, 'flop': 0.5}
        mi2, _r = splitting.conditional_mi(samples, lambda meta: meta['label'], target_coin)
        self.assertAlmostEqual(mi2, 0.0)

    def test_mi_of_a_feature_that_splits_the_target_distribution(self):
        samples = []
        for i in range(400):
            group = 'a' if i % 2 == 0 else 'b'
            samples.append((f'k{i}', 1.0, {'label': group}))
        # group a always plays X, group b splits evenly between X and Y
        def target(key, meta):
            return {'X': 1.0} if meta['label'] == 'a' else {'X': 0.5, 'Y': 0.5}
        mi, report = splitting.conditional_mi(samples, lambda meta: meta['label'], target)
        # H(Y) = 1 bit (pooled: 0.75/0.25); conditional entropy = 0.5*1 = 0.5
        self.assertAlmostEqual(mi, 0.8113 - 0.5, places=3)
        self.assertIn('groups', report)

    def test_permutation_test_separates_signal_from_noise(self):
        """Targets are keyed by position, exactly as the real targets are."""
        rng = random.Random(7)
        signal, noise = [], []
        for i in range(300):
            key = f'k{i}'
            group = 'a' if i % 2 == 0 else 'b'
            signal.append((key, 1.0, {'feature': group}))
            noise.append((key, 1.0, {'feature': rng.choice(('a', 'b'))}))

        def target(key, _meta):
            return {'X': 1.0} if int(key[1:]) % 2 == 0 else {'Y': 1.0}

        mi_signal, _r = splitting.conditional_mi(signal, lambda m: m['feature'], target)
        p_signal = splitting.permutation_p(signal, lambda m: m['feature'], target, mi_signal,
                                           permutations=100)
        mi_noise, _r2 = splitting.conditional_mi(noise, lambda m: m['feature'], target)
        p_noise = splitting.permutation_p(noise, lambda m: m['feature'], target, mi_noise,
                                          permutations=100)
        self.assertGreater(mi_signal, 0.9)          # the feature explains the target
        self.assertLess(p_signal, 0.05)
        self.assertLess(mi_noise, 0.1)              # noise explains nothing
        self.assertGreater(p_noise, 0.20)

    def test_single_group_cannot_split(self):
        samples = [('k1', 1.0, {'label': 'a'}), ('k2', 1.0, {'label': 'a'})]
        mi, report = splitting.conditional_mi(samples, lambda meta: meta['label'],
                                              lambda key, meta: {'X': 1.0})
        self.assertEqual(mi, 0.0)
        self.assertEqual(report['groups'], {})

    def test_min_boards_filters_tiny_groups(self):
        samples = [(f'k{i}', 1.0, {'label': 'a' if i < 10 else 'b'}) for i in range(12)]

        def target(key, meta):
            return {'X': 1.0} if meta['label'] == 'a' else {'Y': 1.0}

        mi, report = splitting.conditional_mi(samples, lambda meta: meta['label'], target,
                                              min_boards=3)
        # group b (2 boards) is dropped, leaving one usable group: no split is possible
        self.assertEqual(mi, 0.0)
        self.assertEqual(report['groups'], {})
        self.assertIn('two usable groups', report['reason'])


if __name__ == '__main__':
    unittest.main()
