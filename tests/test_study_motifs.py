import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from study import motifs  # noqa: E402

START = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -'
AFTER_E4 = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -'
AFTER_E4_D5 = 'rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -'
AFTER_EXD5 = 'rnbqkbnr/ppp1pppp/8/3P4/8/8/PPPP1PPP/RNBQKBNR b KQkq -'
CASTLE_READY = 'rnbqk2r/pppppppp/5n2/8/8/5N2/PPPPPPPP/RNBQKB1R w KQkq -'
AFTER_CASTLE = 'rnbqk2r/pppppppp/5n2/8/8/5N2/PPPPPPPP/RNBQ1RK1 b kq -'
CXD5_READY = 'rnbqkbnr/pp2pppp/8/3p4/2P5/8/PP1PPPPP/RNBQKBNR w KQkq -'

X = ('pawn_move', 'e2', 'e4')
Y = ('piece_move', 'N', 'g1', 'f3')
Z = ('castle', 'w', 'k')


def pos(ply, structure):
    return {'ply': ply, 'structure': structure, 'role': 'white_to_move',
            'enter': 0.0, 'reach': 0.0, 'paths': 1.0}


class SamplingTests(unittest.TestCase):
    def test_linear_graph_events_and_diagnostics(self):
        positions = {'P0': pos(0, 'S0'), 'P1': pos(1, 'S1'),
                     'P2': pos(2, 'S2'), 'P3': pos(3, 'S3')}
        moves = {'P0': [('P1', 'e2e4', 100)], 'P1': [('P2', 'd7d5', 100)],
                 'P2': [('P3', 'e4d5', 100)]}
        fens = {'P0': START, 'P1': AFTER_E4, 'P2': AFTER_E4_D5, 'P3': AFTER_EXD5}
        trajectories, diag = motifs.sample_trajectories(
            None, {'P0': 1.0}, positions, moves, n=50, window_plies=12,
            fen_of=lambda k: fens[k])
        expected = [('pawn_move', 'e2', 'e4'), ('structure_exit', None, None),
                    ('pawn_move', 'd7', 'd5'), ('structure_exit', None, None),
                    ('capture', 'P', 'P', 'e4', 'd5'), ('structure_exit', None, None)]
        self.assertEqual(len(trajectories), 50)
        for trajectory in trajectories:
            self.assertEqual(trajectory, expected)
        self.assertEqual(diag['samples'], 50)
        self.assertEqual(diag['window_plies'], 12)
        self.assertEqual(diag['seed'], 1337)
        self.assertAlmostEqual(diag['seeds_mass'], 1.0)
        self.assertEqual(diag['unique_events'], 4)

    def test_castling_is_one_atomic_event(self):
        positions = {'C0': pos(0, 'S'), 'C1': pos(1, 'S')}
        moves = {'C0': [('C1', 'e1g1', 100)]}
        fens = {'C0': CASTLE_READY, 'C1': AFTER_CASTLE}
        trajectories, diag = motifs.sample_trajectories(
            None, {'C0': 1.0}, positions, moves, n=5, fen_of=lambda k: fens[k])
        self.assertEqual(trajectories[0], [('castle', 'w', 'k')])
        self.assertEqual(diag['unique_events'], 1)

    def test_capture_and_opened_file(self):
        positions = {'F0': pos(0, 'S'), 'F1': pos(1, 'S')}
        moves = {'F0': [('F1', 'c4d5', 100)]}
        fens = {'F0': CXD5_READY, 'F1': AFTER_E4_D5}
        trajectories, _diag = motifs.sample_trajectories(
            None, {'F0': 1.0}, positions, moves, n=1, fen_of=lambda k: fens[k])
        self.assertEqual(trajectories[0], [('capture', 'P', 'P', 'c4', 'd5'),
                                           ('file_open', 'c', None)])

    def test_choice_follows_edge_mass(self):
        positions = {'R0': pos(0, 'S0'), 'R1': pos(1, 'S1'), 'R2': pos(1, 'S2')}
        moves = {'R0': [('R1', 'e2e4', 900), ('R2', 'd2d4', 100)]}
        fens = {'R0': START, 'R1': AFTER_E4,
                'R2': 'rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq -'}
        trajectories, _diag = motifs.sample_trajectories(
            None, {'R0': 1.0}, positions, moves, n=2000, window_plies=1,
            fen_of=lambda k: fens[k])
        e4 = sum(1 for t in trajectories if ('pawn_move', 'e2', 'e4') in t)
        d4 = sum(1 for t in trajectories if ('pawn_move', 'd2', 'd4') in t)
        self.assertAlmostEqual(e4 / 2000, 0.9, delta=0.05)
        self.assertAlmostEqual(d4 / 2000, 0.1, delta=0.05)


