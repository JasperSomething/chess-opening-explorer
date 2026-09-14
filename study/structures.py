"""Structural fingerprints for a board, and pure helpers over pawn bitboards.

Everything here is deterministic and computable from a single position, so the
compression baseline (how many exact positions vs how many exact pawn structures)
never depends on clustering or machine learning.

Definitions used (all documented because they are the basis of study claims):

pawn structure     the exact (white, black) pawn placement. Colour aware, other
                   piece placement ignored. This is the primary structural state.
centre configuration  bitmask: white pawns on c4/d4/e4/f4 and black pawns on
                   c5/d5/e5/f5 (the four files around the centre, by rank).
open file          no pawn of either colour on the file.
semi-open file     no pawn of one colour on the file, at least one of the other.
pawn island        a maximal run of adjacent files containing at least one pawn
                   of that colour.
isolated pawn      no friendly pawn on either adjacent file.
doubled pawns      extra pawns on a file beyond the first.
passed pawn        no enemy pawn on the same or adjacent files ahead of it.
backward pawn      CONSERVATIVE and marked approximate: the pawn's advance square
                   is attacked by an enemy pawn, and no friendly pawn on an
                   adjacent file is on the same rank or behind it, so it cannot
                   be supported by a pawn. Pawns in that shape are frequently
                   called backward, but the definition is not exact.
development count  pieces other than pawns/king off their home squares, plus 1 if
                   the king is no longer on its home square (castling counts).
"""
import hashlib
import struct

import chess

HOME_SQUARES = {
    chess.WHITE: {
        chess.KNIGHT: (chess.B1, chess.G1),
        chess.BISHOP: (chess.C1, chess.F1),
        chess.ROOK: (chess.A1, chess.H1),
        chess.QUEEN: (chess.D1,),
        chess.KING: (chess.E1,),
    },
    chess.BLACK: {
        chess.KNIGHT: (chess.B8, chess.G8),
        chess.BISHOP: (chess.C8, chess.F8),
        chess.ROOK: (chess.A8, chess.H8),
        chess.QUEEN: (chess.D8,),
        chess.KING: (chess.E8,),
    },
}

CENTRE_FILES = {'c': 2, 'd': 3, 'e': 4, 'f': 5}


def pawn_masks(board):
    return (board.pawns & board.occupied_co[chess.WHITE],
            board.pawns & board.occupied_co[chess.BLACK])


def file_of(square):
    return chess.square_file(square)


def pawns_per_file(pawn_bb):
    out = [0] * 8
    bb = pawn_bb
    while bb:
        sq = (bb & -bb).bit_length() - 1
        out[file_of(sq)] += 1
        bb &= bb - 1
    return out


def islands(pawn_bb):
    counts = pawns_per_file(pawn_bb)
    runs = 0
    prev = False
    for n in counts:
        cur = n > 0
        if cur and not prev:
            runs += 1
        prev = cur
    return runs


def isolated(pawn_bb):
    counts = pawns_per_file(pawn_bb)
    result = 0
    bb = pawn_bb
    while bb:
        sq = (bb & -bb).bit_length() - 1
        f = file_of(sq)
        neighbours = (counts[f - 1] if f > 0 else 0) + (counts[f + 1] if f < 7 else 0)
        if neighbours == 0:
            result += 1
        bb &= bb - 1
    return result


def doubled(pawn_bb):
    return sum(max(0, n - 1) for n in pawns_per_file(pawn_bb))


def passed(pawn_bb, enemy_pawn_bb, colour):
    """Pawns with no enemy pawn on the same or adjacent files ahead of them."""
    result = 0
    bb = pawn_bb
    while bb:
        sq = (bb & -bb).bit_length() - 1
        bb &= bb - 1
        f, r = file_of(sq), chess.square_rank(sq)
        blocked = False
        for df in (-1, 0, 1):
            nf = f + df
            if not 0 <= nf <= 7:
                continue
            for nr in range(r + 1, 8) if colour == chess.WHITE else range(0, r):
                if enemy_pawn_bb & chess.BB_SQUARES[chess.square(nf, nr)]:
                    blocked = True
                    break
            if blocked:
                break
        if not blocked:
            result += 1
    return result


