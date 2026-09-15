import csv
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from study import acquire_population  # noqa: E402

FEN = 'rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -'
KEY = b'\x2c\xef\x00\x00\x10\x08\x00\xf7\x01'      # analysis-layer key bytes


def payload(*moves):
    entries = [{'uci': uci, 'white': 40, 'draws': 20, 'black': 5} for uci in moves]
    return {'white': 100, 'draws': 50, 'black': 25, 'moves': entries}


class FakeClient:
    """Counts requests and can be scripted to fail on chosen calls."""

    def __init__(self, respond=None, fail_on=()):
        self.calls = []
        self.fail_on = set(fail_on)
        self.respond = respond or (lambda key: payload('e4d5'))

    def get(self, source, board):
        key = board.fen()
        self.calls.append(key)
        if len(self.calls) in self.fail_on:
            raise RuntimeError('simulated network failure')
        return self.respond(key)


class AcquireTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.cache = self.dir / 'cache.sqlite'
        self.log = self.dir / 'log.csv'
        self.analysis = self.dir / 'analysis.sqlite'
        db = sqlite3.connect(self.analysis)
        db.executescript('CREATE TABLE position (position_key BLOB PRIMARY KEY, fen TEXT NOT NULL,'
                         ' ply INTEGER NOT NULL)')
        db.execute('INSERT INTO position VALUES(?,?,?)', (KEY, FEN, 4))
        db.commit()
        db.close()
        self.queue = [{'position_key': KEY.hex(), 'tier': '1_prescriptive_template_scope',
                       'reach': 0.5}]

    def test_the_delay_floor_is_enforced(self):
        with self.assertRaises(ValueError):
            acquire_population.acquire(self.queue, self.cache, 'token', delay=1.0,
                                       client=FakeClient(), analysis_path=self.analysis)

    def test_a_successful_response_is_stored_with_its_original_counts(self):
        client = FakeClient()
        totals = acquire_population.acquire(self.queue, self.cache, 'token', log_path=self.log,
                                            client=client, verbose=False,
                                            analysis_path=self.analysis)
        self.assertEqual(totals['success'], 1)
        db = sqlite3.connect(self.cache)
        snapshot = db.execute("SELECT white, draws, black FROM snapshots WHERE source='lichess'"
                              ).fetchone()
        self.assertEqual(snapshot, (100, 50, 25))
        moves = db.execute('SELECT uci, white, draws, black FROM moves ORDER BY uci').fetchall()
        self.assertEqual(moves, [('e4d5', 40, 20, 5)])
        positions = db.execute('SELECT COUNT(*) FROM positions').fetchone()[0]
        self.assertGreaterEqual(positions, 1)
        db.close()

    def test_a_cached_position_is_never_requested(self):
        # first run populates the cache, second run must not issue a single request
        acquire_population.acquire(self.queue, self.cache, 'token', client=FakeClient(),
                                   verbose=False, analysis_path=self.analysis)
        client = FakeClient()
        totals = acquire_population.acquire(self.queue, self.cache, 'token', client=client,
                                            verbose=False, analysis_path=self.analysis)
        self.assertEqual(client.calls, [])
        self.assertEqual(totals['skipped_cached'], 1)
        self.assertEqual(totals['attempted'], 0)

    def test_a_failure_is_counted_and_does_not_abort_the_run(self):
        second = dict(self.queue[0])                        # same position, distinct slot
        queue = [self.queue[0], dict(second, tier='2_frozen_template_sample')]
        # the first position fails; the run continues, and a later retry would still work
        client = FakeClient(fail_on=(1,))
        totals = acquire_population.acquire(queue, self.cache, 'token', log_path=self.log,
                                            client=client, verbose=False,
                                            analysis_path=self.analysis)
        self.assertEqual(totals['failed'], 1)
        self.assertEqual(totals['success'], 1)
        rows = list(csv.DictReader(open(self.log)))
        self.assertEqual([row['outcome'] for row in rows], ['failed', 'success'])
        self.assertIn('simulated network failure', rows[0]['error'])

    def test_resumability_after_a_failure(self):
        client = FakeClient(fail_on=(1,))
        acquire_population.acquire(self.queue, self.cache, 'token', client=client,
                                   verbose=False, analysis_path=self.analysis)
        # the failed position was not persisted, so a second run requests it again
        client2 = FakeClient()
        totals = acquire_population.acquire(self.queue, self.cache, 'token', client=client2,
                                            verbose=False, analysis_path=self.analysis)
        self.assertEqual(totals['attempted'], 1)
        self.assertEqual(totals['success'], 1)
        self.assertEqual(len(client2.calls), 1)

    def test_the_log_records_every_position_with_its_outcome(self):
        acquire_population.acquire(self.queue, self.cache, 'token', log_path=self.log,
                                   client=FakeClient(), verbose=False,
                                   analysis_path=self.analysis)
        acquire_population.acquire(self.queue, self.cache, 'token', log_path=self.log,
                                   client=FakeClient(), verbose=False,
                                   analysis_path=self.analysis)
        rows = list(csv.DictReader(open(self.log)))
        self.assertEqual(len(rows), 2)
        self.assertEqual([row['outcome'] for row in rows], ['success', 'skipped_cached'])

    def test_queue_loading_preserves_the_frozen_order_and_filters_tiers(self):
        path = self.dir / 'queue.csv'
        with open(path, 'w', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['request', 'tier', 'position_key', 'reach'])
            writer.writerow([1, '1_prescriptive_template_scope', 'aa', 0.1])
            writer.writerow([2, '2_frozen_template_sample', 'bb', 0.2])
            writer.writerow([3, '3_evaluated_high_flow_campaign', 'cc', 0.3])
            writer.writerow([4, '5_remaining_high_reach', 'dd', 0.4])
        rows = acquire_population.load_queue(path, tiers=acquire_population.TIERS[:2])
        self.assertEqual([row['position_key'] for row in rows], ['aa', 'bb'])
        limited = acquire_population.load_queue(path, limit=1)
        self.assertEqual([row['position_key'] for row in limited], ['aa'])

    def test_the_tail_is_not_in_the_approved_tiers(self):
        self.assertNotIn('5_remaining_high_reach', acquire_population.TIERS)
        self.assertEqual(len(acquire_population.TIERS), 3)


if __name__ == '__main__':
    unittest.main()
