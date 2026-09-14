import sys
import unittest
from pathlib import Path

import chess

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from study import structures  # noqa: E402


class PawnStructureTests(unittest.TestCase):
    def board(self, fen):
        return chess.Board(fen)

    def test_start_position(self):
        fp = structures.fingerprint(self.board(chess.STARTING_FEN))
        self.assertEqual(fp['islands_white'], 1)
        self.assertEqual(fp['islands_black'], 1)
        self.assertEqual(fp['isolated_white'], 0)
        self.assertEqual(fp['doubled_white'], 0)
        self.assertEqual(fp['passed_white'], 0)
        self.assertEqual(fp['passed_black'], 0)
        self.assertEqual(fp['backward_white'], 0)
        self.assertEqual(fp['centre_config'], 0)
        self.assertEqual(fp['open_files'], 0)
        self.assertEqual(fp['dev_white'], 0)
        self.assertEqual(fp['dev_status'], 'undeveloped')

    def test_centre_config_tracks_c4_d4_e4_f4_and_c5_d5_e5_f5(self):
        board = self.board(chess.STARTING_FEN)
        board.push_uci('e2e4')
        board.push_uci('e7e5')
        mask = structures.centre_config(*structures.pawn_masks(board))
        self.assertTrue(mask & (1 << 2))        # white e4
        self.assertTrue(mask & (1 << 6))        # black e5
        self.assertFalse(mask & (1 << 3))       # white f4 absent
        self.assertIn('e4', structures.centre_config_text(mask))
        self.assertIn('e5', structures.centre_config_text(mask))

    def test_isolated_and_islands(self):
        board = self.board('4k3/8/8/8/8/P1P5/8/4K3 w - - 0 1')
        fp = structures.fingerprint(board)
        self.assertEqual(fp['islands_white'], 2)
        self.assertEqual(fp['isolated_white'], 2)
        self.assertEqual(fp['doubled_white'], 0)

    def test_doubled_pawns(self):
        board = self.board('4k3/8/8/8/8/P7/P7/4K3 w - - 0 1')
        fp = structures.fingerprint(board)
        self.assertEqual(fp['doubled_white'], 1)
        self.assertEqual(fp['islands_white'], 1)
        self.assertEqual(fp['isolated_white'], 2)  # neither a-pawn has a neighbour file

    def test_passed_pawns_respect_blocking_enemy_pawns(self):
        # Black d6 and White e4: neither is passed, because each blocks the other.
        board = self.board('4k3/8/3p4/8/4P3/8/8/4K3 w - - 0 1')
        fp = structures.fingerprint(board)
        self.assertEqual(fp['passed_white'], 0)   # black d6 is ahead on an adjacent file
        self.assertEqual(fp['passed_black'], 0)   # white e4 is ahead on an adjacent file
        # A lone black d-pawn is passed.
        board2 = self.board('4k3/8/3p4/8/8/8/8/4K3 w - - 0 1')
        self.assertEqual(structures.fingerprint(board2)['passed_black'], 1)
        self.assertEqual(structures.fingerprint(board2)['passed_white'], 0)
        # A lone white e-pawn is passed.
        board3 = self.board('4k3/8/8/8/4P3/8/8/4K3 w - - 0 1')
        self.assertEqual(structures.fingerprint(board3)['passed_white'], 1)

    def test_backward_is_conservative_and_finds_support(self):
        # White e4, black f6 attacks the e5 advance square; no supporting white pawn.
        lonely = self.board('4k3/8/5p2/8/4P3/8/8/4K3 w - - 0 1')
        self.assertEqual(structures.fingerprint(lonely)['backward_white'], 1)
        # A friendly pawn on d4 (adjacent file, same rank) can support the advance.
        supported = self.board('4k3/8/5p2/8/3PP3/8/8/4K3 w - - 0 1')
        self.assertEqual(structures.fingerprint(supported)['backward_white'], 0)
        # A friendly pawn two files away (c4) is not support.
        distant = self.board('4k3/8/5p2/8/2P1P3/8/8/4K3 w - - 0 1')
        self.assertEqual(structures.fingerprint(distant)['backward_white'], 1)

    def test_open_and_semi_open_files(self):
        # Every file has pawns of both colours: no open files, c-file is semi-open for White.
        board = self.board('4k3/pppppppp/8/8/8/8/PP1PPPPP/4K3 w - - 0 1')
        fp = structures.fingerprint(board)
        self.assertEqual(fp['open_files'], 0)
        self.assertEqual(fp['semi_open_white'], 1 << 2)
        self.assertEqual(fp['semi_open_black'], 0)
        # Remove the black c-pawn too: the c-file is now fully open.
        board2 = self.board('4k3/pp1ppppp/8/8/8/8/PP1PPPPP/4K3 w - - 0 1')
        fp2 = structures.fingerprint(board2)
        self.assertEqual(fp2['open_files'], 1)
        self.assertEqual(fp2['semi_open_white'], 0)
        self.assertEqual(fp2['semi_open_black'], 0)

    def test_structure_id_ignores_non_pawn_pieces(self):
        a = self.board('rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1')
        b = self.board('4k3/pppppppp/8/8/8/8/PPPPPPPP/4K3 w - - 0 1')
        self.assertEqual(structures.structure_id(*structures.pawn_masks(a)),
                         structures.structure_id(*structures.pawn_masks(b)))
        # ... but does not ignore pawn colour or placement.
        c = self.board('4k3/PPPPPPPP/8/8/8/8/pppppppp/4K3 w - - 0 1')
        self.assertNotEqual(structures.structure_id(*structures.pawn_masks(a)),
                            structures.structure_id(*structures.pawn_masks(c)))


