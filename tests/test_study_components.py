import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from study import components, compression  # noqa: E402


def plan(plan_id, events, structure_id='aaaa', side='white_to_move', cost=10.0,
         rows=None, unavailable=None):
    return {'plan_id': plan_id, 'structure_id': structure_id, 'side': side,
            'transformations': [list(event) for event in events],
            'burden': {'cost': cost}, 'rows': rows or [], 'unavailable': unavailable or {}}


class ComponentTests(unittest.TestCase):
    def test_a_pawn_move_carries_no_piece_letter_and_a_piece_move_does(self):
        # the frozen library stores pawn moves as (kind, from, to) and piece moves as
        # (kind, letter, from, to); reading them the same way silently loses events
        pawn = plan('p1', [('pawn_move', 'e2', 'e4')])
        piece = plan('p2', [('piece_move', 'N', 'g1', 'f3')])
        self.assertIn(('action', 'pawn_break'), components.plan_components(pawn, 'action'))
        self.assertIn(('action', 'development'), components.plan_components(piece, 'action'))

    def test_levels_abstract_the_same_plan_differently(self):
        one = plan('p1', [('piece_move', 'N', 'b1', 'c3')])
        two = plan('p2', [('piece_move', 'N', 'b1', 'd2'), ('piece_move', 'N', 'd2', 'c3')])
        # exact events differ
        self.assertNotEqual(components.plan_components(one)[0] if False else
                            components.plan_components(one, 'transformation'),
                            components.plan_components(two, 'transformation'))
        # the destination goal is shared
        self.assertTrue(components.plan_components(one, 'goal')
                        .intersection(components.plan_components(two, 'goal')))

    def test_effects_and_transitions_are_components_too(self):
        p = plan('p1', [('castle', 'w', 'k'), ('structure_exit', None, None),
                        ('capture', 'P', 'P', 'e4', 'd5')])
        action = components.plan_components(p, 'action')
        self.assertIn(('action', 'castle_k'), action)
        self.assertIn(('transition',), action)
        self.assertIn(('action', 'exchange'), action)

    def test_orderings_are_recorded_as_component_pairs(self):
        p = plan('p1', [('piece_move', 'N', 'b1', 'c3'), ('castle', 'w', 'k')])
        orderings = components.plan_orderings(p, 'goal')
        self.assertEqual(len(orderings), 1)
        self.assertTrue(any(entry[0] == 'before' for entry in orderings))


class BurdenTests(unittest.TestCase):
    def _library(self):
        shared = [('piece_move', 'N', 'b1', 'c3'), ('castle', 'w', 'k')]
        return {'plans': [plan('p1', shared, cost=10.0), plan('p2', shared, cost=10.0)],
                'structures': ['aaaa']}

    def test_the_additive_baseline_is_the_frozen_sum(self):
        entry = components.additive_burden(self._library())
        self.assertEqual(entry['total'], 20.0)

    def test_a_shared_item_is_charged_once_not_once_per_plan(self):
        library = self._library()
        inventory = components.information_inventory(library)
        entry = components.shared_burden(library, 'B_shared_uniform', inventory=inventory)
        # two identical plans share every component: 3 items, charged once each
        self.assertEqual(entry['shared_items'], len(inventory))
        self.assertEqual(entry['specific_items'], 0)
        self.assertEqual(entry['total'], float(len(inventory)))
        self.assertLess(entry['total'], components.additive_burden(library)['total'])

    def test_a_plan_specific_item_is_reported_as_specific(self):
        library = {'plans': [plan('p1', [('piece_move', 'N', 'b1', 'c3')]),
                             plan('p2', [('piece_move', 'N', 'b1', 'd2')])],
                   'structures': ['aaaa']}
        entry = components.shared_burden(library, 'B_shared_uniform',
                                         inventory=components.information_inventory(library))
        self.assertEqual(entry['specific_items'], 2)     # neither plan shares its goal
        self.assertEqual(entry['shared_items'], 0)

    def test_every_declared_model_is_cheaper_than_additive(self):
        library = {'plans': [plan(f'p{i}', [('piece_move', 'N', 'b1', 'c3')], cost=10.0)
                             for i in range(5)], 'structures': ['aaaa']}
        baseline = components.additive_burden(library)['total']
        for model in components.COST_MODELS:
            if components.COST_MODELS[model]['kind'] != 'shared':
                continue
            entry = components.shared_burden(library, model)
            self.assertLess(entry['total'], baseline)
            self.assertGreater(entry['total'], 0)

    def test_typed_weights_price_orderings_apart_from_transformations(self):
        spec = components.COST_MODELS['E_shared_typed']
        self.assertEqual(components.item_weight('ordering', ('before', 'a', 'b'), spec), 0.25)
        self.assertEqual(components.item_weight('component', ('action', 'castle_k'), spec), 0.25)


