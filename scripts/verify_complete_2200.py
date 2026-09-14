"""Post-import verification for the complete 2200+ position index.

Run only when the importer has stopped (phase == complete). Covers the
"Verification and remaining work" section of HERMES-HANDOFF.md:

  * phase/offset/sample state
  * root position total equals the accepted-game count
  * terminal positions are stored (game-ending rows)
  * the 1.e4 c5 2.Nc3 g6 3.Bc4 regression that prompted the rebuild, including
    proof that continuations below the old 100-game cutoff are stored
  * transposition identity (different move orders, one position key)
  * every position shared with the completed frequent-position index matches it
    exactly (this also covers transposition merging, because that index is
    keyed by canonical FEN)

One full table scan (`move=0`) is included; it is only affordable once the
writer has stopped, which is the required precondition anyway.

Exit code 0 = every check passed.
"""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import chess

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import complete_2200  # noqa: E402  (repository root must be importable)

DB = ROOT / 'data/lumbra-2200-complete.sqlite'
OLD = ROOT / 'data/lumbra-2200.sqlite'

FAILURES = []


def check(name, ok, detail=''):
    if not ok:
        FAILURES.append(name)
    print(f'[{"PASS" if ok else "FAIL"}] {name}' + (f' — {detail}' if detail else ''))


def state_of(path):
    with sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT value FROM state WHERE id=1').fetchone()
    return json.loads(row[0]) if row else None


def key_after(uci_moves):
    board = chess.Board()
    for uci in uci_moves.split(','):
        board.push_uci(uci)
    return complete_2200.compact_key(board)


def rows_for(db, key):
    return db.execute('SELECT move, white, draws, black FROM edges WHERE position=?', (key,)).fetchall()


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

check('phase is complete', state.get('phase') == 'complete', str(state.get('phase')))
check('not a sample run', not state.get('sample'))
check('scanned whole PGN', state.get('offset') == state['input']['bytes'],
      f"{state.get('offset')} vs {state['input']['bytes']}")

db = sqlite3.connect('file:' + str(DB) + '?mode=ro', uri=True, timeout=60)
db.execute('PRAGMA query_only=ON')

root_total = sum(sum(r[1:]) for r in rows_for(db, complete_2200.compact_key(chess.Board())))
check('root total equals accepted games', root_total == state['accepted'],
      f'{root_total} vs {state["accepted"]}')

# Whole-index terminal signal. Sum over move=0 rows is at most the accepted total
# because a game that ends on a repeated position keeps its earlier continuation.
terminal_total = db.execute('SELECT COALESCE(SUM(white+draws+black),0) FROM edges WHERE move=0').fetchone()[0]
check('game-ending rows are a plausible share of accepted games',
      0 < terminal_total <= state['accepted'], f'{terminal_total} of {state["accepted"]}')

# Regression: the position behind the rebuild, and proof of sub-100 continuations.
parent = key_after('e2e4,c7c5,b1c3,g7g6')
bc4 = complete_2200.encode_move(chess.Move.from_uci('f1c4'))
bc4_row = [r for r in rows_for(db, parent) if r[0] == bc4]
bc4_games = sum(bc4_row[0][1:]) if bc4_row else 0
check('regression: Bc4 continuation indexed', bc4_games > 0, f'{bc4_games} 2200+ games')

child_rows = rows_for(db, key_after('e2e4,c7c5,b1c3,g7g6,f1c4'))
child_total = sum(sum(r[1:]) for r in child_rows)
child_moves = [(r[0], sum(r[1:])) for r in child_rows if r[0] != 0]
check('regression: Bc4 child position has statistics', child_total > 0,
      f'{child_total} games, {len(child_moves)} continuations')
check('child total is not below its incoming edge', child_total >= bc4_games,
      f'{child_total} >= {bc4_games}')
check('no frequency cutoff: sub-100 continuations are stored',
      any(0 < n < 100 for _, n in child_moves),
      'continuations: ' + ', '.join(f'{complete_2200.decode_move(m).uci()}={n}' for m, n in sorted(child_moves, key=lambda x: -x[1])[:6]))

old = state_of(OLD)
if old:
    print()
    print('frequent-position index accepted games:', old.get('accepted'))
    delta = state['accepted'] - old['accepted']
    print('difference (new - old)                :', delta)
    check('accepted total matches the old index', state['accepted'] == old['accepted'],
          f'delta {delta} (review exclusions if nonzero)')

print()
for first, second in (('d2d4,g8f6,c2c4,e7e6,b1c3', 'c2c4,e7e6,b1c3,g8f6,d2d4'),
                      ('e2e4,e7e5,g1f3,b8c6,f1b5', 'g1f3,b8c6,e2e4,e7e5,f1b5')):
    ka, kb = key_after(first), key_after(second)
    check('transposition keys equal', ka == kb, f'{first} vs {second}')

db.close()

print()
rc = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'crosscheck_indexes.py'),
                     '--require-complete'], capture_output=True, text=True)
print(rc.stdout.rstrip())
if rc.stderr.strip():
    print(rc.stderr.rstrip())
check('matches the frequent-position index on every overlap position',
      rc.returncode == 0, 'see cross-check output above' if rc.returncode else '')

print()
if FAILURES:
    print(f'{len(FAILURES)} check(s) FAILED: ' + ', '.join(FAILURES))
    sys.exit(1)
print('all checks passed')