class DevelopmentTests(unittest.TestCase):
    def test_development_counts_and_status(self):
        board = chess.Board()
        board.push_uci('g1f3')
        fp = structures.fingerprint(board)
        self.assertEqual(fp['dev_white'], 1)
        self.assertEqual(fp['dev_status'], 'undeveloped')
        for uci in ('g8f6', 'b1c3', 'b8c6', 'e2e4', 'e7e5', 'f1c4', 'f8c5'):
            board.push_uci(uci)
        fp = structures.fingerprint(board)
        # White: Nf3, Nc3, Bc4. Black: Nf6, Nc6, Bc5. Pawn moves are not development.
        self.assertEqual(fp['dev_white'], 3)
        self.assertEqual(fp['dev_black'], 3)
        self.assertEqual(fp['dev_status'], 'partial')

    def test_castling_state_and_rights(self):
        board = chess.Board()
        for uci in ('e2e4', 'e7e5', 'g1f3', 'b8c6', 'f1c4', 'f8c5', 'e1g1'):
            board.push_uci(uci)
        fp = structures.fingerprint(board)
        self.assertEqual(fp['white_castled'], 'castled_kingside')
        self.assertEqual(fp['black_castled'], 'available_both')
        self.assertFalse(fp['castling_rights'] & chess.BB_H1)
        self.assertTrue(fp['castling_rights'] & chess.BB_A8)
        self.assertEqual(fp['dev_white'], 3)   # Nf3, Bc4, king off e1

    def test_castling_rights_alone_report_as_available(self):
        self.assertEqual(structures.castling_state(chess.Board())[0], 'available_both')
        board = chess.Board('4k3/8/8/8/8/8/8/4K3 w - - 0 1')
        self.assertEqual(structures.castling_state(board)[0], 'none')
        moved = chess.Board('4k3/8/8/8/8/8/8/2K5 w - - 0 1')
        # Documented ambiguity: a king on the castled square is read as castled.
        self.assertEqual(structures.castling_state(moved)[0], 'castled_queenside')
        misplaced = chess.Board('4k3/8/8/8/8/8/8/1K6 w - - 0 1')
        self.assertEqual(structures.castling_state(misplaced)[0], 'king_moved')


class MaterialAndRenderTests(unittest.TestCase):
    def test_material_text(self):
        text = structures.material_text(chess.Board())
        self.assertEqual(text, 'W:Q1R2B2N2P8 B:Q1R2B2N2P8')
        board = chess.Board('4k3/8/8/8/8/8/8/4K3 w - - 0 1')
        self.assertEqual(structures.material_text(board), 'W:Q0R0B0N0P0 B:Q0R0B0N0P0')

    def test_render_pawns_shape(self):
        out = structures.render_pawns(*structures.pawn_masks(chess.Board()))
        lines = out.splitlines()
        self.assertEqual(len(lines), 9)
        self.assertTrue(lines[0].startswith('8'))
        self.assertTrue(lines[1].startswith('7'))
        self.assertIn('p', lines[1])    # rank 7 row: black pawns
        self.assertIn('P', lines[6])    # rank 2 row: white pawns
        self.assertTrue(lines[8].endswith('h'))

    def test_fingerprint_has_every_required_field(self):
        fp = structures.fingerprint(chess.Board())
        for field in ('structure_id', 'white_pawns', 'black_pawns', 'centre_config',
                      'open_files', 'semi_open_white', 'semi_open_black',
                      'islands_white', 'islands_black', 'isolated_white', 'isolated_black',
                      'doubled_white', 'doubled_black', 'backward_white', 'backward_black',
                      'passed_white', 'passed_black', 'material', 'castling_rights',
                      'white_castled', 'black_castled', 'dev_white', 'dev_black',
                      'dev_status', 'pieces_json'):
            self.assertIn(field, fp)
        self.assertEqual(fp['white_pawns'], sum(1 << s for s in chess.SquareSet(chess.BB_RANK_2)))


if __name__ == '__main__':
    unittest.main()