class MiningTests(unittest.TestCase):
    def test_planted_set_support_matches_hand_computed_share(self):
        trajectories = [[X, Y] for _ in range(60)] + [[X] for _ in range(40)]
        found = motifs.mine_motifs(trajectories, min_support=0.05, max_size=4)
        by_events = {frozenset(m['events']): m for m in found}
        self.assertAlmostEqual(by_events[frozenset((X,))]['support'], 1.0)
        self.assertAlmostEqual(by_events[frozenset((Y,))]['support'], 0.6)
        self.assertAlmostEqual(by_events[frozenset((X, Y))]['support'], 0.6)
        self.assertEqual(by_events[frozenset((X, Y))]['size'], 2)
        for key in ('events', 'size', 'support', 'conditional_frequency',
                    'order_flexibility', 'family_id', 'family_lift', 'sample_mass'):
            self.assertIn(key, by_events[frozenset((X, Y))])
        # supports are sorted descending
        supports = [m['support'] for m in found]
        self.assertEqual(supports, sorted(supports, reverse=True))

    def test_order_flexibility_is_the_violated_share(self):
        trajectories = [[X, Y, Z] for _ in range(70)] + [[Y, X, Z] for _ in range(30)]
        found = motifs.mine_motifs(trajectories, min_support=0.05, max_size=4)
        motif = next(m for m in found if frozenset(m['events']) == frozenset((X, Y, Z)))
        self.assertAlmostEqual(motif['support'], 1.0)
        self.assertAlmostEqual(motif['order_flexibility'], 0.30)
        self.assertEqual(motif['events'], [X, Y, Z])          # the majority order

    def test_sets_below_min_support_are_not_reported(self):
        trajectories = ([[X, Y] for _ in range(58)] + [[X] for _ in range(40)]
                        + [[X, Z] for _ in range(2)])
        found = motifs.mine_motifs(trajectories, min_support=0.05, max_size=4)
        events_in_found = {e for m in found for e in m['events']}
        self.assertNotIn(Z, events_in_found)
        self.assertFalse(any(frozenset((X, Z)) == frozenset(m['events']) for m in found))
        self.assertTrue(any(frozenset((X, Y)) == frozenset(m['events']) for m in found))

    def test_duplicate_events_collapse_and_events_are_hashable(self):
        trajectories = [[X, X, Y], [X, Y], [X, Y]]
        self.assertEqual(len(set(trajectories[0])), 2)
        found = motifs.mine_motifs(trajectories, min_support=0.05, max_size=4)
        pair = next(m for m in found if frozenset(m['events']) == frozenset((X, Y)))
        self.assertAlmostEqual(pair['support'], 1.0)
        self.assertEqual(pair['sample_mass'], 3.0)

    def test_min_support_threshold_is_respected_at_the_boundary(self):
        trajectories = [[X, Y] for _ in range(5)] + [[X] for _ in range(95)]
        found = motifs.mine_motifs(trajectories, min_support=0.05, max_size=4)
        self.assertTrue(any(frozenset((X, Y)) == frozenset(m['events']) for m in found))
        found_high = motifs.mine_motifs(trajectories, min_support=0.06, max_size=4)
        self.assertFalse(any(frozenset((X, Y)) == frozenset(m['events']) for m in found_high))


class SummaryTests(unittest.TestCase):
    def test_description_is_built_only_from_event_tuples(self):
        motif = {'events': [Y, Z], 'size': 2, 'support': 1.0, 'conditional_frequency': 1.0,
                 'order_flexibility': 0.0, 'family_id': 'F', 'family_lift': 1.5,
                 'sample_mass': 10.0}
        summary = motifs.motif_summary([motif], top=1)
        self.assertEqual(summary[0]['description'], 'piece N g1->f3 THEN castle w kingside')

    def test_event_formatting_covers_the_vocabulary(self):
        self.assertEqual(motifs.format_event(('pawn_move', 'e2', 'e4')), 'pawn e2->e4')
        self.assertEqual(motifs.format_event(('capture', 'N', 'N', 'f3', 'e5')),
                         'capture N f3->e5')
        self.assertEqual(motifs.format_event(('file_open', 'c', None)), 'file c opens')
        self.assertEqual(motifs.format_event(('structure_exit', None, None)), 'structure exit')


if __name__ == '__main__':
    unittest.main()