def backward(pawn_bb, enemy_pawn_bb, colour):
    """Conservative backward-pawn count (see module docstring: approximate)."""
    result = 0
    bb = pawn_bb
    while bb:
        sq = (bb & -bb).bit_length() - 1
        bb &= bb - 1
        f, r = file_of(sq), chess.square_rank(sq)
        step = 1 if colour == chess.WHITE else -1
        adv_rank = r + step
        if not 0 <= adv_rank <= 7:
            continue
        adv_square = chess.square(f, adv_rank)
        # Squares from which an enemy pawn attacks adv_square. By symmetry these
        # are the squares a pawn of the same colour standing on adv_square attacks.
        if not (chess.BB_PAWN_ATTACKS[colour][adv_square] & enemy_pawn_bb):
            continue  # advance square is not covered by an enemy pawn
        supported = False
        for df in (-1, 1):
            nf = f + df
            if not 0 <= nf <= 7:
                continue
            for nr in range(0, 8):
                if not (pawn_bb & chess.BB_SQUARES[chess.square(nf, nr)]):
                    continue
                behind_or_level = nr <= r if colour == chess.WHITE else nr >= r
                if behind_or_level:
                    supported = True
                    break
            if supported:
                break
        if not supported:
            result += 1
    return result


def centre_config(white_pawns, black_pawns):
    mask = 0
    for i, f in enumerate(sorted(CENTRE_FILES.values())):
        if white_pawns & chess.BB_FILES[f] & chess.BB_RANKS[3]:  # rank 4
            mask |= 1 << i
        if black_pawns & chess.BB_FILES[f] & chess.BB_RANKS[4]:  # rank 5
            mask |= 1 << (i + 4)
    return mask


def centre_config_text(mask):
    white = [name for i, name in enumerate(('c4', 'd4', 'e4', 'f4')) if mask >> i & 1]
    black = [name for i, name in enumerate(('c5', 'd5', 'e5', 'f5')) if mask >> (i + 4) & 1]
    return '/'.join(filter(None, (','.join(white), ','.join(black)))) or 'no central pawns'


def open_files(white_pawns, black_pawns):
    both = white_pawns | black_pawns
    return sum(1 for f in range(8) if not (both & chess.BB_FILES[f]))


def open_file_mask(white_pawns, black_pawns):
    mask = 0
    for f in range(8):
        if not ((white_pawns | black_pawns) & chess.BB_FILES[f]):
            mask |= 1 << f
    return mask


def semi_open_masks(white_pawns, black_pawns):
    white, black = 0, 0
    for f in range(8):
        w = bool(white_pawns & chess.BB_FILES[f])
        b = bool(black_pawns & chess.BB_FILES[f])
        if not w and b:
            white |= 1 << f
        if not b and w:
            black |= 1 << f
    return white, black


def material_text(board):
    parts = []
    for colour, letter in ((chess.WHITE, 'W'), (chess.BLACK, 'B')):
        counts = ''.join(f'{name}{len(board.pieces(piece, colour))}'
                         for piece, name in ((chess.QUEEN, 'Q'), (chess.ROOK, 'R'),
                                             (chess.BISHOP, 'B'), (chess.KNIGHT, 'N'),
                                             (chess.PAWN, 'P')))
        parts.append(f'{letter}:{counts}')
    return ' '.join(parts)


def castling_state(board):
    """Castled state first, then whether castling is still available.

    A king on the castled square is reported as castled. A king that simply
    walked to g1/c1/g8/c8 without castling is indistinguishable from a castled
    king by position alone; that ambiguity is documented rather than hidden.
    """
    def side(colour):
        king = board.king(colour)
        home = chess.E1 if colour == chess.WHITE else chess.E8
        kingside_sq = chess.G1 if colour == chess.WHITE else chess.G8
        queenside_sq = chess.C1 if colour == chess.WHITE else chess.C8
        if king is not None and king == kingside_sq:
            return 'castled_kingside'
        if king is not None and king == queenside_sq:
            return 'castled_queenside'
        k = board.has_kingside_castling_rights(colour)
        q = board.has_queenside_castling_rights(colour)
        if k and q:
            return 'available_both'
        if k:
            return 'available_kingside'
        if q:
            return 'available_queenside'
        if king is None or king == home:
            return 'none'
        return 'king_moved'
    return side(chess.WHITE), side(chess.BLACK), board.castling_rights


