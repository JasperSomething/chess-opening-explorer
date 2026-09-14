import sys
import unittest
from pathlib import Path

import chess

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from study import attractors, family  # noqa: E402


def bb(*squares):
    mask = 0
    for square in squares:
        mask |= chess.BB_SQUARES[chess.parse_square(square)]
    return mask


def position(pieces=None, reach=1.0, enter=0.0, ply=4, role='white_to_move'):
    base = {name: 0 for name in family.PIECES}
    for name, squares in (pieces or {}).items():
        base[name] = bb(*squares)
    return {'pieces': base, 'reach': reach, 'enter': enter, 'ply': ply, 'role': role,
            'fen': 'x', 'dev_status': 'undeveloped', 'white_castled': 'available_both',
            'black_castled': 'available_both', 'paths': 1.0, 'parents': 1,
            'count_ratio': None, 'flags': ''}


def synthetic_family():
    positions = {
        'a': position({'wp': ['d2'], 'wr': ['a1', 'h1'], 'wn': ['g1']}, reach=1.0, enter=1.0),
        'b': position({'wp': ['d2'], 'wr': ['a1', 'h1'], 'wn': ['c3']}, reach=0.6, enter=0.0),
        'c': position({'wp': ['d2'], 'wr': ['a1', 'h1'], 'wn': ['f3']}, reach=0.2, enter=0.0),
    }
    moves = {'a': [('b', 'g1c3', 60), ('c', 'g1f3', 20), ('X', 'd2d4', 20)],
             'b': [('X', 'f1c4', 60)],
             'c': [('X', 'f1c4', 20)]}
    return {'structure_id': 'S', 'positions': positions, 'moves': moves, 'sans': {},
            'source': 'test', 'reach_sum': 1.8}


class OccupancyTests(unittest.TestCase):
    def test_presence_vs_formation_weighting(self):
        fam = synthetic_family()
        presence, _ = family.piece_occupancy(fam, 'reach_flow')
        table = {sq: p for sq, p, _m in family.occupancy_table(presence['wn'])}
        # presence: g1 reach 1.0, c3 reach 0.6, f3 reach 0.2 -> 1.0/1.8, 0.6/1.8, 0.2/1.8
        self.assertAlmostEqual(table['g1'], 1.0 / 1.8)
        self.assertAlmostEqual(table['c3'], 0.6 / 1.8)
        self.assertAlmostEqual(table['f3'], 0.2 / 1.8)
        formation, _ = family.piece_occupancy(fam, 'enter_mass')
        table = {sq: p for sq, p, _m in family.occupancy_table(formation['wn'])}
        self.assertAlmostEqual(table['g1'], 1.0)          # only board a carries entering mass
        self.assertNotIn('c3', table)

    def test_pawns_are_excluded_and_home_multiplicity_is_not_flexibility(self):
        fam = synthetic_family()
        slots = family.flexible_slots(fam)
        labels = {family.PIECE_NAMES[s['piece']] for s in slots}
        self.assertNotIn('White pawn', labels)      # pawn squares are the family definition
        self.assertNotIn('White rook', labels)      # a1/h1 is two rooks at home, not a choice
        self.assertIn('White knight', labels)       # g1 vs c3 is a real split
        knight = [s for s in slots if s['piece'] == 'wn'][0]
        # g1 0.556, c3 0.333, f3 0.111 of board-visits: all above the 10% floor
        self.assertEqual([sq for sq, _p in knight['squares']], ['g1', 'c3', 'f3'])


