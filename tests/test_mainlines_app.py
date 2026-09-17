"""Tests for the opening-mainlines app: the mirror transform, the dataset, and the server.

The mirror is the part worth guarding. A colour-complex twin is the position with piece
colours and ranks swapped, which necessarily flips the side to move -- so a twin can never
occur at the same ply, and the counterpart search matches on board + castling only. Both
facts are asserted here, because getting either wrong silently pairs unrelated openings.
"""
import importlib.util
import json
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import chess

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA = ROOT / 'analysis' / 'mainlines.json'
UI = ROOT / 'mainlines_ui'


def load_by_path(name, path):
    """Load a module that is not importable as a package member."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f'cannot load {path}'
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD = load_by_path('build_mainlines', ROOT / 'scripts' / 'build_mainlines.py')


class MirrorTest(unittest.TestCase):
    """Image the position in the opposing colour complex."""

    def test_mirroring_twice_is_the_identity(self):
        for line in (['e2e4', 'c7c5', 'g1f3'], ['d2d4', 'g8f6', 'c2c4'],
                     ['f2f4', 'd7d5', 'g1f3', 'g7g6']):
            board = chess.Board()
            for uci in line:
                board.push(chess.Move.from_uci(uci))
            fen = BUILD.key(board)
            self.assertEqual(BUILD.mirror_key(BUILD.mirror_key(fen)), fen)

    def test_mirror_swaps_colours_turn_and_castling(self):
        board = chess.Board()
        board.push_san('e4')
        board.push_san('e5')
        board.push_san('Nf3')
        board.push_san('Nc6')
        board.push_san('Bb5')
        board.push_san('a6')
        fen = BUILD.key(board)
        mirrored = BUILD.mirror_key(fen)
        self.assertEqual(mirrored.split()[1], 'b', 'the side to move must flip')
        self.assertIn('K', fen.split()[2])
        self.assertIn('k', mirrored.split()[2], 'castling rights must swap sides')

    def test_mirror_reproduces_a_line_played_with_mirrored_moves(self):
        """Replaying the mirrored moves from the mirrored start must land on the mirrored end."""
        line = ['d2d4', 'g8f6', 'c2c4', 'g7g6', 'b1c3', 'f8g7']
        board = chess.Board()
        for uci in line:
            board.push(chess.Move.from_uci(uci))
        mirrored_board = chess.Board(BUILD.mirror_key(BUILD.key(chess.Board())) + ' 0 1')
        for uci in line:
            move = chess.Move.from_uci(uci)
            mirrored_board.push(chess.Move(move.from_square ^ 56, move.to_square ^ 56))
        self.assertEqual(BUILD.mirror_key(BUILD.key(board)), BUILD.key(mirrored_board))

    def test_mirror_moves_the_en_passant_file(self):
        fen = 'rnbqkbnr/ppp1p1pp/8/3pPp2/8/8/PPPP1PPP/RNBQKBNR w KQkq f6'
        self.assertTrue(BUILD.mirror_key(fen).endswith('f3'))

    def test_a_mirrored_position_never_keeps_the_side_to_move(self):
        """The reason the counterpart search ignores the turn: it is always the other side."""
        for line in (['e2e4'], ['d2d4'], ['c2c4']):
            board = chess.Board()
            for uci in line:
                board.push(chess.Move.from_uci(uci))
            turn = BUILD.key(board).split()[1]
            self.assertNotEqual(BUILD.mirror_key(BUILD.key(board)).split()[1], turn)


@unittest.skipUnless(DATA.exists(), 'mainlines.json not built')
class DatasetTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(DATA.read_text())
        cls.groups = cls.data['groups']

    def test_every_line_is_a_legal_move_sequence(self):
        for group in self.groups:
            board = chess.Board()
            for record in group['line']:
                move = board.parse_san(record['san'])
                self.assertEqual(move.uci(), record['uci'],
                                 f"{group['name']}: {record['san']} is not {record['uci']}")
                board.push(move)
                # fen is stored AFTER its move, which is what the board renders
                self.assertEqual(BUILD.key(board), record['fen'],
                                 f"{group['name']} ply {record['ply']}: stored fen is stale")

    def test_positions_are_unique_across_entries(self):
        """One entry per position -- the whole point of the condensation."""
        seen = {}
        for group in self.groups:
            final = group['line'][-1]['fen']
            self.assertNotIn(final, seen,
                             f"{group['name']} duplicates {seen.get(final)}")
            seen[final] = group['name']

    def test_representation_is_consistent(self):
        for group in self.groups:
            self.assertTrue(group['ecos'], f"{group['name']} has no ECO code")
            self.assertEqual(group['ecos'], sorted(set(group['ecos'])))
            self.assertGreaterEqual(group['n_collapsed'], 1)
            # several taxonomy lines can share one name, so aliases are distinct names and
            # can be fewer than the collapsed line count
            self.assertLessEqual(len(group['aliases']), group['n_collapsed'])
            self.assertEqual(len(set(group['aliases'])), len(group['aliases']))
            self.assertNotIn(group['name'], group['aliases'])
            self.assertTrue(group['base'])

    def test_counterparts_are_mutual_and_mirrored(self):
        by_id = {g['id']: g for g in self.groups}
        paired = [g for g in self.groups if g['counterpart']]
        self.assertTrue(paired, 'expected at least one colour twin')
        self.assertEqual(len(paired) % 2, 0, 'twins must come in pairs')
        for group in paired:
            twin = group['counterpart']
            other = by_id[twin['group']]
            self.assertIsNotNone(other['counterpart'], 'the twin must point back')
            self.assertEqual(other['counterpart']['group'], group['id'],
                             f"{group['name']} is not a mutual pair")
            self.assertNotEqual(other['id'], group['id'], 'an entry cannot twin with itself')
            # the recorded twin position must really be this entry's position, mirrored.
            # The turn legitimately differs -- swapping colours flips the side to move, so
            # the pair sits one tempo apart -- which is why the match is on board+castling.
            here = group['line'][twin['ply'] - 1]['fen']
            there = other['line'][twin['counterpart_ply'] - 1]['fen']
            mirrored = BUILD.mirror_key(here)
            self.assertEqual(mirrored.split()[0], there.split()[0],
                             f"{group['name']}: mirrored board differs from the twin")
            self.assertEqual(mirrored.split()[2], there.split()[2])
            self.assertNotEqual(mirrored.split()[1], there.split()[1],
                                'a colour twin must sit on the other side to move')
            self.assertEqual(mirrored, twin['mirror_fen'])

    def test_every_family_member_exists_and_bands_agree(self):
        by_id = {g['id']: g for g in self.groups}
        covered = set()
        for family in self.data['families']:
            self.assertTrue(family['groups'])
            for gid in family['groups']:
                self.assertIn(gid, by_id)
                covered.add(gid)
                self.assertEqual(by_id[gid]['base'], family['name'])
            self.assertEqual(sum(by_id[i]['games'] for i in family['groups']), family['games'])
        self.assertEqual(len(covered), len(self.groups), 'every entry belongs to a family')

    def test_ecos_sorted_by_code_in_the_family_list(self):
        codes = [f['eco'] for f in self.data['families']]
        self.assertEqual(codes, sorted(codes))


@unittest.skipUnless(DATA.exists() and UI.exists(), 'app not built')
class ServerTest(unittest.TestCase):
    """The server is read-only; it must serve the app and refuse everything else."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            'mainlines_server', ROOT / 'study' / 'mainlines_server.py')
        assert spec and spec.loader, 'cannot load the server module'
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), module.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path):
        return urlopen(f'http://127.0.0.1:{self.port}{path}', timeout=20)

    def test_serves_the_ui(self):
        body = self.get('/').read().decode()
        self.assertIn('Opening Mainlines', body)

    def test_serves_static_assets(self):
        self.assertEqual(self.get('/ui/app.js').status, 200)
        self.assertEqual(self.get('/ui/styles.css').status, 200)
        self.assertEqual(self.get('/ui/board-fritz.jpg').status, 200)

    def test_serves_the_dataset(self):
        payload = json.loads(self.get('/mainlines.json').read())
        self.assertIn('groups', payload)
        self.assertIn('families', payload)

    def test_unknown_paths_are_404(self):
        for path in ('/nope', '/ui/../../etc/passwd', '/ui/missing.css'):
            with self.assertRaises(HTTPError) as caught:
                self.get(path)
            self.assertEqual(caught.exception.code, 404)
            caught.exception.close()


if __name__ == '__main__':
    unittest.main()
