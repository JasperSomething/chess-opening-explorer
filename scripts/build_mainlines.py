"""Build the opening-mainlines dataset the UI serves.

Three stages, all offline except the one-time taxonomy download:

  1. taxonomy  - the lichess-org/chess-openings TSVs (ECO code, name, PGN per line)
  2. mine      - replay every named line against the local 2200+ index and extend it with
                 the most-played continuation. One row per named opening.
  3. condense  - group those lines by their FINAL POSITION (so move orders that transpose
                 collapse), choose one representative per position, and find the
                 colour-complex counterpart of each.

Outputs analysis/mainlines-2200.tsv (stage 2) and analysis/mainlines.json (stage 3), which
is what study/mainlines_server.py serves.

The index used is data/lumbra-2200.sqlite: Lumbra OTB Complete 2026-07-08, both players
rated >= 2200, frequent-position build at threshold 100. Position identity is canonical
(placement, turn, castling, legally capturable en passant), so the corpus pools
transpositions by construction -- that is why grouping stage 3 by final position is the
right unit and why move-order-only distinctions cannot be recovered from this index.
"""
import argparse
import collections
import csv
import io
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parent.parent

TAXONOMY_BASE = 'https://raw.githubusercontent.com/lichess-org/chess-openings/master/{v}.tsv'
VOLUMES = 'abcde'

# Ordered most-preferred first: structure-defining names outrank move-order umbrellas.
# This is what puts the King's Indian position under the King's Indian and not the English.
PREFERENCE = [
    'Catalan Opening', "King's Indian Defense", 'Grünfeld Defense', 'Nimzo-Indian Defense',
    "Queen's Indian Defense", 'Bogo-Indian Defense', 'Budapest Defense', 'Benoni Defense',
    'Benko Gambit', 'Dutch Defense', "Queen's Gambit Declined", "Queen's Gambit Accepted",
    'Semi-Slav Defense', 'Slav Defense', 'Tarrasch Defense', 'Nimzo-Larsen Attack',
    'Trompowsky Attack', 'London System', 'Colle System', 'Torre Attack', 'Réti Opening',
    'Sicilian Defense', 'French Defense', 'Caro-Kann Defense', 'Ruy Lopez', 'Italian Game',
    'Scotch Game', "Petrov's Defense", 'Four Knights Game', "King's Gambit", 'Vienna Game',
    'Alekhine Defense', 'Pirc Defense', 'Modern Defense', 'Scandinavian Defense',
    'English Opening', 'Bird Opening', "King's Indian Attack", 'Owen Defense',
    'St. George Defense', 'Nimzowitsch Defense', 'Philidor Defense', 'Center Game',
    "Bishop's Opening", "King's Pawn Game", "Queen's Pawn Game", 'Zukertort Opening',
    'Indian Defense', 'East Indian Defense', 'Anglo-Indian Defense', 'Polish Opening',
]

TARGET_PLIES = 22     # taxonomy line, then most-played continuation, up to this depth
MIN_GAMES = 100       # never follow a move with fewer 2200+ games than this
DIVERGE_MIN = 50      # only report a divergence where the corpus's move has this many


def key(board):
    """The project's 4-field FEN: placement, turn, castling, legal en passant (no counters)."""
    return ' '.join(board.fen(en_passant='legal').split()[:4])


def mirror_key(fen4):
    """Colour-complex twin: swap piece colours and ranks, and the side to move, castling, ep file.

    Swapping colours flips the side to move, so a mirrored position can never occur at the
    same ply. Callers therefore match on board + castling alone and treat the turn as the
    tempo offset.
    """
    board, turn, castling, ep = fen4.split()
    ranks = [''.join(c.lower() if c.isupper() else c.upper() if c.islower() else c
                     for c in rank)
             for rank in reversed(board.split('/'))]
    cs = ''.join(c for c in 'KQkq' if (c.lower() if c.isupper() else c.upper()) in castling)
    ep2 = '-' if ep == '-' else ep[0] + str(9 - int(ep[1]))
    return f"{'/'.join(ranks)} {'b' if turn == 'w' else 'w'} {cs or '-'} {ep2}"


def base_of(name):
    return name.split(':')[0].strip()


