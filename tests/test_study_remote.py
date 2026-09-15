"""Tests for the server-free campaign runner (study/remote.py).

Offline: no engine is ever started, no network, no shelling out. Every test
injects a fake `analyse` callable, which is the only reason the scheduler,
ledger, leases and importer can be exercised without Stockfish.
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from study import campaign, remote  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

FEN_A = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -'
FEN_B = 'rnbqkbnr/pppppppp/8/3q4/8/2N5/PPPP1PPP/R1BQKBNR b KQkq -'
FEN_C = 'rnb1kbnr/ppp1pppp/8/3q4/8/2N5/PPPP1PPP/R1BQKBNR b KQkq -'

EVAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS eval (
    fen TEXT NOT NULL, multipv INTEGER NOT NULL, source TEXT NOT NULL, depth INTEGER NOT NULL,
    engine TEXT NOT NULL, nodes INTEGER, time_ms INTEGER, pov TEXT NOT NULL, pvs_json TEXT NOT NULL,
    meets_threshold INTEGER NOT NULL, fetched TEXT NOT NULL,
    PRIMARY KEY (fen, multipv, source, depth)
);
"""

CANDIDATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluation_candidate (
    position_key BLOB PRIMARY KEY, fen TEXT NOT NULL, tier TEXT NOT NULL, score REAL NOT NULL,
    selected INTEGER NOT NULL, campaign TEXT NOT NULL
);
"""


def fake_result(nodes=1_000_000, depth=20, time_ms=100):
    return {'source': 'local_stockfish', 'engine': 'fake-stockfish', 'depth': depth, 'nodes': nodes,
            'time_ms': time_ms, 'pov': 'side_to_move',
            'pvs': [{'uci': 'e2e4', 'line': ['e2e4'], 'cp': 20, 'mate': None, 'wdl': [500, 400, 100]}]}


def insert_candidate(db, key_byte, fen, tier, score=1.0, selected=1, campaign_name='phase1'):
    db.execute('INSERT INTO evaluation_candidate VALUES(?,?,?,?,?,?)',
               (bytes([key_byte, key_byte * 2]), fen, tier, score, selected, campaign_name))


def build_db(candidates=True):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript(EVAL_SCHEMA)
    db.executescript(CANDIDATE_SCHEMA)
    remote.ensure_schema(db)
    if candidates:
        insert_candidate(db, 1, FEN_A, 'A', score=9.0)
        insert_candidate(db, 2, FEN_B, 'B', score=7.0)
        insert_candidate(db, 3, FEN_C, 'C', score=5.0)
        insert_candidate(db, 4, FEN_A, 'A', score=1.0, selected=0)          # not selected
        insert_candidate(db, 5, FEN_B, 'A', score=1.0, campaign_name='old')  # other campaign
        db.commit()
    return db


class PlanTests(unittest.TestCase):
    def test_planning_twice_inserts_nothing_the_second_time(self):
        db = build_db()
        first = remote.plan_jobs(db, engine_version='sf-test-1')
        second = remote.plan_jobs(db, engine_version='sf-test-1')
        self.assertEqual(first['candidates'], 3)          # selected rows of this campaign only
        self.assertEqual(first['inserted'], 3)
        self.assertEqual(first['skipped'], 0)
        self.assertEqual(second['inserted'], 0)
        self.assertEqual(second['skipped'], 3)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM engine_job').fetchone()[0], 3)

    def test_node_budgets_come_from_the_campaign_tiers(self):
        db = build_db()
        remote.plan_jobs(db, engine_version='sf-test-1')
        budgets = {row['tier']: row['nodes_budget']
                   for row in db.execute('SELECT tier, nodes_budget FROM engine_job')}
        self.assertEqual(budgets, campaign.NODE_TIERS)
        self.assertEqual({row['multipv'] for row in db.execute('SELECT multipv FROM engine_job')},
                         {campaign.MULTIPV})

    def test_a_done_job_is_never_replanned(self):
        db = build_db()
        remote.plan_jobs(db, engine_version='sf-test-1')
        job_id = db.execute("SELECT job_id FROM engine_job WHERE tier='C'").fetchone()[0]
        db.execute('''UPDATE engine_job SET state='done', depth_achieved=41, nodes_actual=?,
                      result_json='{}' WHERE job_id=?''', (campaign.NODE_TIERS['C'], job_id))
        db.commit()
        again = remote.plan_jobs(db, engine_version='sf-test-1')
        self.assertEqual(again['inserted'], 0)
        self.assertEqual(again['skipped'], 3)
        self.assertEqual(db.execute('SELECT state FROM engine_job WHERE job_id=?',
                                    (job_id,)).fetchone()[0], 'done')

    def test_same_fen_at_a_different_node_budget_is_a_distinct_job(self):
        db = build_db(candidates=False)
        insert_candidate(db, 1, FEN_A, 'A')
        insert_candidate(db, 2, FEN_A, 'C')               # same fen, deeper tier
        db.commit()
        report = remote.plan_jobs(db, engine_version='sf-test-1')
        rows = list(db.execute('SELECT job_id, nodes_budget FROM engine_job ORDER BY nodes_budget'))
        self.assertEqual(report['inserted'], 2)
        self.assertEqual([row['nodes_budget'] for row in rows],
                         [campaign.NODE_TIERS['A'], campaign.NODE_TIERS['C']])
        self.assertNotEqual(rows[0]['job_id'], rows[1]['job_id'])

    def test_unknown_tier_is_reported_not_planned(self):
        db = build_db(candidates=False)
        insert_candidate(db, 1, FEN_A, 'Z')
        db.commit()
        report = remote.plan_jobs(db, engine_version='sf-test-1')
        self.assertEqual(report['inserted'], 0)
        self.assertEqual(report['untiered'], 1)

    def test_missing_engine_version_is_refused(self):
        db = build_db()
        with self.assertRaises(ValueError):
            remote.plan_jobs(db)


class JobKeyTests(unittest.TestCase):
    def test_key_changes_with_the_node_budget_and_not_with_depth(self):
        base = remote.job_id_for(FEN_A, campaign.MULTIPV, campaign.NODE_TIERS['A'], 'sf-1')
        deeper = remote.job_id_for(FEN_A, campaign.MULTIPV, campaign.NODE_TIERS['B'], 'sf-1')
        self.assertNotEqual(base, deeper)
        # the achieved depth is metadata: it is not an input, so no result at any
        # depth can rename the job it belongs to
        self.assertEqual(base, remote.job_id_for(FEN_A, campaign.MULTIPV,
                                                 campaign.NODE_TIERS['A'], 'sf-1'))
        self.assertNotEqual(base, remote.job_id_for(FEN_A, campaign.MULTIPV + 1,
                                                    campaign.NODE_TIERS['A'], 'sf-1'))
        self.assertNotEqual(base, remote.job_id_for(FEN_A, campaign.MULTIPV,
                                                    campaign.NODE_TIERS['A'], 'sf-2'))

    def test_the_key_is_stable_sha1_of_the_declared_tuple(self):
        import hashlib
        expected = hashlib.sha1(
            f'{FEN_A}|{campaign.MULTIPV}|{campaign.NODE_TIERS["A"]}|sf-1'.encode()).hexdigest()
        self.assertEqual(remote.job_id_for(FEN_A, campaign.MULTIPV, campaign.NODE_TIERS['A'],
                                           'sf-1'), expected)


class ClaimTests(unittest.TestCase):
    def test_empty_queue_returns_none(self):
        db = build_db(candidates=False)
        self.assertIsNone(remote.claim_next(db, 'w1'))

    def test_two_workers_never_get_the_same_job(self):
        db = build_db()
        remote.plan_jobs(db, engine_version='sf-test-1')
        first = remote.claim_next(db, 'w1')
        second = remote.claim_next(db, 'w2')
        third = remote.claim_next(db, 'w3')
        claimed = [job['job_id'] for job in (first, second, third)]
        self.assertEqual(len(set(claimed)), 3)
        self.assertIsNone(remote.claim_next(db, 'w4'))

    def test_a_live_lease_is_not_handed_out_again(self):
        db = build_db()
        remote.plan_jobs(db, engine_version='sf-test-1')
        now = time.time()
        first = remote.claim_next(db, 'w1', lease_seconds=900, now=now)
        reassigned = remote.claim_next(db, 'w2', lease_seconds=900, now=now + 10)
        self.assertNotEqual(first['job_id'], reassigned['job_id'])
        self.assertEqual(remote.stats(db)['leased'], 2)

    def test_expired_leases_return_to_pending_and_can_be_claimed_again(self):
        db = build_db()
        remote.plan_jobs(db, engine_version='sf-test-1')
        now = time.time()
        job = remote.claim_next(db, 'dead-worker', lease_seconds=900, now=now)
        recovered = remote.release_expired(db, now=now + 901, lease_seconds=900)
        self.assertEqual(recovered, 1)
        self.assertEqual(remote.stats(db)['pending'], 3)
        self.assertEqual(remote.stats(db)['leased'], 0)
        again = remote.claim_next(db, 'w2', lease_seconds=900, now=now + 901)
        self.assertEqual(again['job_id'], job['job_id'])
        self.assertEqual(db.execute('SELECT attempts FROM engine_job WHERE job_id=?',
                                    (job['job_id'],)).fetchone()[0], 2)

    def test_claim_next_takes_over_an_expired_lease_without_release(self):
        db = build_db()
        remote.plan_jobs(db, engine_version='sf-test-1')
        now = time.time()
        job = remote.claim_next(db, 'dead-worker', lease_seconds=60, now=now)
        refused = remote.claim_next(db, 'w2', lease_seconds=60, now=now + 30)
        self.assertNotEqual(refused['job_id'], job['job_id'])
        taken = remote.claim_next(db, 'w2', lease_seconds=60, now=now + 61)
        self.assertEqual(taken['job_id'], job['job_id'])


class ImportTests(unittest.TestCase):
    def records(self, db):
        out = []
        for row in db.execute('SELECT job_id, multipv, nodes_budget, fen FROM engine_job ORDER BY tier'):
            out.append({'job_id': row['job_id'], 'depth_achieved': 34, 'nodes_actual': 5_000_123,
                        'time_ms': 9012, 'result': fake_result(nodes=5_000_123, depth=34,
                                                               time_ms=9012)})
        return out

    def test_import_is_idempotent(self):
        db = build_db()
        remote.plan_jobs(db, engine_version='sf-test-1')
        records = self.records(db)
        path = Path(tempfile.mkdtemp()) / 'results.jsonl'
        with open(path, 'w', encoding='utf-8') as handle:
            for record in records:
                handle.write(json.dumps(record) + '\n')
        first = remote.import_results(db, path)
        second = remote.import_results(db, path)
        self.assertEqual(first['imported'], 3)
        self.assertEqual(first['already_done'], 0)
        self.assertEqual(second['imported'], 0)
        self.assertEqual(second['already_done'], 3)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM eval').fetchone()[0], 3)
        self.assertEqual(remote.stats(db)['done'], 3)
        self.assertEqual(remote.stats(db)['nodes_done'], 3 * 5_000_123)

    def test_unknown_and_malformed_lines_are_counted(self):
        db = build_db()
        remote.plan_jobs(db, engine_version='sf-test-1')
        path = Path(tempfile.mkdtemp()) / 'results.jsonl'
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(json.dumps({'job_id': 'not-a-job', 'result': fake_result()}) + '\n')
            handle.write('{not json}\n')
        report = remote.import_results(db, path)
        self.assertEqual(report['unknown'], 1)
        self.assertEqual(report['malformed'], 1)
        self.assertEqual(report['imported'], 0)

    def test_export_writes_only_pending_jobs_and_import_round_trips(self):
        db = build_db()
        remote.plan_jobs(db, engine_version='sf-test-1')
        remote.claim_next(db, 'w1')                       # one job is leased, not pending
        path = Path(tempfile.mkdtemp()) / 'queue.jsonl'
        report = remote.export_jobs(db, path)
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(report['exported'], 2)
        self.assertEqual(len(lines), 2)
        self.assertEqual({line['nodes_budget'] for line in lines},
                         {campaign.NODE_TIERS['B'], campaign.NODE_TIERS['C']})
        self.assertTrue(all(len(bytes.fromhex(line['position_key'])) == 2 for line in lines))
        self.assertTrue(all(line['multipv'] == campaign.MULTIPV for line in lines))
        # the exported queue is importable elsewhere: the same job ids are stable
        other = build_db()
        remote.plan_jobs(other, engine_version='sf-test-1')
        for line in lines:
            self.assertIsNotNone(other.execute('SELECT 1 FROM engine_job WHERE job_id=?',
                                               (line['job_id'],)).fetchone())


class StatsTests(unittest.TestCase):
    def test_stats_reports_nodes_done_and_remaining(self):
        db = build_db()
        remote.plan_jobs(db, engine_version='sf-test-1')
        job = remote.claim_next(db, 'w1')
        db.execute("UPDATE engine_job SET state='done', nodes_actual=? WHERE job_id=?",
                   (job['nodes_budget'] + 7, job['job_id']))
        db.commit()
        report = remote.stats(db)
        self.assertEqual(report['done'], 1)
        self.assertEqual(report['pending'], 2)
        self.assertEqual(report['nodes_done'], campaign.NODE_TIERS['A'] + 7)
        self.assertEqual(report['nodes_remaining'],
                         campaign.NODE_TIERS['B'] + campaign.NODE_TIERS['C'])


class BenchmarkTests(unittest.TestCase):
    def make_fake(self, sleep=0.01, nodes=1000, depth=18):
        calls = []
        lock = threading.Lock()

        def analyse(fen, nodes_budget, multipv):
            time.sleep(sleep)
            with lock:
                calls.append((fen, nodes_budget, multipv))
            return fake_result(nodes=nodes, depth=depth, time_ms=int(sleep * 1000))

        analyse.calls = calls
        return analyse

    def test_aggregate_nps_is_total_nodes_over_wall_and_results_are_sorted(self):
        fake = self.make_fake(sleep=0.01, nodes=1000)
        fens = [FEN_A, FEN_B, FEN_C, FEN_A]
        report = remote.benchmark_topologies('unused-binary',
                                             [{'workers': 1, 'threads_per_worker': 2},
                                              {'workers': 2, 'threads_per_worker': 1}],
                                             fens, nodes_per=1_000_000, repeat=2,
                                             analyse_factory=lambda topology: fake)
        entries = report['topologies']
        self.assertEqual(len(entries), 2)
        self.assertEqual([e['aggregate_nps'] for e in entries],
                         sorted([e['aggregate_nps'] for e in entries], reverse=True))
        for entry in entries:
            self.assertEqual(entry['total_nodes'], 8 * 1000)          # 4 fens x repeat 2 x 1000
            self.assertEqual(entry['positions'], 8)
            self.assertEqual(entry['mean_depth_achieved'], 18)
            self.assertAlmostEqual(entry['aggregate_nps'],
                                   entry['total_nodes'] / entry['wall_seconds'])
            # 8 sleeps of 10 ms across `workers` threads: the wall time must be
            # in the neighbourhood of the ideal, never better than it
            ideal = (8 / entry['workers']) * 0.01
            self.assertGreaterEqual(entry['wall_seconds'], ideal * 0.9)
            self.assertLessEqual(entry['wall_seconds'], ideal * 3.5)
        self.assertEqual(entries[0]['workers'], 2)                    # 2x1 beats 1x2
        self.assertEqual(report['best'], entries[0]['label'])
        self.assertEqual(report['best'], '2x1')
        self.assertEqual(report['cores'], os.cpu_count())
        self.assertIn('cores detected', report['note'])

    def test_every_evaluation_uses_the_declared_budget_and_multipv(self):
        fake = self.make_fake()
        report = remote.benchmark_topologies('unused-binary', [{'workers': 1, 'threads_per_worker': 1}],
                                             [FEN_A, FEN_B], nodes_per=777_000,
                                             analyse_factory=lambda topology: fake)
        self.assertEqual(len(fake.calls), 2)
        self.assertTrue(all(call[1] == 777_000 for call in fake.calls))
        self.assertTrue(all(call[2] == campaign.MULTIPV for call in fake.calls))
        self.assertEqual(report['nodes_per'], 777_000)
        self.assertEqual(report['multipv'], campaign.MULTIPV)
        self.assertEqual(report['topologies'][0]['total_nodes'], 2 * 1000)

    def test_repeat_must_be_positive(self):
        with self.assertRaises(ValueError):
            remote.benchmark_topologies('binary', [{'workers': 1, 'threads_per_worker': 1}],
                                        [FEN_A], repeat=0)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.db_path = self.directory / 'worker.sqlite'
        db = sqlite3.connect(self.db_path)
        db.executescript(EVAL_SCHEMA)
        db.executescript(CANDIDATE_SCHEMA)
        db.row_factory = sqlite3.Row
        remote.ensure_schema(db)
        insert_candidate(db, 1, FEN_A, 'A')
        insert_candidate(db, 2, FEN_B, 'B')
        insert_candidate(db, 3, FEN_C, 'C')
        db.commit()
        db.close()
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def test_worker_drains_the_queue_is_resumable_and_feeds_the_eval_cache(self):
        remote.plan_jobs(self.conn, engine_version='sf-test-1')
        seen = []

        def analyse(fen, nodes_budget, multipv):
            seen.append((fen, nodes_budget, multipv))
            return fake_result(nodes=nodes_budget, depth=30, time_ms=5)

        results_path = self.directory / 'results.jsonl'
        totals = remote.run_worker(self.db_path, 'worker-a', 'unused-binary', workers=2,
                                   analyse=analyse, results_path=results_path)
        self.assertEqual(totals['claimed'], 3)
        self.assertEqual(totals['done'], 3)
        self.assertEqual(totals['failed'], 0)
        self.assertEqual(len(seen), 3)
        self.assertEqual(sorted(call[1] for call in seen), sorted(campaign.NODE_TIERS.values()))
        self.assertTrue(all(call[2] == campaign.MULTIPV for call in seen))
        report = remote.stats(self.conn)
        self.assertEqual(report['done'], 3)
        self.assertEqual(report['pending'], 0)
        self.assertEqual(report['leased'], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM eval').fetchone()[0], 3)
        # the worker's own JSONL output re-imports as already-covered work
        lines = results_path.read_text().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(json.loads(line)['result']['depth'] == 30 for line in lines))
        self.assertEqual(remote.import_results(self.conn, results_path),
                         {'imported': 0, 'already_done': 3, 'unknown': 0, 'malformed': 0,
                          'eval_stored': 0})
        # resumable: nothing left to claim on a second run
        second = remote.run_worker(self.db_path, 'worker-a', 'unused-binary', workers=2,
                                   analyse=analyse)
        self.assertEqual(second['claimed'], 0)
        self.assertEqual(second['done'], 0)

    def test_max_jobs_stops_the_run_early_and_leaves_the_rest_pending(self):
        remote.plan_jobs(self.conn, engine_version='sf-test-1')
        totals = remote.run_worker(self.db_path, 'worker-a', 'unused-binary', workers=1,
                                   max_jobs=1, analyse=lambda fen, nodes, multipv: fake_result())
        self.assertEqual(totals['done'], 1)
        self.assertEqual(remote.stats(self.conn)['pending'], 2)

    def test_a_failing_analysis_is_requeued_then_marked_failed(self):
        remote.plan_jobs(self.conn, engine_version='sf-test-1')

        def broken(fen, nodes_budget, multipv):
            raise RuntimeError('engine died')

        first = remote.run_worker(self.db_path, 'worker-a', 'unused-binary', workers=1,
                                  max_attempts=1, max_jobs=1, analyse=broken)
        self.assertEqual(first['failed'], 1)
        self.assertEqual(first['requeued'], 0)
        self.assertEqual(remote.stats(self.conn)['failed'], 1)
        self.assertEqual(remote.stats(self.conn)['pending'], 2)

        # the terminal job is not claimable, so the next run takes a fresh one and
        # its failure goes back to pending because attempts remain
        retried = remote.run_worker(self.db_path, 'worker-b', 'unused-binary', workers=1,
                                    max_attempts=3, max_jobs=1, analyse=broken)
        self.assertEqual(retried['requeued'], 1)
        self.assertEqual(retried['failed'], 0)
        self.assertEqual(remote.stats(self.conn)['pending'], 2)
        self.assertEqual(remote.stats(self.conn)['failed'], 1)

        final = remote.run_worker(self.db_path, 'worker-c', 'unused-binary', workers=1,
                                  max_attempts=2, max_jobs=1, analyse=broken)
        self.assertEqual(final['failed'], 1)          # attempts exhausted: terminal
        self.assertEqual(remote.stats(self.conn)['failed'], 2)
        self.assertEqual(remote.stats(self.conn)['pending'], 1)


class ConcurrentClaimTests(unittest.TestCase):
    """The lease has to hold between processes, so it is tested concurrently."""

    def test_two_threads_claiming_at_once_never_get_the_same_job(self):
        directory = Path(tempfile.mkdtemp())
        path = directory / 'race.sqlite'
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        db.executescript(CANDIDATE_SCHEMA)
        remote.ensure_schema(db)
        for index in range(1, 13):
            insert_candidate(db, index, f'{FEN_A} {index}', 'A')
        db.commit()
        remote.plan_jobs(db, engine_version='sf-test-1')
        db.close()

        claimed = []
        claimed_lock = threading.Lock()
        start = threading.Barrier(4)

        def worker(index):
            connection = sqlite3.connect(path, timeout=60)
            connection.row_factory = sqlite3.Row
            start.wait()
            while True:
                job = remote.claim_next(connection, f'w{index}', lease_seconds=900)
                if job is None:
                    break
                with claimed_lock:
                    claimed.append(job['job_id'])
            connection.close()

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(claimed), 12)
        self.assertEqual(len(set(claimed)), 12)          # no double lease, no lost job


class SchemaTests(unittest.TestCase):
    def test_the_only_table_created_is_the_job_ledger(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        remote.ensure_schema(db)
        tables = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        self.assertEqual(tables, ['engine_job'])
        columns = {row[1] for row in db.execute('PRAGMA table_info(engine_job)')}
        self.assertEqual(columns, {'job_id', 'fen', 'multipv', 'nodes_budget', 'engine_version',
                                   'state', 'worker', 'leased_at', 'attempts', 'position_key',
                                   'tier', 'campaign', 'depth_achieved', 'nodes_actual', 'time_ms',
                                   'result_json', 'finished_at'})

    def test_the_module_never_touches_an_ingestion_database(self):
        from study import db as studydb
        self.assertEqual(studydb.DEFAULT_ANALYSIS_DB, ROOT / 'data' / 'atlas-analysis.sqlite')
        source = Path(remote.__file__).read_text()
        self.assertNotIn('lumbra', source)
        self.assertIn('connect(db_path, create=False)', source)


if __name__ == '__main__':
    unittest.main()