def development_count(board, colour):
    """Pieces off their home squares; a castled king and its castled rook count once each.

    The rook that castling moved is not counted as a developing move, so a
    castled side reads as king + two minor/major pieces rather than three.
    """
    total = 0
    king = board.king(colour)
    home_king = chess.E1 if colour == chess.WHITE else chess.E8
    kingside_sq = chess.G1 if colour == chess.WHITE else chess.G8
    queenside_sq = chess.C1 if colour == chess.WHITE else chess.C8
    castled = king is not None and king in (kingside_sq, queenside_sq)
    castled_rook = None
    if castled:
        castled_rook = ((chess.F1 if king == kingside_sq else chess.D1) if colour == chess.WHITE
                        else (chess.F8 if king == kingside_sq else chess.D8))
    for piece, homes in HOME_SQUARES[colour].items():
        if piece == chess.KING:
            continue
        for sq in board.pieces(piece, colour):
            if castled_rook is not None and piece == chess.ROOK and sq == castled_rook:
                continue
            if sq not in homes:
                total += 1
    if king is not None and king != home_king:
        total += 1
    return total


def development_status(dev_white, dev_black):
    total = dev_white + dev_black
    if total <= 3:
        return 'undeveloped'
    if total <= 7:
        return 'partial'
    return 'developed'


def structure_id(white_pawns, black_pawns):
    return hashlib.blake2b(struct.pack('<2Q', white_pawns, black_pawns), digest_size=8).hexdigest()


def fingerprint(board):
    """All structural fields for a board, ready for the analysis database."""
    wp, bp = pawn_masks(board)
    sm_w, sm_b = semi_open_masks(wp, bp)
    dev_w, dev_b = development_count(board, chess.WHITE), development_count(board, chess.BLACK)
    return {
        'structure_id': structure_id(wp, bp),
        'white_pawns': wp,
        'black_pawns': bp,
        'centre_config': centre_config(wp, bp),
        'open_files': open_files(wp, bp),
        'open_file_mask': open_file_mask(wp, bp),
        'semi_open_white': sm_w,
        'semi_open_black': sm_b,
        'islands_white': islands(wp),
        'islands_black': islands(bp),
        'isolated_white': isolated(wp),
        'isolated_black': isolated(bp),
        'doubled_white': doubled(wp),
        'doubled_black': doubled(bp),
        'backward_white': backward(wp, bp, chess.WHITE),
        'backward_black': backward(bp, wp, chess.BLACK),
        'passed_white': passed(wp, bp, chess.WHITE),
        'passed_black': passed(bp, wp, chess.BLACK),
        'material': material_text(board),
        'castling_rights': board.castling_rights,
        'white_castled': castling_state(board)[0],
        'black_castled': castling_state(board)[1],
        'dev_white': dev_w,
        'dev_black': dev_b,
        'dev_status': development_status(dev_w, dev_b),
        'pieces_json': _pieces_json(board),
    }


def _pieces_json(board):
    import json
    out = {}
    for colour, cname in ((chess.WHITE, 'w'), (chess.BLACK, 'b')):
        for piece, pname in ((chess.PAWN, 'p'), (chess.KNIGHT, 'n'), (chess.BISHOP, 'b'),
                             (chess.ROOK, 'r'), (chess.QUEEN, 'q'), (chess.KING, 'k')):
            out[cname + pname] = int(board.pieces(piece, colour))
    return json.dumps(out)


def render_pawns(white_pawns, black_pawns):
    """ASCII pawn diagram, for human inspection of a structure."""
    lines = []
    for rank in range(7, -1, -1):
        row = []
        for file in range(8):
            sq = chess.square(file, rank)
            bit = chess.BB_SQUARES[sq]
            row.append('P' if white_pawns & bit else 'p' if black_pawns & bit else '.')
        lines.append(f'{rank + 1} ' + ' '.join(row))
    lines.append('  a b c d e f g h')
    return '\n'.join(lines)
