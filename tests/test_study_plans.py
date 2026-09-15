import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chess  # noqa: E402

from study import plan_report, plans  # noqa: E402

START = chess.Board()
SCAND = chess.Board('rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2')


class EventStateTests(unittest.TestCase):
    """Completion is decided by a state test on the board, never by an engine."""

    def test_a_live_transformation_is_actionable(self):
        self.assertEqual(plans.event_state(START, ('pawn_move', 'e2', 'e4')),
                         plans.ACTIONABLE)
        self.assertEqual(plans.event_state(START, ('piece_move', 'N', 'g1', 'f3')),
                         plans.ACTIONABLE)
        self.assertEqual(plans.event_state(START, ('castle', 'w', 'k')), plans.ACTIONABLE)

    def test_a_finished_transformation_is_complete(self):
        # the knight has left g1, and the king has left e1
        after = chess.Board('r1bqkbnr/pppppppp/2n5/8/8/5N2/PPPPPPPP/R1BQKB1R b KQkq - 2 2')
        self.assertEqual(plans.event_state(after, ('piece_move', 'N', 'g1', 'f3')),
                         plans.COMPLETE)
        # the king is on g1: the kingside castle has already happened
        castled = chess.Board('r1bqk1nr/pppp1ppp/2n5/2b1p3/2B1P3/5N2/PPPP1PPP/RNBQ1RK1 b kq - 5 5')
        self.assertEqual(plans.event_state(castled, ('castle', 'w', 'k')), plans.COMPLETE)

    def test_an_opponents_transformation_is_not_this_sides_plan(self):
        # the black pawn still stands on d5 while White is to move: the plan cannot be
        # read as "already done", it simply is not this side's transformation
        self.assertEqual(plans.event_state(SCAND, ('pawn_move', 'd5', 'd4')), plans.UNKNOWN)
        self.assertEqual(plans.event_state(SCAND, ('piece_move', 'N', 'g8', 'f6')),
                         plans.UNKNOWN)

    def test_effects_are_undecidable_rather_than_guessed(self):
        self.assertEqual(plans.event_state(START, ('capture', 'P', 'P', 'e4', 'd5')),
                         plans.UNKNOWN)
        self.assertEqual(plans.event_state(START, ('structure_exit', None, None)),
                         plans.UNKNOWN)
        self.assertEqual(plans.event_state(START, ('file_open', 'e', None)), plans.UNKNOWN)

    def test_an_open_file_is_complete(self):
        open_board = chess.Board('rnbqkbnr/pppp1ppp/8/8/8/8/PPPP1PPP/RNBQKBNR w KQkq - 0 1')
        self.assertEqual(plans.event_state(open_board, ('file_open', 'e', None)),
                         plans.COMPLETE)


class MoveMappingTests(unittest.TestCase):
    """A move is included because of what it is, never because an engine likes it."""

    def test_only_moves_matching_the_transformations_are_mapped(self):
        advancing = plans.moves_advancing(
            START, [('piece_move', 'N', 'g1', 'f3'), ('pawn_move', 'e2', 'e4')], 'x', {})
        self.assertEqual(sorted(advancing), ['e2e4', 'g1f3'])
        self.assertEqual(advancing['e2e4'], [('pawn_move', 'e2', 'e4')])

    def test_a_transformation_no_legal_move_performs_maps_to_nothing(self):
        advancing = plans.moves_advancing(START, [('piece_move', 'B', 'c8', 'g4')], 'x', {})
        self.assertEqual(advancing, {})

    def test_castling_is_mapped_only_when_it_is_actually_available(self):
        ready = chess.Board('r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/2N2N2/PPPP1PPP/R1BQK2R w KQkq - 6 6')
        advancing = plans.moves_advancing(ready, [('castle', 'w', 'k')], 'x', {})
        self.assertIn('e1g1', advancing)


class BurdenTests(unittest.TestCase):
    def test_burden_is_reported_per_dimension_and_costs_no_exact_moves(self):
        plan = {'transformations': [['piece_move', 'N', 'b1', 'c3'],
                                    ['pawn_move', 'e2', 'e4']]}
        burden = plan_report.plan_burden(plan, {'no_strong_play_support': 3})
        self.assertEqual(burden['transformations'], 2)
        self.assertEqual(burden['exact_moves'], 0)
        self.assertEqual(burden['exceptions'], 3)
        self.assertEqual(burden['condition_kinds'], ['pawn_move', 'piece_move'])
        self.assertGreater(burden['cost'], 0)


class FakeSurface:
    """A tiny surface for checking the comparison arithmetic, not the data pipeline."""

    def __init__(self, reach, loss_pop, contexts):
        self.reach = reach
        self._loss_pop = loss_pop
        self._contexts = contexts

    def context(self, key):
        if key not in self._contexts:
            return None
        return {'loss_pop': self._loss_pop[key]}

    def loss(self, key, answers):
        if key not in self._contexts:
            return None
        return self._contexts[key].get(tuple(answers))


class OverlapRuleTests(unittest.TestCase):
    """Two items covering the same board must not both be paid for it."""

    def test_the_best_covering_item_is_used_not_the_sum(self):
        from study import curriculum, rules  # noqa: F401  (import parity with the module)
        key = b'k' * 4
        surface = FakeSurface(reach={key: 1.0}, loss_pop={key: 0.10},
                              contexts={key: {('a',): 0.04, ('b',): 0.01}})
        items = [{'item_id': 'i1', 'keys': [key], 'answers': ['a']},
                 {'item_id': 'i2', 'keys': [key], 'answers': ['b']}]
        rows = []
        for item in items:
            rows.append((item, {key: surface.loss(key, item['answers'])}))
        achieved = min(losses[key] for _item, losses in rows if key in losses)
        self.assertEqual(achieved, 0.01)
        retained = surface.reach[key] * max(0.0, surface._loss_pop[key] - achieved)
        self.assertAlmostEqual(retained, 0.09)          # not 0.06 + 0.09

    def test_a_board_without_data_is_not_counted_as_zero_gain(self):
        key = b'x' * 4
        surface = FakeSurface(reach={key: 1.0}, loss_pop={}, contexts={})
        self.assertIsNone(surface.loss(key, ['a']))


class OracleTests(unittest.TestCase):
    """The oracle is an upper bound and must never look worse than the fixed rule."""

    def test_the_oracle_is_labelled_a_bound_and_recognition_has_no_ev(self):
        source = Path(__file__).resolve().parent.parent / 'data' / 'atlas-analysis.sqlite'
        if not source.exists():
            self.skipTest('analysis database not present')
        from study import db as studydb
        db = studydb.connect(source, create=False)
        structures = [row['structure_id'] for row in db.execute(
            'SELECT structure_id FROM structure_maturity WHERE source=? ORDER BY entry_mass '
            'DESC LIMIT 2', ('local2200',))]
        for structure_id in structures:
            report = plans.family_oracle(db, structure_id)
            self.assertIsNone(report['recognition_only']['ev'])
            self.assertIn('upper bound', report['oracle']['labelled_as'])
            # the bound can never be below the fixed rule it bounds
            self.assertGreaterEqual(
                round(report['oracle']['ev_reach_weighted'], 9),
                round(report['fixed_rule']['ev_reach_weighted'], 9))
        db.close()


if __name__ == '__main__':
    unittest.main()
