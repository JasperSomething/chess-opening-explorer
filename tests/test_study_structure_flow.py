import sys
import unittest
from collections import defaultdict
from pathlib import Path

import chess

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from study import maturity, structure_flow, templates  # noqa: E402

START = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -'
AFTER_E4 = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -'
AFTER_E4_NF6 = 'rnbqkb1r/pppppppp/5n2/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -'


def position(pieces, ply, structure, enter=0.0, reach=0.0):
    return {'ply': ply, 'structure': structure, 'role': 'white_to_move', 'enter': enter,
            'reach': reach, 'paths': 1.0}


class TransitionEdgeTests(unittest.TestCase):
    def test_mass_is_conserved_and_attributed_to_the_destination(self):
        positions = {
            'A': position({}, 0, 'S1', enter=1.0, reach=1.0),
            'B': position({}, 1, 'S2', enter=0.7, reach=0.7),
            'C': position({}, 1, 'S3', enter=0.3, reach=0.3),
        }
        moves = {'A': [('B', 'e2e4', 70), ('C', 'd2d4', 30)]}
        edges, summary = structure_flow.transition_edges(positions, moves)
        self.assertAlmostEqual(edges[('S1', 'S2', 'e2e4')], 0.7)
        self.assertAlmostEqual(edges[('S1', 'S3', 'd2d4')], 0.3)
        total_out = sum(mass for (a, _b, _u), mass in edges.items() if a == 'S1')
        self.assertAlmostEqual(total_out + summary['S1']['leak_mass'],
                               summary['S1']['entry_mass'])

    def test_moves_inside_the_same_family_do_not_create_edges(self):
        positions = {'A': position({}, 0, 'S1', enter=1.0),
                     'B': position({}, 1, 'S1'), 'C': position({}, 2, 'S2')}
        moves = {'A': [('B', 'g1f3', 100)], 'B': [('C', 'd2d4', 100)]}
        edges, _summary = structure_flow.transition_edges(positions, moves)
        self.assertEqual(set(edges), {('S1', 'S2', 'd2d4')})
        self.assertAlmostEqual(edges[('S1', 'S2', 'd2d4')], 1.0)

    def test_mass_leaving_the_domain_is_leakage_not_an_edge(self):
        positions = {'A': position({}, 0, 'S1', enter=1.0)}
        moves = {'A': [('OUTSIDE', 'e2e4', 100)]}
        edges, summary = structure_flow.transition_edges(positions, moves)
        self.assertEqual(edges, {})
        self.assertAlmostEqual(summary['S1']['leak_mass'], 1.0)


class CoverageTests(unittest.TestCase):
    def graph(self):
        # entry -> (a, b); a -> c; b -> c ; c -> d
        return {('entry', 'a', 'x'): 0.5, ('entry', 'b', 'y'): 0.5,
                ('a', 'c', 'x'): 0.5, ('b', 'c', 'y'): 0.5,
                ('c', 'd', 'x'): 1.0}

    def test_single_selection(self):
        coverage, _mass = structure_flow.coverage_of_set(self.graph(), 'entry', ['a'])
        self.assertAlmostEqual(coverage, 0.5)

    def test_overlapping_selections_only_earn_their_margin(self):
        """The property task E relies on: a family whose games already pass through a
        selected family adds exactly zero, however large its own mass is."""
        coverage_a, _ = structure_flow.coverage_of_set(self.graph(), 'entry', ['a'])
        coverage_c, _ = structure_flow.coverage_of_set(self.graph(), 'entry', ['c'])
        coverage_a_c, _ = structure_flow.coverage_of_set(self.graph(), 'entry', ['a', 'c'])
        coverage_all, _ = structure_flow.coverage_of_set(self.graph(), 'entry', ['a', 'b', 'c', 'd'])
        self.assertAlmostEqual(coverage_a, 0.5)              # half the games take a
        self.assertAlmostEqual(coverage_c, 1.0)             # every game eventually reaches c
        self.assertAlmostEqual(coverage_a_c, 1.0)
        self.assertAlmostEqual(coverage_a_c - coverage_a, 0.5)   # c earns its margin over a
        self.assertAlmostEqual(coverage_all, 1.0)
        # d is fully subsumed by c: adding it changes nothing
        coverage_without_d, _ = structure_flow.coverage_of_set(self.graph(), 'entry', ['a', 'b', 'c'])
        self.assertAlmostEqual(coverage_all - coverage_without_d, 0.0)

    def test_leakage_size_is_respected(self):
        graph = {('entry', 'a', 'x'): 0.5}
        coverage, _ = structure_flow.coverage_of_set(graph, 'entry', ['a'], leak={'entry': 0.5})
        # only half of the entry mass moves on an edge; the rest leaves the domain
        self.assertAlmostEqual(coverage, 0.5)


