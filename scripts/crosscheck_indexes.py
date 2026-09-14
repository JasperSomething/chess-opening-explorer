"""Cross-check the complete 2200+ index against the completed frequent-position index.

The frequent-position index (data/lumbra-2200.sqlite, threshold 100) counted the
both-2200+ subset exactly at every position it retained, keyed by canonical FEN.
The complete index (data/lumbra-2200-complete.sqlite) stores the same subset
keyed by the lossless 34-byte position key. Where both cover a position the
numbers must be identical.

While the complete index is still building, every comparison must satisfy
new <= old (partial coverage). new > old is a counting or identity bug and is
reported as a violation. Once the importer reports phase=complete, any
difference is a defect worth investigating.

Read-only; point lookups only, so it is safe to run while the importer works.
"""
import argparse
import struct
import sys
from pathlib import Path

import chess
import sqlite3

ROOT = Path(__file__).resolve().parent.parent
OLD = ROOT / 'data/lumbra-2200.sqlite'
NEW = ROOT / 'data/lumbra-2200-complete.sqlite'


def compact_key(board):
    rights = board.clean_castling_rights()
    flags = int(board.turn) << 4
    for i, sq in enumerate((chess.H1, chess.A1, chess.H8, chess.A8)):
        if rights & chess.BB_SQUARES[sq]:
            flags |= 1 << i
    return struct.pack('<4QBB', board.pawns | board.bishops | board.queens,
                       board.knights | board.bishops | board.kings,
                       board.rooks | board.queens | board.kings,
                       board.occupied_co[chess.WHITE], flags,
                       board.ep_square if board.has_legal_en_passant() else 64)


def encode(move):
    return move.from_square | (move.to_square << 6) | ((move.promotion or 0) << 12)


def load_old():
    """position fen -> (total2200, {uci: games2200})"""
    data = {}
    with sqlite3.connect('file:' + str(OLD) + '?mode=ro', uri=True) as db:
        for fen, uci, w, d, b in db.execute(
                'SELECT position, uci, white2200, draws2200, black2200 FROM counts'):
            total, moves = data.setdefault(fen, (0, {}))
            n = w + d + b
            if uci == '':
                data[fen] = (n, moves)
            else:
                moves[uci] = n
    return data


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--limit', type=int, help='only the first N positions (for a quick timing run)')
    ap.add_argument('--examples', type=int, default=5, help='violation examples to print')
    ap.add_argument('--require-complete', action='store_true',
                    help='also fail unless every overlap position matches the completed index exactly')
    args = ap.parse_args()

    old = load_old()
    print(f'positions in the frequent-position index: {len(old):,}')

    new_db = sqlite3.connect('file:' + str(NEW) + '?mode=ro', uri=True, timeout=30)
    new_db.execute('PRAGMA query_only=ON')

    exactly_equal = short = violation = unparsable = 0
    examples = []
    checked = 0
    items = sorted(old.items())
    for fen, (old_total, old_moves) in items:
        if args.limit and checked >= args.limit:
            break
        try:
            key = compact_key(chess.Board(fen))
        except Exception:
            unparsable += 1
            continue
        checked += 1
        rows = new_db.execute('SELECT move, white, draws, black FROM edges WHERE position=?',
                              (key,)).fetchall()
        new_total = sum(r[1] + r[2] + r[3] for r in rows)
        if new_total > old_total:
            violation += 1
            if len(examples) < args.examples:
                examples.append(('TOTAL', fen, old_total, new_total))
            continue
        new_moves = {}
        for move, w, d, b in rows:
            if move:
                from_sq, to_sq, promo = move & 63, (move >> 6) & 63, (move >> 12) or None
                new_moves[chess.Move(from_sq, to_sq, promotion=promo).uci()] = w + d + b
        bad = [(uci, n, old_moves.get(uci, 0)) for uci, n in new_moves.items() if n > old_moves.get(uci, 0)]
        if bad:
            violation += len(bad)
            if len(examples) < args.examples:
                examples.append(('MOVE', fen, bad[0][2], bad[0][1]))
            continue
        if new_total == old_total and new_moves == old_moves:
            exactly_equal += 1
        else:
            short += 1

    print(f'checked: {checked:,}   unparsable FENs: {unparsable}')
    print(f'  already identical to the completed index: {exactly_equal:,}')
    print(f'  partial (new < old, expected while building): {short:,}')
    print(f'  VIOLATIONS (new > old): {violation:,}')
    for kind, fen, oldn, newn in examples:
        print(f'    {kind}: {fen}  old={oldn} new={newn}')
    new_db.close()
    failed = violation > 0
    if args.require_complete and short:
        print(f'  FAIL: {short:,} overlap position(s) still differ from the completed index')
        failed = True
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