# ------------------------------------------------------------------ taxonomy
def load_taxonomy(folder):
    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for volume in VOLUMES:
        path = folder / f'{volume}.tsv'
        if not path.exists():
            url = TAXONOMY_BASE.format(v=volume)
            print(f'  downloading {url}', flush=True)
            try:
                with urllib.request.urlopen(url, timeout=60) as response:
                    path.write_bytes(response.read())
            except Exception as error:
                sys.exit(f'could not fetch the taxonomy ({url}): {error}')
        with path.open() as handle:
            rows.extend(csv.DictReader(handle, delimiter='\t'))
    return [r for r in rows if r.get('pgn')]


# ---------------------------------------------------------------------- index
def load_index(db_path):
    """position -> total, and position -> {uci: games}, for the 2200+ frequent-position build."""
    connection = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    totals, moves = {}, collections.defaultdict(dict)
    for row in connection.execute(
            'SELECT position, uci, white2200, draws2200, black2200 FROM counts'):
        games = row['white2200'] + row['draws2200'] + row['black2200']
        if row['uci'] == '':
            totals[row['position']] = games
        else:
            moves[row['position']][row['uci']] = games
    connection.close()
    return totals, moves


# ----------------------------------------------------------------------- mine
def mine(taxonomy, totals, moves):
    """One row per named opening: its line, the corpus continuation, and where they first part."""
    rows = []
    for entry in taxonomy:
        game = chess.pgn.read_game(io.StringIO(entry['pgn']))
        if game is None:
            continue
        board = chess.Board()
        tax_ucis, tax_sans = [], []
        for move in game.mainline_moves():
            tax_ucis.append(move.uci())
            tax_sans.append(board.san(move))
            board.push(move)
        if not tax_ucis:
            continue
        # walk the taxonomy line, noting the first ply where the corpus prefers something else
        probe = chess.Board()
        diverge = None
        agree = 0
        for index, uci in enumerate(tax_ucis):
            fen = key(probe)
            candidates = moves.get(fen, {})
            played = candidates.get(uci, 0)
            if candidates:
                top_uci, top_games = max(candidates.items(), key=lambda kv: kv[1])
                if top_uci == uci:
                    agree += 1
                elif diverge is None and top_games >= DIVERGE_MIN:
                    top_san = probe.san(chess.Move.from_uci(top_uci))
                    diverge = dict(ply=index + 1, san=tax_sans[index], games=played,
                                   top_san=top_san, top_games=top_games)
            probe.push(chess.Move.from_uci(uci))
        # the taxonomy line's endpoint decides whether this named line was played at all,
        # so it must be read BEFORE the extension advances the board
        end = key(probe)
        # extend by the most-played continuation
        ext_sans, ext_games = [], []
        while len(tax_ucis) + len(ext_sans) < TARGET_PLIES:
            fen = key(probe)
            candidates = moves.get(fen, {})
            if not candidates:
                break
            uci, games = max(candidates.items(), key=lambda kv: kv[1])
            if games < MIN_GAMES:
                break
            san = probe.san(chess.Move.from_uci(uci))
            probe.push(chess.Move.from_uci(uci))
            ext_sans.append(san)
            ext_games.append(games)
        rows.append(dict(eco=entry['eco'], name=entry['name'], tax_pgn=entry['pgn'],
                         tax_sans=' '.join(tax_sans), tax_ucis=' '.join(tax_ucis),
                         plies=len(tax_ucis),
                         tax_end_total=totals.get(end, 0),
                         agree_plies=agree,
                         diverge_ply=(diverge or {}).get('ply', ''),
                         diverge_san=(diverge or {}).get('san', ''),
                         diverge_top_san=(diverge or {}).get('top_san', ''),
                         diverge_top_games=(diverge or {}).get('top_games', ''),
                         tax_move_games=(diverge or {}).get('games', ''),
                         extension=' '.join(ext_sans),
                         ext_games=' '.join(str(g) for g in ext_games)))
    return rows