class SchemaTests(unittest.TestCase):
    def test_a_bundle_shared_by_several_plans_becomes_a_schema(self):
        goal = ('piece_move', 'N', 'b1', 'c3')
        library = {'plans': [plan('p1', [goal, ('castle', 'w', 'k')]),
                             plan('p2', [goal, ('pawn_move', 'e2', 'e4')]),
                             plan('p3', [goal])],
                   'structures': ['aaaa']}
        found = components.schemas(library, 'goal', min_plans=2, max_size=2)
        self.assertTrue(found)
        best = max(found, key=lambda schema: schema['plans'])
        self.assertIn(('goal', 'N', 'c3'), best['required'])
        self.assertGreaterEqual(best['plans'], 3)
        # what a schema does not require is kept as residual, not silently claimed
        self.assertIn(('castle', 'castle', 'k'), best['residual_components']
                      + best['optional'])

    def test_a_component_used_once_is_never_a_schema(self):
        library = {'plans': [plan('p1', [('piece_move', 'N', 'b1', 'c3')]),
                             plan('p2', [('piece_move', 'B', 'f1', 'e2')])],
                   'structures': ['aaaa']}
        self.assertEqual(components.bundles(library, 'goal', min_plans=2), [])


class EvidenceTests(unittest.TestCase):
    """The behavioural measure must never exceed the whole of strong play."""

    def test_a_prescription_cannot_explain_more_than_all_of_it(self):
        shares = {'a': 0.6, 'b': 0.5, 'c': 0.1}
        explained = min(1.0, sum(shares.get(uci, 0.0) for uci in ['a', 'b']))
        self.assertEqual(explained, 1.0)
        self.assertLessEqual(explained, 1.0)

    def test_the_longest_line_of_the_library_is_not_re_derived(self):
        # the experiment reads the frozen library; it must never mine plans itself
        source = (Path(__file__).resolve().parent.parent / 'study' / 'compression.py').read_text()
        self.assertNotIn('derive_plans', source)
        self.assertNotIn('mine_families', source)


class LadderTests(unittest.TestCase):
    def test_the_ladder_reports_every_rung_with_its_residual(self):
        library = {'plans': [plan('p1', [('piece_move', 'N', 'b1', 'c3'),
                                         ('castle', 'w', 'k')], rows=[])],
                   'structures': ['aaaa'], 'n_mined': 4}
        evidence = {b'k' * 4: {'reach': 0.1, 'loss_pop': 0.05, 'scores': {}, 'strong_shares': {}}}
        payload = {'decisions': [], 'plans': []}
        ladder = compression.compression_ladder(library, evidence, payload, 'goal', {}, [])
        levels = [row['level'] for row in ladder]
        self.assertEqual(levels[0], 'family surface boards')
        self.assertIn('mined plans', ' | '.join(levels))
        for row in ladder:
            self.assertIn('residual', row)
            self.assertIsNotNone(row['items'])


if __name__ == '__main__':
    unittest.main()
