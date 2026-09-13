import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import chess
import explorer
import lumbra


def game(moves, white='2300', black='2200', result='1-0'):
    return f'[Event "Fixture"]\n[WhiteElo "{white}"]\n[BlackElo "{black}"]\n[Result "{result}"]\n\n{moves} {result}\n\n'


class ImportTests(unittest.TestCase):
    def test_exact_transpositions_ratings_repetitions_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            pgn, dbpath = Path(directory)/'test.pgn', Path(directory)/'test.sqlite'
            pgn.write_text(game('1. Nf3 Nf6 2. g3 g6 3. Bg2') +
                           game('1. g3 g6 2. Nf3 Nf6 3. Bg2', black='2199') +
                           game('1. Nf3 Nf6 2. Ng1 Ng8 3. e4', white='?') +
                           game('1. d4', result='1/2-1/2') +
                           game('1. e4 e5 2. Bh6') + game('1. e4', result='*'))
            with contextlib.redirect_stdout(io.StringIO()):
                state = lumbra.run(pgn, dbpath, bits=8, threshold=1, batch=2)
                lumbra.run(pgn, dbpath, bits=8, threshold=1, batch=2)
            self.assertEqual(state['accepted'], 4)
            self.assertEqual(state['strong'], 2)
            self.assertEqual(state['excluded'], {'parse_error': 1, 'unknown_result': 1})
            db = lumbra.connect(dbpath)
            root = explorer.key(chess.Board())
            rows = {r['uci']: r for r in db.execute('SELECT * FROM counts WHERE position=?', (root,))}
            self.assertEqual(rows['']['white'], 3)
            self.assertEqual(rows['']['draws2200'], 1)
            self.assertNotIn('e2e4', rows)  # Repetition uses first continuation only.
            board = chess.Board()
            for san in ('Nf3','Nf6','g3','g6'): board.push_san(san)
            row = db.execute('SELECT * FROM counts WHERE position=? AND uci=?', (explorer.key(board),'f1g2')).fetchone()
            self.assertEqual(row['white'], 2)
            self.assertEqual(row['white2200'], 1)
            db.close()

    def test_fingerprint_identity(self):
        a = chess.Board(); a.push_san('e4')
        b = a.copy(); b.ep_square = None; b.halfmove_clock = 50
        self.assertEqual(lumbra.fingerprint(a), lumbra.fingerprint(b))
        a = chess.Board('4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1')
        b = a.copy(); b.ep_square = None
        self.assertNotEqual(lumbra.fingerprint(a), lumbra.fingerprint(b))
        a = chess.Board(); b = a.copy(); b.castling_rights = 0
        self.assertNotEqual(lumbra.fingerprint(a), lumbra.fingerprint(b))

    def test_checkpoint_recovery_and_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            pgn, path = Path(directory)/'test.pgn', Path(directory)/'test.sqlite'
            pgn.write_text(game('1. e4') * 3 + game('1. d4'))
            original = lumbra.aggregate
            calls = 0
            def interrupted(db, visitor):
                nonlocal calls
                calls += 1
                original(db, visitor)
                if calls == 3: raise RuntimeError('Interruption')
            with contextlib.redirect_stdout(io.StringIO()):
                with patch('lumbra.aggregate', interrupted):
                    with self.assertRaises(RuntimeError): lumbra.run(pgn, path, bits=4, threshold=3, batch=2)
                result = lumbra.run(pgn, path, bits=4, threshold=3, batch=2)
            db = lumbra.connect(path)
            self.assertEqual(result['accepted'], 4)
            self.assertEqual(result['retained_positions'], 2)
            self.assertEqual(db.execute("SELECT white FROM counts WHERE position=? AND uci=''", (explorer.key(chess.Board()),)).fetchone()[0], 4)
            db.close()

    def test_missing_and_complete_local_overlay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = explorer.connect(root/'old.sqlite')
            data = lumbra.decorate(explorer.position(old, []), root/'missing.sqlite')
            self.assertIsNone(data['moves'][0]['lumbra'])
            pgn = root/'games.pgn'; pgn.write_text(game('1. e4') + game('1. d4', black='2199'))
            with contextlib.redirect_stdout(io.StringIO()): lumbra.run(pgn, root/'local.sqlite', bits=4, threshold=1)
            data = lumbra.decorate(explorer.position(old, []), root/'local.sqlite', '2200')
            self.assertEqual(data['local_totals'], [2,1])
            self.assertEqual(data['moves'][0]['san'], 'e4')
            self.assertEqual(data['moves'][0]['reference_percent'], 100)
            self.assertIsNone(data['moves'][0]['lichess'])
            self.assertNotIn('input',data['import'])
            old.close()

class EnrichmentTests(unittest.TestCase):
    def test_enrichment_only_missing_lichess_and_reuses_cache(self):
        import enrich_lumbra
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pgn = root/'games.pgn'; pgn.write_text(game('1. e4'))
            with contextlib.redirect_stdout(io.StringIO()): lumbra.run(pgn, root/'local.sqlite', bits=4, threshold=1)
            db = explorer.connect(root/'cache.sqlite')
            with db: db.execute("INSERT INTO meta VALUES('poc_verified','fixture')")
            explorer.save(db, explorer.key(chess.Board()), 'lichess', {'white':1,'draws':0,'black':0,'moves':[]})
            db.close()
            class Fake:
                calls = []
                def get(self, source, board):
                    self.calls.append((source, explorer.key(board)))
                    return {'white':0,'draws':0,'black':0,'moves':[]}
            fake = Fake()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(enrich_lumbra.enrich(root/'local.sqlite', root/'cache.sqlite', fake),1)
                self.assertEqual(enrich_lumbra.enrich(root/'local.sqlite', root/'cache.sqlite', fake),0)
            self.assertEqual([s for s,_ in fake.calls], ['lichess'])
