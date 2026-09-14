import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from study import db as studydb  # noqa: E402
from study import flow  # noqa: E402


def key(letter):
    return letter.encode().ljust(34, b'\0')


class FlowTests(unittest.TestCase):
    """Synthetic graph: entry -> A,B -> C (two parents) plus an out-of-order transposition."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = studydb.connect(Path(self.tmp.name) / 'flow.sqlite')
        # positions: key, ply, is_entry
        rows = [('E', 2, 1), ('A', 3, 0), ('B', 3, 0), ('C', 4, 0), ('D', 5, 0), ('F', 6, 0)]
        for letter, ply, is_entry in rows:
            self.db.execute('''INSERT INTO position(position_key, fen, full_fen, domain, ply, min_ply,
                                role, is_entry, structure_id, white_pawns, black_pawns, pieces_json,
                                castling_rights, white_castled, black_castled, dev_white, dev_black,
                                dev_status, run_id)
                              VALUES(?,?,'',?,?,?,?,?,?,?,?,'{}','0','none','none',0,0,'x',1)''',
                            (key(letter), f'fen-{letter}', 'test', ply, ply, 'white_to_move',
                             is_entry, f's{letter}', '0', '0'))
        edges = [('E', 'A', 'a', 60), ('E', 'B', 'b', 40),
                 ('A', 'C', 'c', 30), ('A', 'OUTSIDE', 'x', 70),   # leaves the domain
                 ('B', 'C', 'd', 5), ('B', 'D', 'e', 35),
                 ('D', 'F', 'f', 100),
                 ('F', 'C', 'g', 10)]                              # transposition back to C
        for parent, child, uci, games in edges:
            self.db.execute('INSERT INTO provenance VALUES(?,?,?,?,?)',
                            (key(child), key(parent), uci, 'local2200', games))
        for letter, ratio in (('E', 1.0), ('A', 0.6), ('B', 0.4), ('C', 0.9), ('D', 0.35), ('F', 0.35)):
            self.db.execute('''INSERT INTO position_source(position_key, source, games, white, draws,
                                black, entry_games, reach_prob, coverage_state, retrieved)
                              VALUES(?,?,?,0,0,0,1000,?,'complete','now')''',
                            (key(letter), 'local2200', int(ratio * 1000), ratio))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def value(self, letter, column, source='local2200'):
        return self.db.execute(f'SELECT {column} FROM position_flow WHERE position_key=? AND source=?',
                               (key(letter), source)).fetchone()[0]

    def test_flow_distribution(self):
        flow.compute(self.db, 'local2200', verbose=False)
        self.assertAlmostEqual(self.value('E', 'reach_flow'), 1.0)
        self.assertAlmostEqual(self.value('A', 'reach_flow'), 0.6)
        self.assertAlmostEqual(self.value('B', 'reach_flow'), 0.4)
        # C is reached from A (0.6 * 0.3) and from B (0.4 * 5/40), and the late
        # transposition from F is not added to its per-game count.
        self.assertAlmostEqual(self.value('C', 'reach_flow'), 0.18 + 0.05)
        self.assertAlmostEqual(self.value('D', 'reach_flow'), 0.35)
        self.assertAlmostEqual(self.value('F', 'reach_flow'), 0.35)

    def test_leakage_and_late_arrivals_are_recorded_not_hidden(self):
        flow.compute(self.db, 'local2200', verbose=False)
        self.assertAlmostEqual(self.value('A', 'leakage'), 0.7)
        self.assertAlmostEqual(self.value('C', 'ignored_back_mass'), 0.35)
        self.assertIn('mass_leaves_domain', self.value('A', 'flags'))
        self.assertIn('late_transposition_arrivals', self.value('C', 'flags'))

    def test_count_ratio_disagreement_is_flagged(self):
        flow.compute(self.db, 'local2200', verbose=False)
        self.assertIn('fed_from_outside_family', self.value('C', 'flags'))
        self.assertNotIn('fed_from_outside_family', self.value('A', 'flags'))
        self.assertAlmostEqual(self.value('C', 'count_ratio'), 0.9)

    def test_path_counts_and_parent_counts(self):
        flow.compute(self.db, 'local2200', verbose=False)
        self.assertEqual(self.value('C', 'path_count'), 2)      # via A and via B
        self.assertEqual(self.value('C', 'n_parents_contributing'), 2)
        self.assertEqual(self.value('C', 'n_parents_total'), 3)  # includes the late parent F
        self.assertEqual(self.value('E', 'path_count'), 1)

    def test_missing_source_is_not_zero_flow(self):
        """A source with no move data must leave flow at 0 and say so."""
        flow.compute(self.db, 'masters', verbose=False)
        self.assertEqual(self.value('A', 'reach_flow', source='masters'), 0.0)
        self.assertIn('unreachable_in_flow', self.value('A', 'flags', source='masters'))


if __name__ == '__main__':
    unittest.main()
