"""Exact one-pass index of every position in standard-start both-2200+ games."""
import argparse
import contextlib
import fcntl
import json
from pathlib import Path
import shutil
import sqlite3
import struct
import time
import chess
import chess.pgn
from lumbra import Visitor, get_state, checkpoint

ROOT = Path(__file__).resolve().parent


def compact_key(board):
    """Lossless 34-byte canonical position, not a probabilistic hash.

    Three bitplanes encode piece type, one encodes white occupancy. The final
    bytes encode standard castling rights, side to move and legal en passant.
    """
    rights = board.clean_castling_rights()
    flags = int(board.turn) << 4
    for i, square in enumerate((chess.H1, chess.A1, chess.H8, chess.A8)):
        if rights & chess.BB_SQUARES[square]: flags |= 1 << i
    return struct.pack('<4QBB', board.pawns | board.bishops | board.queens,
                       board.knights | board.bishops | board.kings,
                       board.rooks | board.queens | board.kings,
                       board.occupied_co[chess.WHITE], flags,
                       board.ep_square if board.has_legal_en_passant() else 64)


def encode_move(move):
    return move.from_square | (move.to_square << 6) | ((move.promotion or 0) << 12)


def decode_move(code):
    return chess.Move(code & 63, (code >> 6) & 63, promotion=(code >> 12) or None)


class FullVisitor(Visitor):
    def __init__(self): super().__init__(None, True, 2200)
    def visit_board(self, board): self.rows.setdefault(compact_key(board), 0)
    def visit_move(self, board, move):
        if not move:
            self.error = 'null_move'
            return
        k = compact_key(board)
        if not self.rows.get(k): self.rows[k] = encode_move(move)


def connect(path):
    db = sqlite3.connect(path, timeout=60)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA cache_size=-65536')
    db.execute('PRAGMA wal_autocheckpoint=16384')
    db.executescript('''
      CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1),value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS edges(position BLOB, move INTEGER,
        white INTEGER NOT NULL, draws INTEGER NOT NULL, black INTEGER NOT NULL,
        PRIMARY KEY(position,move)) WITHOUT ROWID;
    ''')
    return db


def flush(db, buffer, state):
    with db:
        db.executemany('''INSERT INTO edges VALUES(?,?,?,?,?) ON CONFLICT(position,move)
          DO UPDATE SET white=white+excluded.white, draws=draws+excluded.draws,
          black=black+excluded.black''', ((k, m, *counts) for (k,m),counts in sorted(buffer.items())))
        checkpoint(db, state)
    buffer.clear()


def run(pgn, path, limit=None, batch=1000):
    pgn, path = Path(pgn).resolve(), Path(path).resolve()
    stat = pgn.stat()
    identity = {'file':str(pgn), 'bytes':stat.st_size, 'mtime_ns':stat.st_mtime_ns,
                'limit':limit, 'version':'lossless-bitplanes-v1', 'minimum_rating':2200}
    with contextlib.closing(connect(path)) as db:
        state = get_state(db)
        if state and state['input'] != identity: raise ValueError('Input/settings changed; use a new database')
        if not state:
            state = {'input':identity, 'phase':'counting', 'threshold':1, 'sample':limit is not None,
                     'games':0, 'accepted':0, 'strong':0, 'excluded':{}, 'offset':0,
                     'started':time.time(), 'updated':time.time()}
            with db: checkpoint(db, state)
        if state['phase'] == 'complete': return state
        buffer = {}
        with pgn.open(encoding='utf-8-sig', errors='replace') as handle:
            handle.seek(state['offset'])
            while limit is None or state['games'] < limit:
                if state['games'] % batch == 0 and shutil.disk_usage(path.parent).free < 5 * 1024**3:
                    raise RuntimeError('Less than 5 GiB free; stopped at last saved checkpoint')
                game = chess.pgn.read_game(handle, Visitor=FullVisitor)
                if game is None: break
                state['games'] += 1
                if game.error:
                    state['excluded'][game.error] = state['excluded'].get(game.error,0) + 1
                else:
                    state['accepted'] += 1; state['strong'] += 1
                    for k,m in game.rows.items():
                        counts = buffer.setdefault((k,m), [0,0,0])
                        counts[game.outcome] += 1
                if state['games'] % batch == 0:
                    state.update(offset=handle.tell(), updated=time.time())
                    flush(db, buffer, state)
                    print(json.dumps({k:state[k] for k in ('phase','games','accepted','updated')}),flush=True)
            state.update(offset=handle.tell(), updated=time.time())
            flush(db, buffer, state)
        root = db.execute('SELECT SUM(white+draws+black) FROM edges WHERE position=?', (compact_key(chess.Board()),)).fetchone()[0] or 0
        if root != state['accepted']: raise ValueError('Root game count does not match accepted total')
        state.update(phase='complete', finished=time.time())
        with db: checkpoint(db, state)
        print(json.dumps({k:v for k,v in state.items() if k!='input'}),flush=True)
        return state


def overlay(data, path, reference='2200'):
    """Keep completed frequent-position counts until the full replacement finishes.

    Rare positions may show clearly labelled partial counts while importing.
    """
    if not Path(path).exists(): return data
    with contextlib.closing(sqlite3.connect('file:'+str(Path(path).resolve())+'?mode=ro',uri=True,timeout=30)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        state = get_state(db)
        if not state: return data
        from explorer import board_for
        rows = db.execute('SELECT move,white,draws,black FROM edges WHERE position=?', (compact_key(board_for(data['key'])),)).fetchall()
    public = {k:v for k,v in state.items() if k!='input'}
    public['progress_percent'] = min(100,100 * state['offset']/state['input']['bytes'])
    data['complete_2200_import'] = public
    done = state['phase'] == 'complete' and not state.get('sample')
    if not done and data['local_totals'][1] is not None:
        data['strong_position_coverage'] = 'complete_frequent'
        return data
    counts = {decode_move(r['move']).uci():sum(r[f] for f in ('white','draws','black')) for r in rows if r['move']}
    denominator = sum(counts.values())
    total = sum(sum(r[f] for f in ('white','draws','black')) for r in rows)
    known = bool(rows) or done
    for move in data['moves']:
        n = counts.get(move['uci'],0) if known else None
        move['lumbra2200'] = n
        move['lumbra2200_percent'] = n/denominator*100 if denominator else None
        if reference == '2200': move['reference_percent'] = move['lumbra2200_percent']
    data['local_totals'][1] = total if known else None
    data['strong_import'] = public
    data['strong_position_coverage'] = 'complete' if done else 'partial' if rows else 'pending'
    if reference == '2200':
        data['local_retained'] = bool(rows)
        data['parents'] = 0  # No incoming-edge index in the compact database.
    field = 'lumbra2200' if reference == '2200' else 'lumbra'
    data['moves'].sort(key=lambda m:(-(m[field] or 0),-(m['lichess'] or 0),m['san']))
    return data


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pgn',type=Path,default=ROOT/'data/LumbrasGigaBase_OTB_Complete.pgn')
    p.add_argument('--db',type=Path,default=ROOT/'data/lumbra-2200-complete.sqlite')
    p.add_argument('--limit',type=int)
    args=p.parse_args()
    with open(str(args.db)+'.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        run(args.pgn,args.db,args.limit)
