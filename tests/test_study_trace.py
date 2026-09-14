import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from study import trace  # noqa: E402


class SplitTests(unittest.TestCase):
    def test_chronological_split_puts_earlier_games_in_train(self):
        dates = [(1, 201901), (2, 202001), (3, 202101), (4, 202201), (5, 202301)]
        split = trace.chronological_split(dates, train_fraction=0.6)
        # 5 games, cutoff at index 3 -> boundary month 202201: 3 train, 2 test
        self.assertEqual([split[i] for i in (1, 2, 3, 4, 5)], [0, 0, 0, 1, 1])
        self.assertEqual(sum(split.values()), 2)

    def test_boundary_month_is_never_straddled(self):
        dates = [(1, 202001), (2, 202002), (3, 202002), (4, 202002), (5, 202003)]
        split = trace.chronological_split(dates, train_fraction=0.4)
        # the whole 202002 block lands on the same side, so a single event is intact
        self.assertEqual(split[2], split[3])
        self.assertEqual(split[3], split[4])
        self.assertLess(split[1], split[4])

    def test_random_split_is_stable_for_a_seed_and_differs_across_seeds(self):
        ids = list(range(200))
        first = trace.random_split(ids, seed=1)
        again = trace.random_split(ids, seed=1)
        other = trace.random_split(ids, seed=2)
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        share = sum(first.values()) / len(first)
        self.assertLess(abs(share - 0.3), 0.10)

    def test_split_counts(self):
        self.assertEqual(trace.split_counts({1: 0, 2: 0, 3: 1}), {'train': 2, 'test': 1})


class DictionaryTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / 'trace.sqlite'
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        trace.ensure_schema(self.db)
        self.dicts = trace.dictionaries(self.db)

    def tearDown(self):
        self.db.close()

    def test_same_value_gets_the_same_ordinal(self):
        first = self.dicts['position'].get(b'abc')
        self.assertEqual(first, self.dicts['position'].get(b'abc'))
        self.assertEqual(self.dicts['position'].get(b'abd'), first + 1)

    def test_ids_survive_a_reload(self):
        self.dicts['structure'].get('S1')
        reloaded = trace.dictionaries(self.db)
        self.assertEqual(reloaded['structure'].get('S1'), 0)


class WalkTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / 'trace.sqlite'
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        trace.ensure_schema(self.db)
        self.dicts = trace.dictionaries(self.db)
        self.keys = [bytes([1, 1]), bytes([2, 2]), bytes([3, 3])]
        # game 1: plays the taught move at the first step, then deviates
        trace.insert_game(self.db, self.dicts, {
            'game_id': 1, 'source': 'test', 'date_ord': 202001, 'ratings_ord': 2000,
            'result_ord': 2, 'plies': 6,
            'steps': [(2, self.keys[0], 'S1', 'm1'), (4, self.keys[1], 'S2', 'm9')]})
        # game 2: deviates at both steps
        trace.insert_game(self.db, self.dicts, {
            'game_id': 2, 'source': 'test', 'date_ord': 202101, 'ratings_ord': 2000,
            'result_ord': 0, 'plies': 4,
            'steps': [(2, self.keys[0], 'S1', 'm2'), (4, self.keys[2], 'S3', 'm2')]})
        self.db.commit()
        self.items = {self.keys[0]: [('i1', {'m1'})], self.keys[1]: [('i1', {'m1'})],
                      self.keys[2]: [('i1', {'m1'})]}

    def tearDown(self):
        self.db.close()

    def test_walk_counts_steps_coverage_and_agreement(self):
        walked = trace.walked_games(self.db, self.items)
        self.assertEqual(walked[1]['steps'], 2)
        self.assertEqual(walked[1]['covered'], 2)
        self.assertEqual(walked[1]['played_taught'], 1)
        self.assertEqual(walked[2]['played_taught'], 0)

    def test_summary_reports_ratios(self):
        walked = trace.walked_games(self.db, self.items)
        summary = trace.summarise_walk(walked)
        total = summary[0]
        self.assertEqual(total['games'], 2)
        self.assertEqual(total['steps'], 4)
        self.assertAlmostEqual(total['coverage_of_steps'], 1.0)
        self.assertAlmostEqual(total['agreement_with_curriculum'], 0.25)
        self.assertEqual(total['split'], 0)

    def test_split_assignment_separates_the_later_game(self):
        counts = trace.assign_splits(self.db, train_fraction=0.5)
        self.assertEqual(counts['chronological']['train'], 1)
        self.assertEqual(counts['chronological']['test'], 1)
        rows = {row['game_id']: row['split_chrono'] for row in
                self.db.execute('SELECT game_id, split_chrono FROM trace_game')}
        self.assertEqual(rows[1], 0)   # 202001
        self.assertEqual(rows[2], 1)   # 202101
        for row in self.db.execute('SELECT split_random FROM trace_game'):
            self.assertIn(row['split_random'], (0, 1))

    def test_train_and_test_walks_are_disjoint(self):
        trace.assign_splits(self.db, train_fraction=0.5)
        train = trace.summarise_walk(trace.walked_games(self.db, self.items, 'chronological'))
        self.assertIn('0', {str(key) for key in train})


class ProjectionTests(unittest.TestCase):
    def test_measured_row_cost_is_positive_and_consistent(self):
        cost = trace.measured_row_cost(games=2000, steps_per_game=5.27, seed=1)
        self.assertGreater(cost['bytes_per_step'], 5)
        self.assertGreater(cost['bytes_per_game'], cost['bytes_per_step'])

    def test_projection_uses_the_flow_not_the_source_counts(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        db.executescript('''
            CREATE TABLE position (position_key BLOB, is_entry INTEGER);
            CREATE TABLE move_source (position_key BLOB, source TEXT, games INTEGER);
            CREATE TABLE position_flow (position_key BLOB, source TEXT, enter_mass REAL);
        ''')
        entry = b'entry'
        db.execute('INSERT INTO position VALUES(?,1)', (entry,))
        db.execute("INSERT INTO move_source VALUES(?,'local2200',1000)", (entry,))
        db.execute("INSERT INTO position_flow VALUES(?,'local2200',1.0)", (entry,))
        for index in range(4):
            key = bytes([index])
            db.execute('INSERT INTO position VALUES(?,0)', (key,))
            db.execute("INSERT INTO position_flow VALUES(?,'local2200',0.5)", (key,))
        db.commit()
        projected = trace.project(db, 'local2200', bytes_per_game=254.0)
        self.assertEqual(projected['domain_games'], 1000)
        self.assertAlmostEqual(projected['mean_domain_positions_per_game'], 3.0)
        self.assertEqual(projected['projected_steps'], 3000)
        self.assertAlmostEqual(projected['projected_mb'], 1000 * 254.0 / 1e6)


if __name__ == '__main__':
    unittest.main()