# ------------------------------------------------------------------- condense
def build_lines(mined_rows, totals, moves):
    """Replay each row into per-ply records, FEN stored AFTER the move it belongs to."""
    openings = []
    for row in mined_rows:
        total = int(row['tax_end_total'] or 0)
        if total < 1:
            continue                      # named in the taxonomy, not played at 2200+
        board = chess.Board()
        line, ok = [], True
        tax_plies = len(row['tax_sans'].split())
        for index, token in enumerate(row['tax_sans'].split()):
            try:
                move = board.parse_san(token)
            except ValueError:
                ok = False
                break
            before = key(board)
            board.push(move)
            line.append(dict(ply=index + 1, san=token, uci=move.uci(), fen=key(board),
                             before=before, pos_games=totals.get(before, 0),
                             move_games=moves.get(before, {}).get(move.uci(), 0), tax=True))
        if not ok:
            continue
        while len(line) < TARGET_PLIES:
            before = key(board)
            candidates = moves.get(before, {})
            if not candidates:
                break
            uci, games = max(candidates.items(), key=lambda kv: kv[1])
            if games < MIN_GAMES:
                break
            move = chess.Move.from_uci(uci)
            san = board.san(move)
            board.push(move)
            line.append(dict(ply=len(line) + 1, san=san, uci=uci, fen=key(board),
                             before=before, pos_games=totals.get(before, 0),
                             move_games=games, tax=False))
        if not line:
            continue
        openings.append(dict(
            eco=row['eco'], name=row['name'], base=base_of(row['name']), games=total,
            tax_plies=tax_plies, n_plies=len(line), line=line,
            divergence=(dict(ply=int(row['diverge_ply']), san=row['diverge_san'],
                             top_san=row['diverge_top_san'],
                             top_games=int(row['diverge_top_games']),
                             move_games=int(row['tax_move_games']))
                        if row['diverge_ply'] else None)))
    return openings


def choose_representative(members):
    """Convention first, member count second. Returns (base name, reason)."""
    bases = {o['base'] for o in members}
    ranked = [b for b in PREFERENCE if b in bases]
    if ranked:
        return ranked[0], 'convention'
    count, games = collections.Counter(), collections.Counter()
    for o in members:
        count[o['base']] += 1
        games[o['base']] += o['games']
    return max(count, key=lambda b: (count[b], games[b])), 'majority'


def condense(openings, totals, moves, prefer_convention=True):
    by_position = collections.defaultdict(list)
    for o in openings:
        by_position[o['line'][-1]['fen']].append(o)
    groups = []
    for position, members in by_position.items():
        members.sort(key=lambda o: -o['tax_plies'])
        if prefer_convention:
            base, why = choose_representative(members)
        else:
            count = collections.Counter(o['base'] for o in members)
            base, why = max(count, key=lambda b: count[b]), 'majority'
        in_base = [o for o in members if o['base'] == base]
        rep = max(in_base, key=lambda o: (o['tax_plies'], o['games']))
        # distinct alias names, in order: several taxonomy lines can share one name, so the
        # alias list is shorter than the collapsed-line count whenever that happens
        aliases, seen = [], {rep['name']}
        for o in members:
            if o['name'] not in seen:
                seen.add(o['name'])
                aliases.append(o['name'])
        groups.append(dict(
            eco=rep['eco'], name=rep['name'], base=base, why=why,
            # every member ends on this same position by construction, so the entry's
            # volume is that position's reach -- summing the members' own endpoint counts
            # would add up unrelated positions
            games=totals.get(position, 0),
            ecos=sorted({o['eco'] for o in members}), n_collapsed=len(members),
            aliases=aliases,
            tax_plies=rep['tax_plies'], n_plies=rep['n_plies'], line=rep['line'],
            divergence=rep['divergence'], counterpart=None))
    for gid, group in enumerate(groups):
        group['id'] = gid

    # colour-complex counterparts, matched on board + castling (the turn is the tempo offset)
    fen_to_group = collections.defaultdict(list)
    for group in groups:
        for record in group['line']:
            fen_to_group[record['fen']].append(group['id'])
    by_board = collections.defaultdict(list)
    for fen in fen_to_group:
        fields = fen.split()
        by_board[(fields[0], fields[2])].append(fen)

    for group in groups:
        best = None
        for i in range(len(group['line']), 0, -1):
            mirrored = mirror_key(group['line'][i - 1]['fen'])
            fields = mirrored.split()
            scored = [(totals.get(fen, 0), other, fen)
                      for fen in by_board.get((fields[0], fields[2]), [])
                      for other in fen_to_group[fen] if other != group['id']]
            if scored:
                scored.sort(key=lambda item: -item[0])
                games, other, fen = scored[0]
                ply = next(k for k, rec in enumerate(groups[other]['line'], 1) if rec['fen'] == fen)
                best = dict(ply=i, group=other, games=games, mirror_fen=mirrored,
                            counterpart_ply=ply, san_here=group['line'][i - 1]['san'])
                break
        group['counterpart'] = best

    # keep only mutual pairs: a shallow board coincidence otherwise pairs unrelated openings
    for group in groups:
        candidate = group['counterpart']
        if not candidate:
            continue
        back = groups[candidate['group']]['counterpart']
        if back and back['group'] == group['id']:
            candidate['mutual'] = True
            candidate['same_base'] = groups[candidate['group']]['base'] == group['base']
        else:
            group['counterpart'] = None
    return groups


