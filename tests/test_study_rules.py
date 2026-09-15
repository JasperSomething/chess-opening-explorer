import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from study import rules  # noqa: E402


def fake_db(boards):
    """boards: [(key, role, {uci: share}, {uci: games})]"""
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE position (position_key BLOB, structure_id TEXT, role TEXT, fen TEXT);
        CREATE TABLE position_flow (position_key BLOB, source TEXT, reach_flow REAL,
                                    enter_mass REAL);
        CREATE TABLE move_source (position_key BLOB, source TEXT, uci TEXT, san TEXT, games INTEGER,
                                  white INTEGER, draws INTEGER, black INTEGER, share REAL);
    ''')
    for index, (key, role, dist, games) in enumerate(boards):
        db.execute('INSERT INTO position VALUES(?,?,?,?)', (key, 'S1', role, f'fen{index}'))
        db.execute('INSERT INTO position_flow VALUES(?,?,?,?)',
                   (key, 'local2200', 1.0 / len(boards), 1.0 / len(boards)))
        for uci, share in dist.items():
            db.execute('INSERT INTO move_source VALUES(?,?,?,?,?,?,?,?,?)',
                       (key, 'local2200', uci, uci, games.get(uci, 100), 0, 0, 0, share))
    db.commit()
    return db


class BoardAnswerTests(unittest.TestCase):
    def test_a_dominant_move_becomes_the_answer(self):
        answers, top = rules.board_answers({'m1': 0.9, 'm2': 0.1})
        self.assertEqual(answers, ['m1'])
        self.assertEqual(top, 'm1')

    def test_a_contested_board_has_no_answer(self):
        answers, top = rules.board_answers({'m1': 0.3, 'm2': 0.3, 'm3': 0.3})
        self.assertEqual(answers, [])
        self.assertIn(top, ('m1', 'm2', 'm3'))

    def test_thin_evidence_is_filtered_by_the_game_floor(self):
        answers, _top = rules.board_answers({'m1': 0.6, 'm2': 0.4}, games={'m1': 5, 'm2': 100})
        self.assertEqual(answers, ['m2'])


class DerivationTests(unittest.TestCase):
    def test_a_family_with_one_consistent_answer_is_prescriptive(self):
        db = fake_db([(b'1', 'white_to_move', {'m1': 0.8, 'm2': 0.2}, {}),
                      (b'2', 'white_to_move', {'m1': 0.7, 'm2': 0.3}, {})])
        rule = rules.derive_rule(db, 'S1', 'local2200', {'feature': 'role',
                                                         'value': 'white_to_move'},
                                 None, None, role='white_to_move')
        self.assertEqual(rule['kind'], 'prescriptive')
        self.assertEqual(rule['answers'], ['m1'])
        self.assertAlmostEqual(rule['support'], 1.0)
        self.assertEqual(rule['condition']['role'], 'white_to_move')

    def test_a_family_with_split_answers_is_recognition_only(self):
        # four competing answers: no set of at most two holds on 60% of the mass
        db = fake_db([(b'1', 'white_to_move', {'m1': 0.9, 'm2': 0.1}, {}),
                      (b'2', 'white_to_move', {'m2': 0.9, 'm1': 0.1}, {}),
                      (b'3', 'white_to_move', {'m3': 0.9, 'm1': 0.1}, {}),
                      (b'4', 'white_to_move', {'m4': 0.9, 'm1': 0.1}, {})])
        rule = rules.derive_rule(db, 'S1', 'local2200', {'feature': 'role',
                                                         'value': 'white_to_move'},
                                 None, None, role='white_to_move')
        self.assertEqual(rule['kind'], 'recognition_only')
        self.assertEqual(rule['answers'], [])
        self.assertIn('share of the family', rule['reason_text'])
        self.assertGreater(rule['n_exceptions'], 0)

    def test_rules_never_mix_the_two_sides(self):
        """A taught answer is an instruction, so it must be scoped to the side to move."""
        db = fake_db([(b'1', 'white_to_move', {'w1': 0.9, 'w2': 0.1}, {}),
                      (b'2', 'black_to_move', {'b1': 0.9, 'b2': 0.1}, {}),
                      (b'3', 'white_to_move', {'w1': 0.8, 'w2': 0.2}, {}),
                      (b'4', 'black_to_move', {'b1': 0.85, 'b2': 0.15}, {})])
        derived = rules.derive_family_rules(db, 'S1', 'local2200')
        by_role = {rule['condition'].get('role'): rule for rule in derived
                   if rule['kind'] == 'prescriptive'}
        self.assertEqual(set(by_role), {'white_to_move', 'black_to_move'})
        self.assertEqual(by_role['white_to_move']['answers'], ['w1'])
        self.assertEqual(by_role['black_to_move']['answers'], ['b1'])

    def test_support_is_measured_on_flow_mass_not_board_count(self):
        db = fake_db([(b'1', 'white_to_move', {'m1': 0.9, 'm2': 0.1}, {}),
                      (b'2', 'white_to_move', {'m2': 0.9, 'm1': 0.1}, {})])
        db.execute('UPDATE position_flow SET reach_flow=0.99 WHERE position_key=?', (b'1',))
        db.execute('UPDATE position_flow SET reach_flow=0.01 WHERE position_key=?', (b'2',))
        db.commit()
        rule = rules.derive_rule(db, 'S1', 'local2200', {'feature': 'role',
                                                         'value': 'white_to_move'},
                                 None, None, role='white_to_move')
        self.assertGreater(rule['support'], 0.9)
        self.assertEqual(rule['kind'], 'prescriptive')

    def test_a_two_answer_set_must_be_matched_strictly(self):
        """A 50/50 split between two answers is not a rule, even though two moves
        together cover the family: no board teaches that pair."""
        db = fake_db([(b'1', 'white_to_move', {'m1': 0.9, 'm2': 0.1}, {}),
                      (b'2', 'white_to_move', {'m2': 0.9, 'm1': 0.1}, {})])
        rule = rules.derive_rule(db, 'S1', 'local2200', {'feature': 'role',
                                                         'value': 'white_to_move'},
                                 None, None, role='white_to_move')
        self.assertEqual(rule['kind'], 'recognition_only')
        self.assertAlmostEqual(rule['strict_support'], 0.0)

    def test_condition_selects_a_subset(self):
        db = fake_db([(b'1', 'white_to_move', {'m1': 0.9, 'm2': 0.1}, {}),
                      (b'2', 'white_to_move', {'m2': 0.9, 'm1': 0.1}, {})])
        meta_of = {b'1': {'castling_white': 'available_both'},
                   b'2': {'castling_white': 'castled_kingside'}}
        rule = rules.derive_rule(db, 'S1', 'local2200',
                                 {'feature': 'castling_white', 'value': 'castled_kingside'},
                                 meta_of, None, role='white_to_move')
        self.assertEqual(rule['kind'], 'prescriptive')
        self.assertEqual(rule['answers'], ['m2'])
        self.assertAlmostEqual(rule['applicability'], 0.5)


class ItemTests(unittest.TestCase):
    def test_recognition_rules_become_items_without_answers(self):
        db = fake_db([(b'1', 'white_to_move', {'m1': 0.9, 'm2': 0.1}, {}),
                      (b'2', 'white_to_move', {'m2': 0.9, 'm1': 0.1}, {}),
                      (b'3', 'white_to_move', {'m3': 0.9, 'm1': 0.1}, {}),
                      (b'4', 'white_to_move', {'m4': 0.9, 'm1': 0.1}, {})])
        rule = rules.derive_rule(db, 'S1', 'local2200', {'feature': 'role',
                                                         'value': 'white_to_move'},
                                 None, None, role='white_to_move')
        items = rules.as_items(db, [rule], 'local2200')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['type'], 'orientation')
        self.assertEqual(items[0]['answers'], [])
        self.assertGreater(items[0]['complexity']['cost'], 0)   # burden is charged

    def test_prescriptive_rules_carry_their_answer_set_and_scope(self):
        db = fake_db([(b'1', 'white_to_move', {'m1': 0.9, 'm2': 0.1}, {}),
                      (b'2', 'white_to_move', {'m1': 0.8, 'm2': 0.2}, {}),
                      (b'3', 'black_to_move', {'b1': 0.9, 'b2': 0.1}, {})])
        derived = rules.derive_family_rules(db, 'S1', 'local2200')
        items = rules.as_items(db, derived, 'local2200')
        prescriptive = [item for item in items if item['kind'] == 'prescriptive']
        self.assertEqual(len(prescriptive), 2)
        for item in prescriptive:
            self.assertEqual(len(item['keys']), 2) if item['condition']['role'] == 'white_to_move' \
                else self.assertEqual(len(item['keys']), 1)
            self.assertTrue(item['answers'])


if __name__ == '__main__':
    unittest.main()