class MaturityTests(unittest.TestCase):
    def pieces(self, white_pawns=('a2', 'b2', 'c2', 'd2', 'f2', 'g2', 'h2'),
               black_pawns=('a7', 'b7', 'c7', 'e7', 'f7', 'g7', 'h7'),
               developed=(), black_developed=()):
        board = {name: 0 for name in ('wp', 'wn', 'wb', 'wr', 'wq', 'wk',
                                      'bp', 'bn', 'bb', 'br', 'bq', 'bk')}
        for square in white_pawns:
            board['wp'] |= chess.BB_SQUARES[chess.parse_square(square)]
        for square in black_pawns:
            board['bp'] |= chess.BB_SQUARES[chess.parse_square(square)]
        for name, square in developed:
            board[name] |= chess.BB_SQUARES[chess.parse_square(square)]
        for name, square in black_developed:
            board[name] |= chess.BB_SQUARES[chess.parse_square(square)]
        return board

    def test_components_of_an_undeveloped_board(self):
        components = maturity.position_components(self.pieces(), {'w_castled': 'available_both',
                                                                  'b_castled': 'available_both'})
        self.assertEqual(components['minors_white'], 0.0)
        self.assertEqual(components['castled_white'], 0.0)
        # White's e-pawn is gone (played exd5 and was captured), Black's d-pawn is gone.
        self.assertAlmostEqual(components['centre_left_white'], 0.5)
        self.assertAlmostEqual(components['centre_left_black'], 0.5)
        # c2,d2,f2,g2,a2,b2,h2 minus e2: c,d,f (and not e) still on their home squares
        self.assertAlmostEqual(components['centre_files_white'], 0.25)   # only e-file left home
        self.assertAlmostEqual(components['centre_files_black'], 0.25)   # only d-file left home

    def test_components_of_a_developed_board(self):
        components = maturity.position_components(
            self.pieces(white_pawns=('a2', 'b2', 'c4', 'd4', 'f2', 'g2', 'h2'),
                        developed=(('wn', 'c3'), ('wn', 'f3'), ('wb', 'g2'), ('wb', 'b2')),
                        black_developed=(('bn', 'f6'), ('bn', 'c6'))),
            {'w_castled': 'castled_kingside', 'b_castled': 'available_both'})
        self.assertAlmostEqual(components['minors_white'], 1.0)   # 4/4
        self.assertAlmostEqual(components['minors_black'], 0.5)   # Nf6, Nc6 = 2/4
        self.assertEqual(components['castled_white'], 1.0)
        self.assertEqual(components['castled_black'], 0.0)
        self.assertAlmostEqual(components['centre_left_white'], 1.0)

    def test_king_moved_counts_half(self):
        components = maturity.position_components(self.pieces(), {'w_castled': 'king_moved',
                                                                  'b_castled': 'none'})
        self.assertAlmostEqual(components['castled_white'], 0.5)
        self.assertAlmostEqual(components['castled_black'], 0.0)

    def test_development_level_and_depth_residual_are_different_questions(self):
        # A family that is undeveloped but deeper than its peers scores well on the
        # residual and poorly on the absolute level — the corridor/template split.
        structures = {
            f'X{i}': {'ply_mean': 5, 'minors_white': 0.0, 'minors_black': 0.0,
                      'castled_white': 0.0, 'castled_black': 0.0,
                      'centre_left_white': 0.5, 'centre_left_black': 0.5} for i in range(4)}
        structures['corridor'] = {'ply_mean': 5, 'minors_white': 0.25, 'minors_black': 0.0,
                                  'castled_white': 0.0, 'castled_black': 0.0,
                                  'centre_left_white': 0.5, 'centre_left_black': 0.5}
        residuals = maturity.ply_adjust(structures)
        # ahead of schedule: the residual for the component that moved is large...
        self.assertGreater(residuals['corridor']['minors_white'], 1.5)
        # ...while the absolute level is still that of an undeveloped board
        self.assertLess(maturity.development_level(structures['corridor']), 0.25)
        # and the corridor beats its undeveloped peers on the residual only
        self.assertGreater(maturity.maturity_index(residuals, 'corridor'),
                           maturity.maturity_index(residuals, 'X0'))


