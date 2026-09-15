"""Lichess population enrichment from the local cache, and the audit that explains it.

Read-only against `data/lumbra-lichess.sqlite` (the raw local cache) and against the
analysis database's existing rows. Everything written goes to the analysis database
only, under a provenance label that says exactly where it came from.

Why the audit exists: the raw cache holds move rows for 5,214 positions while the
analysis layer exposes only 179 move distributions. The audit separates the
candidate explanations rather than assuming one:

* key / FEN representation mismatch,
* domain filtering (positions outside the walked Scandinavian domain),
* minimum-game thresholds (rows deliberately withheld below the source floor),
* stale / incomplete import (positions that should have arrived and did not),
* different position universes (the cache crawled a wider or narrower set).

Mapping policy: a position that cannot be mapped is recorded with its reason and is
never written as a zero-population row, because "no data" and "zero games" are
different claims.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import db as studydb  # noqa: E402

RAW_CACHE = ROOT / 'data' / 'lumbra-lichess.sqlite'
PROVENANCE = 'existing_local_lichess_cache'
LEGACY_SOURCES = ('lichess',)

SCHEMA = """
CREATE TABLE IF NOT EXISTS lichess_enrichment_audit (
    position_key    BLOB,                -- NULL when the position is not in the domain
    fen             TEXT NOT NULL,
    outcome         TEXT NOT NULL,       -- mapped | unmappable_no_domain_position | ...
    reason          TEXT NOT NULL,
    raw_moves       INTEGER,
    raw_games       INTEGER,
    source_label    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lichess_audit_outcome
    ON lichess_enrichment_audit(outcome);
"""


def normalise(fen):
    """4-field canonical key: the comparison both databases agree on."""
    return ' '.join((fen or '').split(' ')[:4])


def audit(raw_path=RAW_CACHE, db=None, verbose=True):
    """Explain the 5,214 vs 179 discrepancy with numbers, not a guess."""
    import sqlite3
    raw = sqlite3.connect('file:' + str(Path(raw_path).resolve()) + '?mode=ro', uri=True)
    raw.row_factory = sqlite3.Row
    report = {}

    raw_keys = {}
    for row in raw.execute('SELECT key, depth FROM positions'):
        raw_keys[normalise(row['key'])] = row['depth']
    raw_moves = {}
    for row in raw.execute('''SELECT position, COUNT(*) moves, SUM(white+draws+black) games,
                                     SUM(source='lichess') lichess_moves,
                                     SUM(source='masters') masters_moves
                              FROM moves GROUP BY position'''):
        raw_moves[normalise(row['position'])] = {'moves': row['moves'], 'games': row['games'] or 0,
                                                 'lichess_moves': row['lichess_moves'],
                                                 'masters_moves': row['masters_moves']}
    report['raw_positions'] = len(raw_keys)
    report['raw_positions_with_moves'] = len(raw_moves)
    report['raw_move_rows'] = raw.execute('SELECT COUNT(*) FROM moves').fetchone()[0]
    report['raw_key_is_fen_like'] = sum(
        1 for key in raw_keys if key.count(' ') == 3 and '/' in key) / max(len(raw_keys), 1)

    domain_fens = {row['fen'] for row in db.execute('SELECT fen FROM position')}
    report['domain_positions'] = len(domain_fens)
    mapped = sorted(set(raw_keys).intersection(domain_fens))
    unmapped = sorted(set(raw_keys).difference(domain_fens))
    report['candidate_mapped'] = len(mapped)
    report['candidate_unmapped'] = len(unmapped)

    # who already has move distributions in the analysis layer, per source?
    exposed = {}
    for row in db.execute('''SELECT ms.position_key, ms.source, COUNT(*) rows,
                                    SUM(ms.games) games
                             FROM move_source ms GROUP BY ms.position_key, ms.source'''):
        exposed.setdefault(bytes(row['position_key']), {})[row['source']] = (
            row['rows'], row['games'])
    fen_of = {row[0]: row[1] for row in db.execute('SELECT position_key, fen FROM position')}
    fen_to_key = {fen: key for key, fen in fen_of.items()}
    exposed_fens = {fen_of[key] for key in exposed if key in fen_of}
    lichess_fens = {fen_of[key] for key, sources in exposed.items()
                    if key in fen_of and any(name in LEGACY_SOURCES for name in sources)}
    report['analysis_positions_with_moves'] = len(exposed_fens)
    report['analysis_positions_with_lichess_source'] = len(lichess_fens)

    # the explanations, measured separately, against the lichess source specifically
    games_floor_probe = 50
    cross = {'below_floor_imported': 0, 'below_floor_missing': 0,
             'above_floor_imported': 0, 'above_floor_missing': 0,
             'masters_only_imported': 0, 'masters_only_missing': 0}
    for fen in mapped:
        info = raw_moves.get(fen, {})
        games = info.get('games', 0)
        has_lichess_moves = info.get('lichess_moves', 0) > 0
        imported = fen in lichess_fens
        if not has_lichess_moves:
            cross['masters_only_imported' if imported else 'masters_only_missing'] += 1
        elif games < games_floor_probe:
            cross['below_floor_imported' if imported else 'below_floor_missing'] += 1
        else:
            cross['above_floor_imported' if imported else 'above_floor_missing'] += 1
    report['mapped_cross_tab'] = cross
    stale = [fen for fen in mapped if fen not in lichess_fens]
    report['mapped_but_not_imported'] = len(stale)
    report['unmapped_not_in_domain'] = len(unmapped)
    games_floor = 50                       # study/domain.py's lichess minimum
    report['mapped_below_game_floor'] = sum(
        1 for fen in mapped if raw_moves.get(fen, {}).get('games', 0) < games_floor)
    report['mapped_at_or_above_game_floor'] = sum(
        1 for fen in mapped if raw_moves.get(fen, {}).get('games', 0) >= games_floor)
    report['mapped_with_masters_only_moves'] = sum(
        1 for fen in mapped if raw_moves.get(fen, {}).get('lichess_moves', 0) == 0)
    report['imported_positions'] = sum(1 for fen in exposed_fens)
    report['imported_that_are_not_mapped'] = len(exposed_fens.difference(set(mapped)))
    report['explanations'] = {
        'key_or_fen_mismatch': report['raw_key_is_fen_like'] < 0.99,
        'domain_filtering': len(unmapped),
        'minimum_game_threshold_withheld': report['mapped_below_game_floor'],
        'stale_or_incomplete_import': len(stale) - report['mapped_below_game_floor'],
    }
    if verbose:
        print(json.dumps(report, indent=1))
    raw.close()
    return report


def map_positions(db, raw_path=RAW_CACHE):
    """Every raw-cache position mapped into the domain, with the reason when it is not."""
    import sqlite3
    raw = sqlite3.connect('file:' + str(Path(raw_path).resolve()) + '?mode=ro', uri=True)
    raw.row_factory = sqlite3.Row
    key_of_fen = {row['fen']: row['position_key'] for row in
                  db.execute('SELECT fen, position_key FROM position')}
    rows = []
    for row in raw.execute('''SELECT p.key fen, COUNT(m.uci) moves,
                                     SUM(m.white+m.draws+m.black) games,
                                     SUM(m.source='lichess') lichess_moves
                              FROM positions p LEFT JOIN moves m ON m.position = p.key
                              GROUP BY p.key'''):
        fen = normalise(row['fen'])
        key = key_of_fen.get(fen)
        rows.append({
            'fen': fen, 'position_key': key,
            'raw_moves': row['moves'] or 0, 'raw_games': row['games'] or 0,
            'lichess_moves': row['lichess_moves'] or 0,
            'outcome': 'mapped' if key is not None else 'unmappable_no_domain_position',
            'reason': '' if key is not None else
                      ('fen not among the 29,876 analysed domain positions: the raw cache '
                       'crawled a wider position universe than the domain walk retains'),
        })
    raw.close()
    return rows


def enrich(db, raw_path=RAW_CACHE, min_games=1, verbose=True):
    """Write the locally cached Lichess distributions into the analysis layer.

    Deterministic and idempotent: rows are keyed (position_key, source, uci) and
    replaced, never appended. Provenance is `existing_local_lichess_cache`; original
    sample sizes and W/D/L per move are carried across unchanged. Positions that
    cannot be mapped are recorded in the audit table with their reason and are never
    written as zero.
    """
    import sqlite3
    db.executescript(SCHEMA)
    raw = sqlite3.connect('file:' + str(Path(raw_path).resolve()) + '?mode=ro', uri=True)
    raw.row_factory = sqlite3.Row
    key_of_fen = {row['fen']: row['position_key'] for row in
                  db.execute('SELECT fen, position_key FROM position')}

    audit_rows = []
    written = skipped_floor = skipped_unmapped = 0
    for position in raw.execute('SELECT key FROM positions'):
        fen = normalise(position['key'])
        key = key_of_fen.get(fen)
        moves = list(raw.execute('''SELECT uci, san, white, draws, black, source
                                    FROM moves WHERE position=? ORDER BY uci''',
                                 (position['key'],)))
        total = sum((row['white'] or 0) + (row['draws'] or 0) + (row['black'] or 0)
                    for row in moves)
        if key is None:
            skipped_unmapped += 1
            audit_rows.append((None, fen, 'unmappable_no_domain_position',
                               'fen outside the analysed domain', len(moves), total, PROVENANCE))
            continue
        if total < min_games:
            skipped_floor += 1
            audit_rows.append((key, fen, 'skipped_below_game_floor',
                               f'raw cache has {total} games, floor is {min_games}',
                               len(moves), total, PROVENANCE))
            continue
        for row in moves:
            games = (row['white'] or 0) + (row['draws'] or 0) + (row['black'] or 0)
            if games <= 0:
                continue
            db.execute('''INSERT OR REPLACE INTO move_source(
                    position_key, source, uci, san, games, white, draws, black, share)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                       (key, PROVENANCE, row['uci'], row['san'], games, row['white'],
                        row['draws'], row['black'], games / total))
        written += 1
        audit_rows.append((key, fen, 'mapped', 'imported with provenance',
                           len(moves), total, PROVENANCE))
    for row in audit_rows:
        db.execute('''INSERT INTO lichess_enrichment_audit(
                position_key, fen, outcome, reason, raw_moves, raw_games, source_label)
            VALUES(?,?,?,?,?,?,?)''', row)
    db.commit()
    raw.close()
    result = {'mapped_positions': written, 'unmappable': skipped_unmapped,
              'below_floor': skipped_floor, 'audit_rows': len(audit_rows),
              'provenance': PROVENANCE}
    if verbose:
        print(json.dumps(result, indent=1))
    return result


def overlap_report(db, bars=(0.30, 0.40, 0.50, 0.60), verbose=True):
    """The joint-coverage numbers, raw counts and reach-weighted, per rule bar."""
    from study import curriculum, rules
    reach = curriculum.load_reach(db)
    domain = [row[0] for row in db.execute('SELECT position_key FROM position')]
    population_sources = {row[0] for row in db.execute(
        'SELECT DISTINCT source FROM move_source')}
    # ordinary-play sources only: expert sources are not a population distribution
    placeholders = ','.join('?' * len(curriculum.POPULATION_PREFERENCE))
    pop_fens = {row[0] for row in db.execute(
        f'''SELECT p.fen FROM position p WHERE EXISTS (
              SELECT 1 FROM move_source ms WHERE ms.position_key = p.position_key
                AND ms.source IN ({placeholders}))''', curriculum.POPULATION_PREFERENCE)}
    engine_fens = {row[0] for row in db.execute(
        "SELECT DISTINCT fen FROM eval WHERE source='local_stockfish' AND nodes > 1000000")}
    report = {
        'analysis_domain_positions': len(domain),
        'population_sources_present': sorted(population_sources),
        'positions_with_population': len(pop_fens),
        'positions_with_engine_evaluation': len(engine_fens),
        'positions_with_both': len(pop_fens.intersection(engine_fens)),
        'reach_total': sum(reach.get(key, {}).get('reach', 0.0) for key in domain),
    }
    pop_keys = {key for key in domain
                if db.execute(f'''SELECT 1 FROM move_source WHERE position_key=?
                                  AND source IN ({placeholders}) LIMIT 1''',
                              (key, *curriculum.POPULATION_PREFERENCE)).fetchone()}
    eng_keys = {key for key in domain
                if db.execute('''SELECT 1 FROM eval e JOIN position p ON p.fen = e.fen
                                 WHERE p.position_key=? AND e.source='local_stockfish'
                                   AND e.nodes > 1000000 LIMIT 1''', (key,)).fetchone()}
    both = pop_keys.intersection(eng_keys)
    report['population_positions_in_domain'] = len(pop_keys)
    report['population_reach'] = sum(reach.get(key, {}).get('reach', 0.0) for key in pop_keys)
    report['joint_reach'] = sum(reach.get(key, {}).get('reach', 0.0) for key in both)
    report['joint_reach_share'] = (report['joint_reach'] / report['reach_total']
                                   if report['reach_total'] else None)
    report['coverage'] = {}
    campaign_keys = {row[0] for row in db.execute('SELECT position_key FROM evaluation_candidate')}
    report['campaign_positions_with_both'] = len(campaign_keys.intersection(both))
    report['campaign_positions'] = len(campaign_keys)
    samples = {row[0] for row in db.execute('SELECT DISTINCT position_key FROM template_sample')}
    report['template_sample_boards'] = len(samples)
    report['template_sample_boards_with_both'] = len(samples.intersection(both))
    report['template_sample_boards_with_engine'] = len(samples.intersection(eng_keys))
    report['per_bar'] = {}
    for bar in bars:
        derived = rules.derive_all(db, settings={'support_floor': bar})
        items = rules.as_items(db, derived)
        prescriptive = [item for item in items if item.get('kind') == 'prescriptive']
        scope = set()
        for item in prescriptive:
            scope.update(item['keys'])
        evaluated = rules.estimate_rule_ev(db, derived)
        with_value = [entry for entry in evaluated.values()
                      if entry.get('ev_per_board') is not None]
        report['per_bar'][f'{bar:.2f}'] = {
            'prescriptive_rules': len(prescriptive),
            'scope_boards': len(scope),
            'scope_boards_with_engine': len(scope.intersection(eng_keys)),
            'scope_boards_with_population': len(scope.intersection(pop_keys)),
            'scope_boards_with_both': len(scope.intersection(both)),
            'scope_reach': sum(reach.get(key, {}).get('reach', 0.0) for key in scope),
            'scope_reach_with_both': sum(reach.get(key, {}).get('reach', 0.0)
                                         for key in scope.intersection(both)),
            'rules_with_ev': len(with_value),
        }
    if verbose:
        print(json.dumps(report, indent=1, default=str))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=studydb.DEFAULT_ANALYSIS_DB)
    parser.add_argument('--raw', type=Path, default=RAW_CACHE)
    parser.add_argument('--audit', action='store_true')
    parser.add_argument('--enrich', action='store_true')
    parser.add_argument('--overlap', action='store_true')
    parser.add_argument('--min-games', type=int, default=1)
    args = parser.parse_args()
    db = studydb.connect(args.db, create=args.enrich)
    if args.audit or not (args.enrich or args.overlap):
        audit(args.raw, db)
    if args.enrich:
        enrich(db, args.raw, args.min_games)
    if args.overlap:
        overlap_report(db)
    db.close()


if __name__ == '__main__':
    main()