def families_of(groups):
    buckets = collections.defaultdict(list)
    for group in groups:
        buckets[group['base']].append(group['id'])
    families = []
    for name, ids in buckets.items():
        ids.sort(key=lambda i: -groups[i]['games'])
        families.append(dict(name=name, games=sum(groups[i]['games'] for i in ids),
                             n=len(ids), eco=min(groups[i]['eco'] for i in ids), groups=ids))
    families.sort(key=lambda f: f['eco'])
    for ids in buckets.values():
        groups[ids[0]]['is_family_main'] = True
    for group in groups:
        group.setdefault('is_family_main', False)
    return families


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', type=Path, default=ROOT / 'data/lumbra-2200.sqlite')
    parser.add_argument('--taxonomy-dir', type=Path, default=ROOT / 'data/taxonomy')
    parser.add_argument('--tsv', type=Path, default=ROOT / 'analysis/mainlines-2200.tsv')
    parser.add_argument('--out', type=Path, default=ROOT / 'analysis/mainlines.json')
    options = parser.parse_args()

    if not options.db.exists():
        sys.exit(f'missing {options.db}: build the 2200+ frequent-position index first '
                 f'(see README)')
    options.tsv.parent.mkdir(parents=True, exist_ok=True)
    options.out.parent.mkdir(parents=True, exist_ok=True)

    print('taxonomy...', flush=True)
    taxonomy = load_taxonomy(options.taxonomy_dir)
    print(f'  {len(taxonomy)} named lines', flush=True)

    print('index...', flush=True)
    totals, moves = load_index(options.db)
    print(f'  {len(totals)} positions, {sum(len(v) for v in moves.values())} moves', flush=True)

    print('mining...', flush=True)
    mined = mine(taxonomy, totals, moves)
    with options.tsv.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(mined[0].keys()), delimiter='\t')
        writer.writeheader()
        writer.writerows(mined)
    print(f'  {len(mined)} rows -> {options.tsv}', flush=True)

    print('condensing...', flush=True)
    openings = build_lines(mined, totals, moves)
    groups = condense(openings, totals, moves)
    families = families_of(groups)
    with_count = sum(1 for g in groups if g['counterpart'])
    print(f'  {len(openings)} lines -> {len(groups)} positions, {len(families)} families, '
          f'{with_count} with a colour twin', flush=True)

    options.out.write_text(json.dumps(dict(
        meta=dict(generated=time.strftime('%Y-%m-%d'),
                  source='Lumbra OTB Complete 2026-07-08',
                  filter='both players >= 2200', games=2823189, positions=46044,
                  taxonomy='lichess-org/chess-openings',
                  rule=f'most-played continuation, min {MIN_GAMES} games/node to ply {TARGET_PLIES}',
                  condensed=f'{len(openings)} taxonomy lines collapsed to {len(groups)} positions'),
        families=families, groups=groups), separators=(',', ':')))
    print(f'  wrote {options.out} ({options.out.stat().st_size / 1e6:.1f} MB)', flush=True)


if __name__ == '__main__':
    main()