class TransformationTests(unittest.TestCase):
    def test_pawn_move_structure_change_and_piece_move(self):
        positions = {'P0': position({}, 0, 'S1'), 'P1': position({}, 1, 'S2'),
                     'P2': position({}, 2, 'S2')}
        moves = {'P0': [('P1', 'e2e4', 100)], 'P1': [('P2', 'g8f6', 100)]}
        fens = {'P0': START, 'P1': AFTER_E4, 'P2': AFTER_E4_NF6}
        result = templates.forward_transformations(None, {'P0': 1.0}, positions=positions,
                                                   moves=moves, fen_of=lambda k: fens[k])
        window = result['windows'][(1, 6)]
        self.assertAlmostEqual(window['moves']['pawn e2e4'], 1.0)
        self.assertAlmostEqual(window['moves']['gives up the pawn structure'], 1.0)
        self.assertAlmostEqual(window['moves']['piece N g8->f6'], 1.0)
        self.assertAlmostEqual(window['destinations']['N->f6'], 1.0)

    def test_ply_windows_separate_early_and_late_transformations(self):
        positions = {f'P{i}': position({}, i, f'S{i}') for i in range(4)}
        moves = {'P0': [('P1', 'g1f3', 100)], 'P1': [('P2', 'b1c3', 100)],
                 'P2': [('P3', 'f1c4', 100)]}
        fens = {0: START, 1: 'rnbqkbnr/pppppppp/8/8/8/5N2/PPPPPPPP/RNBQKB1R b KQkq -',
                2: 'rnbqkbnr/pppppppp/8/8/8/2N2N2/PPPPPPPP/R1BQKB1R b KQkq -',
                3: 'rnbqkbnr/pppppppp/8/8/2B5/2N2N2/PPPPPPPP/R1BQK2R b KQkq -'}
        result = templates.forward_transformations(None, {'P0': 1.0}, positions=positions,
                                                   moves=moves, fen_of=lambda k: fens[int(k[1:])])
        early = result['windows'][(1, 6)]['moves']
        self.assertIn('piece N g1->f3', early)
        self.assertIn('piece N b1->c3', early)
        self.assertIn('piece B f1->c4', early)


class FrontierOverlapTests(unittest.TestCase):
    def test_pawn_distance_counts_differing_squares(self):
        class FakeDb:
            def execute(self, sql, params=()):
                rows = {'A': {'structure_id': 'A', 'white_pawns': '000000000000ff00',
                              'black_pawns': '00ff000000000000'},
                        'B': {'structure_id': 'B', 'white_pawns': '000000000000ff00',
                              'black_pawns': '00ff000000000000'},
                        'C': {'structure_id': 'C', 'white_pawns': '000000000000ff00',
                              'black_pawns': '00ef000000000000'}}
                return [rows[p] for p in params if p in rows]
        self.assertEqual(templates.pawn_distance(FakeDb(), 'A', 'B'), 0)
        self.assertEqual(templates.pawn_distance(FakeDb(), 'A', 'C'), 1)


if __name__ == '__main__':
    unittest.main()
