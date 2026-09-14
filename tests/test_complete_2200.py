import contextlib
import io
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch
import chess
import explorer
import lumbra
import complete_2200 as full
from test_lumbra import game


class FullIndexTests(unittest.TestCase):
    def setUp(self):
        # Fixture directories may be on a small tmpfs; production keeps its guard.
        from types import SimpleNamespace
        guard=patch('complete_2200.shutil.disk_usage',return_value=SimpleNamespace(free=100*1024**3))
        guard.start();self.addCleanup(guard.stop)

    def test_rare_transposed_terminal_and_repeated_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); pgn=root/'games.pgn'; path=root/'full.sqlite'
            pgn.write_text(game('1. e4 c5 2. Nc3 g6 3. Bc4 Bg7')+
                           game('1. Nf3 Nf6 2. g3 g6 3. Bg2')+
                           game('1. g3 g6 2. Nf3 Nf6 3. Bg2')+
                           game('1. Nf3 Nf6 2. Ng1 Ng8 3. e4')+
                           game('1. d4',black='2199'))
            with contextlib.redirect_stdout(io.StringIO()): state=full.run(pgn,path,batch=2)
            self.assertEqual(state['accepted'],4)
            db=full.connect(path)
            b=chess.Board()
            for san in ('e4','c5','Nc3','g6','Bc4'): b.push_san(san)
            rows=db.execute('SELECT * FROM edges WHERE position=?',(full.compact_key(b),)).fetchall()
            self.assertEqual(len(rows),1)
            self.assertEqual(full.decode_move(rows[0]['move']).uci(),'f8g7')
            self.assertEqual(rows[0]['white'],1)
            b.push_san('Bg7')
            self.assertEqual(db.execute('SELECT move FROM edges WHERE position=?',(full.compact_key(b),)).fetchone()[0],0)
            b=chess.Board()
            for san in ('Nf3','Nf6','g3','g6'):b.push_san(san)
            self.assertEqual(db.execute('SELECT white FROM edges WHERE position=?',(full.compact_key(b),)).fetchone()[0],2)
            rows=db.execute('SELECT * FROM edges WHERE position=?',(full.compact_key(chess.Board()),)).fetchall()
            self.assertEqual(sum(r['white'] for r in rows),4)
            self.assertEqual(next(r['white'] for r in rows if full.decode_move(r['move']).uci()=='e2e4'),1)
            db.close()

    def test_resume_atomic_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); pgn=root/'games.pgn'; path=root/'full.sqlite'
            pgn.write_text(game('1. e4 e5')*5)
            original=full.checkpoint; calls=0
            def interrupt(db,state):
                nonlocal calls
                calls+=1
                if calls==3: raise RuntimeError('simulate failure')
                original(db,state)
            with contextlib.redirect_stdout(io.StringIO()):
                with patch('complete_2200.checkpoint',interrupt):
                    with self.assertRaises(RuntimeError):full.run(pgn,path,batch=2)
                state=full.run(pgn,path,batch=2)
                full.run(pgn,path,batch=2)
            self.assertEqual(state['accepted'],5)
            db=full.connect(path)
            self.assertEqual(db.execute('SELECT SUM(white) FROM edges WHERE position=?',(full.compact_key(chess.Board()),)).fetchone()[0],5)
            db.close()

    def test_key_identity_and_move_encoding(self):
        rng=random.Random(7); seen={}; b=chess.Board()
        for _ in range(1000):
            if b.is_game_over():b=chess.Board()
            k=full.compact_key(b); fen=explorer.key(b)
            self.assertEqual(len(k),34)
            self.assertEqual(seen.setdefault(k,fen),fen)
            m=rng.choice(list(b.legal_moves))
            self.assertEqual(full.decode_move(full.encode_move(m)),m)
            b.push(m)
        a=chess.Board();a.push_san('e4');b=a.copy();b.ep_square=None;b.halfmove_clock=99
        self.assertEqual(full.compact_key(a),full.compact_key(b))
        a=chess.Board('4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1');b=a.copy();b.ep_square=None
        self.assertNotEqual(full.compact_key(a),full.compact_key(b))
        a=chess.Board();b=a.copy();b.castling_rights=0
        self.assertNotEqual(full.compact_key(a),full.compact_key(b))
        for promotion in 'qrbn':
            move=chess.Move.from_uci('a7a8'+promotion)
            self.assertEqual(full.decode_move(full.encode_move(move)),move)

    def test_overlay_preserves_finished_counts_and_handles_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);pgn=root/'games.pgn';path=root/'full.sqlite'
            pgn.write_text(game('1. e4 c5 2. Nc3 g6 3. Bc4 Bg7'))
            with contextlib.redirect_stdout(io.StringIO()):full.run(pgn,path)
            db=explorer.connect(root/'api.sqlite')
            seq=['e2e4','c7c5','b1c3','g7g6','f1c4']
            data=lumbra.decorate(explorer.position(db,seq),root/'missing.sqlite')
            result=full.overlay(data,path)
            self.assertEqual(result['local_totals'],[None,1])
            self.assertEqual(result['moves'][0]['san'],'Bg7')
            self.assertEqual(result['moves'][0]['lumbra2200_percent'],100)
            self.assertEqual(result['strong_position_coverage'],'complete')
            data=lumbra.decorate(explorer.position(db,['a2a4']),root/'missing.sqlite')
            result=full.overlay(data,path)
            self.assertEqual(result['local_totals'][1],0)
            self.assertTrue(all(m['lumbra2200']==0 for m in result['moves']))
            self.assertNotIn('input',result['complete_2200_import'])
            db.close()

    def test_partial_rebuild_keeps_finished_frequent_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);pgn=root/'games.pgn';path=root/'full.sqlite'
            pgn.write_text(game('1. e4'))
            with contextlib.redirect_stdout(io.StringIO()):full.run(pgn,path)
            compact=full.connect(path)
            state=full.get_state(compact);state['phase']='counting'
            with compact:full.checkpoint(compact,state)
            compact.close()
            db=explorer.connect(root/'api.sqlite')
            data=lumbra.decorate(explorer.position(db,[]),root/'missing.sqlite')
            data['local_totals'][1]=123
            e4=next(m for m in data['moves'] if m['san']=='e4')
            e4['lumbra2200']=123
            result=full.overlay(data,path)
            self.assertEqual(result['local_totals'][1],123)
            self.assertEqual(next(m['lumbra2200'] for m in result['moves'] if m['san']=='e4'),123)
            self.assertEqual(result['strong_position_coverage'],'complete_frequent')
            data=lumbra.decorate(explorer.position(db,[]),root/'missing.sqlite')
            result=full.overlay(data,path)
            self.assertEqual(result['local_totals'][1],1)
            self.assertEqual(result['strong_position_coverage'],'partial')
            db.close()