class FeatureTests(unittest.TestCase):
    def test_feature_holds_kinds(self):
        meta = position({'wn': ['c3'], 'wq': ['d1'], 'wr': ['a1', 'h1']})
        self.assertTrue(family.feature_holds(meta, {'kind': 'piece_square', 'piece': 'wn',
                                                    'squares': ['c3']}))
        self.assertFalse(family.feature_holds(meta, {'kind': 'piece_square', 'piece': 'wn',
                                                     'squares': ['f3']}))
        self.assertTrue(family.feature_holds(meta, {'kind': 'piece_square_union', 'piece': 'wn',
                                                    'squares': ['g1', 'c3']}))
        self.assertTrue(family.feature_holds(meta, {'kind': 'state', 'piece': 'white_castled',
                                                    'squares': ['available_both']}))

    def test_feature_stats_board_scopes(self):
        fam = synthetic_family()
        feature = {'kind': 'piece_square', 'piece': 'wn', 'squares': ['g1']}
        weighted = family.feature_stats(fam, feature, top=0, boards='weighted')
        self.assertEqual(weighted['holds_boards'], 1)     # only board a carries entering mass
        self.assertEqual(weighted['fails_boards'], 0)
        every = family.feature_stats(fam, feature, top=0, boards='all')
        self.assertEqual(every['holds_boards'], 1)
        self.assertEqual(every['fails_boards'], 2)        # b and c do not have the knight on g1
        self.assertAlmostEqual(every['holds_share'], 1.0 / 1.8)


class ExitTimeTests(unittest.TestCase):
    def test_transit_structure_exits_immediately(self):
        members = {'T1', 'T2'}
        moves = {'T1': [('T2', 'x', 10), ('X', 'y', 90)], 'T2': [('X', 'z', 100)]}
        stats = attractors.exit_time_stats(members, moves, {'T1': 1.0}, max_steps=6)
        self.assertAlmostEqual(stats['exit_first_ply'], 0.9)
        self.assertAlmostEqual(stats['mean_dwell'], 1.1)          # 0.9 at 1 ply, 0.1 at 2 plies
        self.assertEqual(stats['median_dwell'], 1)
        self.assertAlmostEqual(stats['retention_3'], 0.0)

    def test_persistent_structure_retains_mass(self):
        members = {'S1', 'S2', 'S3', 'S4', 'S5'}
        moves = {f'S{i}': [(f'S{i + 1}', 'x', 90), ('X', 'y', 10)] for i in range(1, 5)}
        moves['S5'] = [('X', 'y', 100)]
        stats = attractors.exit_time_stats(members, moves, {'S1': 1.0}, max_steps=8)
        self.assertAlmostEqual(stats['exit_first_ply'], 0.1)
        # exits at 1,2,3,4 plies carry 0.1 each and the rest leaves at ply 5
        self.assertEqual(stats['median_dwell'], 5)
        # after three plies: 1 - (0.1 + 0.09 + 0.081)
        self.assertAlmostEqual(stats['retention_3'], 0.729, places=6)

    def test_entry_mass_is_counted_once(self):
        members = {'A'}
        moves = {'A': [('X', 'z', 100)]}
        stats = attractors.exit_time_stats(members, moves, {'A': 0.25}, max_steps=3)
        self.assertAlmostEqual(stats['entry_mass'], 0.25)
        self.assertAlmostEqual(stats['mean_dwell'], 1.0)
        self.assertEqual(stats['median_dwell'], 1)

    def test_empty_mass_is_reported_not_crashed(self):
        stats = attractors.exit_time_stats({'A'}, {'A': []}, {'A': 0.0})
        self.assertEqual(stats['entry_mass'], 0.0)
        self.assertIsNone(stats['mean_dwell'])


class ClassificationTests(unittest.TestCase):
    def test_rules(self):
        transit = {'exit_share_first_ply': 0.68, 'retention_3': 0.05}
        self.assertEqual(family.classify(1.5, 1.0, transit, 28), 'transit')
        strategic = {'exit_share_first_ply': 0.09, 'retention_3': 0.16}
        self.assertEqual(family.classify(3.13, 3.0, strategic, 172), 'strategic_attractor')
        # high retention but no convergence: not a template yet
        narrow = {'exit_share_first_ply': 0.09, 'retention_3': 0.50}
        self.assertEqual(family.classify(4.0, 4.0, narrow, 2), 'intermediate')
        # long dwell but nothing retained past three plies
        shallow = {'exit_share_first_ply': 0.30, 'retention_3': 0.01}
        self.assertEqual(family.classify(2.5, 2.0, shallow, 50), 'intermediate')


if __name__ == '__main__':
    unittest.main()
