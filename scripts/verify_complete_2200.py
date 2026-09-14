"""Post-import verification for the complete 2200+ position index.

Run only when the importer has stopped (phase == complete). Checks the claims in
HERMES-HANDOFF.md "Verification and remaining work": root totals, accepted-game
count against the completed frequent-position fallback, terminal rows, and the
1.e4 c5 2.Nc3 g6 3.Bc4 regression that prompted the rebuild.

Exit code 0 = every check passed.
"""
import json
import sqlite3
import sys
from pathlib import Path

import chess

import complete_2200

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / 'data/lumbra-2200-complete.sqlite'
OLD = ROOT / 'data/lumbra-2200.sqlite'

FAILURES = []


def check(name, ok, detail=''):
    status = 'PASS' if ok else 'FAIL'
    if not ok:
        FAILURES.append(name)
    print(f'[{status}] {name}' + (f' — {detail}' if detail else ''))


def state_of(path):
    with sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT value FROM state WHERE id=1').fetchone()
    return json.loads(row[0]) if row else None


def key_after(uci_moves):
    board = chess.Board()
    for uci in uci_moves.split(','):
        board.push_uci(uci)
    return complete_2200.compact_key(board)


state = state_of(DB)
if not state:
    print('[FAIL] state row present')
    sys.exit(1)

print('phase             :', state.get('phase'))
print('records scanned   :', state.get('games'))
print('accepted games    :', state.get('accepted'))
print('offset / bytes    :', state.get('offset'), '/', state['input']['bytes'])
print('exclusions        :', state.get('excluded'))
print()

check('phase is complete', state.get('phase') == 'complete', state.get('phase'))
check('not a sample run', not state.get('sample'))
check('scanned whole PGN', state.get('offset') == state['input']['bytes'],
      f"{state.get('offset')} vs {state['input']['bytes']}")

db = sqlite3.connect('file:' + str(DB) + '?mode=ro', uri=True)
db.execute('PRAGMA query_only=ON')

root_total = db.execute('SELECT COALESCE(SUM(white+draws+black),0) FROM edges WHERE position=?',
                        (complete_2200.compact_key(chess.Board()),)).fetchone()[0]
check('root total equals accepted games', root_total == state['accepted'],
      f'{root_total} vs {state["accepted"]}')

terminal = db.execute('SELECT COUNT(*) FROM edges WHERE move=0').fetchone()[0]
check('terminal positions stored', terminal > 0, f'{terminal} game-ending rows')

rare_moves = db.execute('SELECT COUNT(*) FROM edges WHERE move!=0 AND white+draws+black=1').fetchone()[0]
check('positions seen only once are indexed', rare_moves > 0, f'{rare_moves} one-game move rows')

# Rare-but-legal continuations must exist, not just common ones.
parent = key_after('e2e4,c7c5,b1c3,g7g6')
bc4 = complete_2200.encode_move(chess.Move.from_uci('f1c4'))
row = db.execute('SELECT white,draws,black FROM edges WHERE position=? AND move=?',
                 (parent, bc4)).fetchone()
bc4_games = sum(row) if row else 0
check('regression: Bc4 continuation indexed', bc4_games > 0, f'{bc4_games} games')

child = key_after('e2e4,c7c5,b1c3,g7g6,f1c4')
child_total = db.execute('SELECT COALESCE(SUM(white+draws+black),0) FROM edges WHERE position=?',
                         (child,)).fetchone()[0]
check('regression: Bc4 child position has statistics', child_total > 0,
      f'{child_total} games (incoming Bc4 edge: {bc4_games})')
check('child total is not below its incoming edge', child_total >= bc4_games,
      f'{child_total} >= {bc4_games}')

old = state_of(OLD)
if old:
    print()
    print('old frequent-position 2200+ index accepted games:', old.get('accepted'))
    delta = state['accepted'] - old['accepted']
    print('difference (new - old)                         :', delta)
    check('accepted total matches the old index', state['accepted'] == old['accepted'],
          f'delta {delta} (review exclusions if nonzero)')
    with sqlite3.connect('file:' + str(OLD) + '?mode=ro', uri=True) as odb:
        old_retained = odb.execute('SELECT COUNT(*) FROM retained').fetchone()[0]
    print('old retained positions:', old_retained)

db.close()
print()
if FAILURES:
    print(f'{len(FAILURES)} check(s) FAILED: ' + ', '.join(FAILURES))
    sys.exit(1)
print('all checks passed')
