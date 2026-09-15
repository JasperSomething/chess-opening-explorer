import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from study import curriculum, enrich_lichess, phase2_plan  # noqa: E402

FEN_A = 'rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -'
FEN_OUTSIDE = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -'


def raw_cache(rows):
    """rows: [(fen, [(uci, san, white, draws, black, source)])]"""
    path = Path(tempfile.mkdtemp()) / 'lichess.sqlite'
    db = sqlite3.connect(path)
    db.executescript('''
        CREATE TABLE positions (key TEXT PRIMARY KEY, depth INTEGER NOT NULL);
        CREATE TABLE moves (position TEXT, source TEXT, uci TEXT, san TEXT, target TEXT,
                            white INTEGER, draws INTEGER, black INTEGER, raw TEXT,
                            PRIMARY KEY (position, source, uci));
    ''')
    for fen, moves in rows:
        db.execute('INSERT INTO positions VALUES(?,?)', (fen + ' 0 1', 4))
        for uci, san, w, d, b, source in moves:
            db.execute('INSERT INTO moves VALUES(?,?,?,?,?,?,?,?,?)',
                       (fen + ' 0 1', source, uci, san, '', w, d, b, '{}'))
    db.commit()
    db.close()
    return path


def analysis_db(fens):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE position (position_key BLOB PRIMARY KEY, fen TEXT NOT NULL,
                               ply INTEGER NOT NULL DEFAULT 4, is_entry INTEGER DEFAULT 0);
        CREATE TABLE move_source (position_key BLOB, source TEXT, uci TEXT, san TEXT,
                                  games INTEGER, white INTEGER, draws INTEGER, black INTEGER,
                                  share REAL, PRIMARY KEY (position_key, source, uci));
        CREATE TABLE position_flow (position_key BLOB, source TEXT, reach_flow REAL,
                                    enter_mass REAL);
        CREATE TABLE eval (fen TEXT, multipv INTEGER, source TEXT, depth INTEGER, engine TEXT,
                           nodes INTEGER, time_ms INTEGER, pov TEXT, pvs_json TEXT,
                           meets_threshold INTEGER, fetched TEXT);
        CREATE TABLE metric (run_id INTEGER, position_key BLOB);
        CREATE TABLE deviation (run_id INTEGER, position_key BLOB);
        CREATE TABLE template_sample (structure_id TEXT, position_key BLOB, stratum REAL,
                                      weight REAL);
        CREATE TABLE structure_maturity (structure_id TEXT, source TEXT, boards INTEGER,
            entry_mass REAL, development_level REAL, dwell_mean REAL, retention_3 REAL);
        CREATE TABLE structure_stat (structure_id TEXT, source TEXT, run_id INTEGER,
                                     n_positions INTEGER);
    ''')
    for index, fen in enumerate(fens):
        db.execute('INSERT INTO position(position_key, fen, ply) VALUES(?,?,?)',
                   (bytes([index, index]), fen, 4))
        db.execute('INSERT INTO position_flow VALUES(?,?,?,?)',
                   (bytes([index, index]), 'local2200', 0.1 * (index + 1), 0.1 * (index + 1)))
    db.commit()
    return db


class NormaliseTests(unittest.TestCase):
    def test_counters_are_stripped_and_keys_agree(self):
        self.assertEqual(enrich_lichess.normalise(FEN_A + ' 0 1'),
                         enrich_lichess.normalise(FEN_A + ' 3 7'))

    def test_missing_fen_is_empty_not_an_exception(self):
        self.assertEqual(enrich_lichess.normalise(None), '')


class EnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.raw = raw_cache([
            (enrich_lichess.normalise(FEN_A), [('e4d5', 'exd5', 60, 30, 10, 'lichess'),
                                               ('g1f3', 'Nf3', 20, 10, 5, 'lichess')]),
            (enrich_lichess.normalise(FEN_OUTSIDE), [('e2e4', 'e4', 5, 5, 5, 'lichess')]),
        ])
        self.db = analysis_db([enrich_lichess.normalise(FEN_A)])

    def tearDown(self):
        self.db.close()

    def test_only_positions_inside_the_domain_are_written(self):
        result = enrich_lichess.enrich(self.db, self.raw, verbose=False)
        self.assertEqual(result['mapped_positions'], 1)
        self.assertEqual(result['unmappable'], 1)
        rows = self.db.execute('SELECT position_key, source, uci, games, white, draws, black, share '
                               'FROM move_source ORDER BY uci').fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row['source'], enrich_lichess.PROVENANCE)
        # original sample sizes and W/D/L carried across unchanged
        by_uci = {row['uci']: row for row in rows}
        self.assertEqual((by_uci['e4d5']['games'], by_uci['e4d5']['white'],
                          by_uci['e4d5']['draws'], by_uci['e4d5']['black']), (100, 60, 30, 10))
        self.assertEqual((by_uci['g1f3']['games'], by_uci['g1f3']['white'],
                          by_uci['g1f3']['draws'], by_uci['g1f3']['black']), (35, 20, 10, 5))
        total = sum(row['games'] for row in rows)
        self.assertAlmostEqual(sum(row['share'] for row in rows), 1.0, places=6)
        self.assertEqual(total, 135)      # 100 + 35

    def test_an_unmappable_position_is_recorded_never_zeroed(self):
        enrich_lichess.enrich(self.db, self.raw, verbose=False)
        audit = {row['outcome']: row for row in self.db.execute(
            'SELECT outcome, COUNT(*) n FROM lichess_enrichment_audit GROUP BY outcome')}
        self.assertEqual(audit['mapped']['n'], 1)
        self.assertEqual(audit['unmappable_no_domain_position']['n'], 1)
        zero_rows = self.db.execute(
            'SELECT COUNT(*) FROM move_source WHERE games = 0').fetchone()[0]
        self.assertEqual(zero_rows, 0)
        reasoning = self.db.execute('''SELECT reason FROM lichess_enrichment_audit
                                       WHERE outcome='unmappable_no_domain_position' ''').fetchone()
        self.assertIn('domain', reasoning['reason'])

    def test_enrichment_is_idempotent(self):
        enrich_lichess.enrich(self.db, self.raw, verbose=False)
        first = self.db.execute('SELECT COUNT(*) FROM move_source').fetchone()[0]
        enrich_lichess.enrich(self.db, self.raw, verbose=False)
        second = self.db.execute('SELECT COUNT(*) FROM move_source').fetchone()[0]
        self.assertEqual(first, second)


class PopulationLoaderTests(unittest.TestCase):
    def test_the_source_with_more_games_wins_and_sources_are_never_merged(self):
        db = analysis_db([enrich_lichess.normalise(FEN_A)])
        key = bytes([0, 0])
        db.executemany('INSERT INTO move_source VALUES(?,?,?,?,?,?,?,?,?)', [
            (key, 'lichess', 'e4d5', 'exd5', 40, 0, 0, 0, 0.4),
            (key, 'lichess', 'g1f3', 'Nf3', 60, 0, 0, 0, 0.6),
            (key, enrich_lichess.PROVENANCE, 'e4d5', 'exd5', 900, 0, 0, 0, 1.0),
        ])
        db.commit()
        source, dist, total = curriculum.load_population(db, key)
        self.assertEqual(source, enrich_lichess.PROVENANCE)
        self.assertEqual(total, 900)
        self.assertEqual(set(dist), {'e4d5'})     # not merged with the older source
        db.close()

    def test_absent_population_stays_distinguishable_from_zero(self):
        db = analysis_db([enrich_lichess.normalise(FEN_A)])
        source, dist, total = curriculum.load_population(db, bytes([0, 0]))
        self.assertIsNone(source)
        self.assertEqual(dist, {})
        self.assertEqual(total, 0)
        db.close()


class QueueTests(unittest.TestCase):
    def test_tiers_are_ordered_and_population_positions_are_excluded(self):
        db = analysis_db([enrich_lichess.normalise(FEN_A), 'fen-2', 'fen-3'])
        keys = [row[0] for row in db.execute('SELECT position_key FROM position')]
        # key 0 has population data already and must not be requested
        db.execute('INSERT INTO move_source VALUES(?,?,?,?,?,?,?,?,?)',
                   (keys[0], 'lichess', 'e4d5', 'exd5', 100, 0, 0, 0, 1.0))
        db.execute('INSERT INTO template_sample VALUES(?,?,?,?)', ('S', keys[1], 0.5, 1.0))
        db.execute('INSERT INTO eval VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   ('fen-3', 5, 'local_stockfish', 22, 'sf', 5_000_100, 1, 'side_to_move',
                    json.dumps([{'uci': 'e2e4', 'wdl': (1, 0, 0)}]), 1, 'now'))
        db.commit()
        queue, context = phase2_plan.build_queue(db, bars=())
        tiers = {row['tier'] for row in queue}
        self.assertNotIn(keys[0].hex(), [row['position_key'] for row in queue])
        self.assertIn(keys[1].hex(), [row['position_key'] for row in queue])
        self.assertIn('2_frozen_template_sample', tiers)
        self.assertIn('3_evaluated_high_flow_campaign', tiers)

    def test_plan_arithmetic_uses_the_spacing_rule(self):
        db = analysis_db(['fen-1', 'fen-2'])
        result, rows, context = phase2_plan.plan(db, bars=())
        self.assertEqual(result['requests'], len(rows))
        self.assertAlmostEqual(result['wall_seconds'],
                               result['requests'] * phase2_plan.REQUEST_SPACING_SECONDS)
        self.assertGreaterEqual(result['spacing_seconds'], 3.0)
        if result['total_domain_reach']:
            self.assertLessEqual(result['joint_share_now'], 1.0)
        db.close()


if __name__ == '__main__':
    unittest.main()
