import contextlib, io, json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
import chess
import explorer as e

def payload(board,items):
    return dict(white=sum(n for _,n in items),draws=0,black=0,moves=[dict(uci=board.parse_san(s).uci(),san=s,white=n,draws=0,black=0) for s,n in items])

class GraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=e.connect(str(Path(self.tmp.name)/'test.sqlite'))
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def test_transpositions(self):
        a,b=chess.Board(),chess.Board()
        for san in ['Nf3','Nf6','g3','g6']:a.push_san(san)
        for san in ['g3','g6','Nf3','Nf6']:b.push_san(san)
        self.assertEqual(e.key(a),e.key(b));b.halfmove_clock=90;b.fullmove_number=70
        self.assertEqual(e.key(a),e.key(b))
    def test_transposed_statistics_and_history(self):
        a=['d2d4','g8f6','c2c4','e7e6']
        b=['c2c4','e7e6','d2d4','g8f6']
        board=chess.Board()
        for uci in a: board.push_uci(uci)
        k=e.key(board)
        with self.db:self.db.execute('INSERT INTO positions VALUES (?,4)',(k,))
        e.save(self.db,k,'masters',payload(board,[('Nc3',100)]))
        x,y=e.position(self.db,a),e.position(self.db,b)
        self.assertEqual(x['moves'],y['moves']);self.assertNotEqual(x['sans'],y['sans'])
    def test_ep_castling(self):
        a=chess.Board();a.push_san('e4');self.assertTrue(e.key(a).endswith('-'))
        a=chess.Board('4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1');self.assertTrue(e.key(a).endswith('d6'))
        b=a.copy();b.ep_square=None;self.assertNotEqual(e.key(a),e.key(b))
        a=chess.Board();b=a.copy();b.castling_rights=0;self.assertNotEqual(e.key(a),e.key(b))
    def test_threshold_major_missing(self):
        b=chess.Board();k=e.key(b)
        e.save(self.db,k,'masters',payload(b,[('e4',100),('d4',99)]));e.expand(self.db,k,0)
        self.assertEqual(e.status(self.db)['positions'],2)
        e.save(self.db,k,'lichess',payload(b,[('e4',20),('d4',79),('a3',1)]))
        moves={m['san']:m for m in e.position(self.db,[])['moves']}
        self.assertTrue(moves['e4']['major']);self.assertEqual(moves['e4']['percent'],20)
        self.assertFalse(moves['a3']['major']);self.assertEqual(moves['h3']['lichess'],0)
        self.assertIsNone(e.position(self.db,['e2e4'])['moves'][0]['lichess'])
    def test_resume_depth_extension(self):
        class Fake:
            def __init__(self):self.calls=[]
            def get(self,source,b):
                self.calls.append((source,e.key(b)))
                return payload(b,[('e4',100)] if e.key(b)==e.key(chess.Board()) else [])
        c=Fake()
        with contextlib.redirect_stdout(io.StringIO()):
            e.crawl(self.db,c,max_ply=0);self.assertEqual(len(c.calls),2)
            e.crawl(self.db,c,max_ply=0);self.assertEqual(len(c.calls),2)
            e.crawl(self.db,c)
        self.assertEqual(len(c.calls),4);self.assertTrue(e.status(self.db)['complete'])
    def test_recovery_between_sources(self):
        b=chess.Board();e.save(self.db,e.key(b),'masters',payload(b,[('e4',100)]))
        class Fake:
            def get(self,source,board):
                assert source=='lichess'
                return payload(board,[])
        with contextlib.redirect_stdout(io.StringIO()):e.crawl(self.db,Fake(),max_ply=0)
        self.assertEqual(e.status(self.db)['positions'],2)
    def test_bad_payload_preserves_snapshot(self):
        b=chess.Board();k=e.key(b);p=payload(b,[('e4',100)]);e.save(self.db,k,'masters',p)
        bad=payload(b,[('d4',100)]);bad['moves'][0]['uci']='e2e5'
        with self.assertRaises(ValueError):e.save(self.db,k,'masters',bad)
        self.assertEqual(json.loads(self.db.execute('SELECT raw FROM snapshots').fetchone()[0]),p)
    def test_illegal_line(self):
        with self.assertRaises(ValueError):e.position(self.db,['e2e5'])
    def test_cycle(self):
        k=e.key(chess.Board())
        with self.db:self.db.execute('INSERT INTO moves VALUES (?,?,?,?,?,?,?,?,?)',(k,'masters','g1f3','Nf3',k,100,0,0,'{}'))
        e.expand(self.db,k,0);self.assertEqual(e.status(self.db)['positions'],1)
    def test_terminal_denominator(self):
        b=chess.Board();p=payload(b,[('e4',20),('d4',80)]);p['draws']=50
        e.save(self.db,e.key(b),'lichess',p);d=e.position(self.db,[])
        self.assertEqual(d['continuation_total'],100)
        self.assertEqual(next(m['percent'] for m in d['moves'] if m['san']=='e4'),20)
        e.save(self.db,e.key(b),'lichess',payload(b,[]))
        self.assertTrue(all(m['percent'] is None for m in e.position(self.db,[])['moves']))
    def test_rate_limit(self):
        waits=[];response=io.BytesIO(json.dumps(payload(chess.Board(),[])).encode())
        with patch('explorer.request.urlopen',side_effect=[HTTPError('url',429,'rate',{},None),response]):
            with contextlib.redirect_stdout(io.StringIO()):e.Client('fake',0,sleep=waits.append).get('masters',chess.Board())
        self.assertIn(60,waits)
    def test_auth_no_retry(self):
        with patch('explorer.request.urlopen',side_effect=HTTPError('url',401,'auth',{},None)) as c:
            with self.assertRaisesRegex(RuntimeError,'authentication'):e.Client('fake',0).get('masters',chess.Board())
            self.assertEqual(c.call_count,1)
if __name__=='__main__':unittest.main()
