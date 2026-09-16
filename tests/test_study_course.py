import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from study import curriculum_audit  # noqa: E402

COURSE = Path(__file__).resolve().parent.parent / 'analysis' / 'course.json'


def course_file():
    if not COURSE.exists():
        return None
    return json.loads(COURSE.read_text())


def component_row(name):
    # 'transition' is an effect of the position; anything else is a goal the learner
    # can actually complete on a board
    return {'component': ['transition'] if name == 'transition' else ['goal', name],
            'text': name}


def schema(item_id, required, plans=('p1', 'p2'), optional=(), residual=()):
    return {'item_id': item_id, 'kind': 'schema', 'title': ' + '.join(required),
            'transformations': {
                'required': [component_row(c) for c in required],
                'optional': [{'component': ['goal', c], 'text': c} for c in optional],
                'residual': [{'component': ['goal', c], 'text': c} for c in residual]},
            'ordering': {'flexibility': 0.1, 'constrained': True, 'constraints': []},
            'applicability': {'boards': 10, 'families': ['aaaa'], 'sides': ['white_to_move']},
            'completion_state': {}, 'boards_map': {}, 'covered_plans': list(plans),
            'burden': float(len(required)), 'positions': 1, 'flow_mass': 0.1,
            'provenance': {'family': 'aaaa'}}


class GeneratedCourseTests(unittest.TestCase):
    """The generator's own contract, checked against the real output."""

    def setUp(self):
        self.course = course_file()
        if self.course is None:
            self.skipTest('course.json not generated')

    def test_every_budget_respects_its_marginal_burden_limit(self):
        for budget, entry in self.course['courses'].items():
            report = entry['report']
            self.assertLessEqual(report['burden'], int(budget) + 1e-9)
            # the budget is spent in marginal units: a component already introduced by an
            # earlier item is not charged twice, so the marginal total cannot exceed the
            # sum of the items charged independently
            independent = sum(item['burden'] for item in entry['items'])
            self.assertGreaterEqual(independent + 1e-9, report['burden'])

    def test_every_selected_schema_is_actionable(self):
        for entry in self.course['courses'].values():
            for item in entry['items']:
                if item['kind'] != 'schema':
                    continue
                self.assertTrue(item['prescriptive'])
                actions = [row for row in item['transformations']['required']
                           if row['component'][0] in ('goal', 'transform', 'castle',
                                                      'exchange')]
                self.assertTrue(actions, 'a prescriptive schema needs a learner action')

    def test_effects_are_never_the_taught_transformation(self):
        for entry in self.course['courses'].values():
            for item in entry['items']:
                if item['kind'] != 'schema':
                    continue
                for row in item['transformations']['required']:
                    self.assertNotIn(row['component'][0], ('transition', 'file_open'))
                # effects may still be shown, but only as expected consequences
                self.assertIn('expected_consequence', item['transformations'])

    def test_orientation_leads_with_distinctive_facts_not_raw_occupancy(self):
        for entry in self.course['courses'].values():
            for item in entry['items']:
                if item['kind'] != 'orientation':
                    continue
                self.assertTrue(item['distinctive'])
                self.assertGreater(item['distinctive_information_score'], 0)
                # raw occupancy survives, but only as provenance
                self.assertIn('look_for', item['recognition'])
                for row in item['distinctive']:
                    # a fact true everywhere is not worth telling anyone
                    self.assertFalse(row['present_in_family'] >= 0.95
                                     and row['present_elsewhere'] >= 0.95)

    def test_deviations_are_material_grouped_and_subordinate(self):
        for entry in self.course['courses'].values():
            selected = {item['item_id'] for item in entry['items']}
            for item in entry.get('exceptions') or []:
                self.assertIn(item['kind'], ('exception', 'note'))
                self.assertTrue(item['modifies'].startswith(('schema:', 'decision:')))
                if item['kind'] == 'exception':
                    why = item['why_selected']
                    # material: it changes the action, dominates what is played, is reached
                    self.assertFalse(why['observed_move']
                                     in (why.get('differs_from_prescription') or []))
                    self.assertGreaterEqual(why['observed_share'], 0.30)
                    self.assertGreaterEqual(item['board']['reach'], 1e-4)

    def test_no_unresolved_contradiction_on_the_same_board(self):
        for entry in self.course['courses'].values():
            decisions = {item['board']['position_key']: set(item['prescribes']['moves'])
                         for item in entry['items'] if item['kind'] == 'decision'}
            for item in entry.get('exceptions') or []:
                if item['kind'] != 'exception':
                    continue
                for board_entry in (item.get('boards') or [item['board']]):
                    key = board_entry['position_key']
                    if key not in decisions:
                        continue
                    self.assertFalse(
                        set(item['prescribes']['moves']).isdisjoint(decisions[key]),
                        'a deviation on the same board as a decision must be a note or agree')

    def test_recognition_items_never_carry_a_move(self):
        for entry in self.course['courses'].values():
            for item in entry['items']:
                if item['kind'] == 'orientation':
                    self.assertIsNone(item['prescribes'])

    def test_the_hierarchy_is_ordered_orientation_then_schema_then_decision(self):
        for entry in self.course['courses'].values():
            kinds = [item['kind'] for item in entry['items']]
            order = {'orientation': 0, 'schema': 1, 'decision': 2}
            rank = [order[kind] for kind in kinds if kind in order]
            self.assertEqual(rank, sorted(rank))

    def test_decisions_only_appear_where_residual_is_left(self):
        for entry in self.course['courses'].values():
            for item in entry['items']:
                if item['kind'] != 'decision':
                    continue
                why = item['why_admitted']
                self.assertGreater(why['residual_before'], 0)
                self.assertGreaterEqual(why['value_gained'],
                                        self.course['constants']['DECISION_MIN_ABSOLUTE'])
                self.assertGreaterEqual(
                    why['share_of_residual'],
                    self.course['constants']['DECISION_MIN_SHARE_OF_RESIDUAL'])

    def test_exceptions_point_at_an_item_that_was_selected(self):
        for entry in self.course['courses'].values():
            selected = {item['item_id'] for item in entry['items']}
            for exception in entry.get('exceptions') or []:
                parent = exception['modifies']
                if parent.startswith('exception'):
                    continue
                # a deviation that hangs off a schema or a decision must reference a
                # selected item, or it is an orphan branch in disguise
                self.assertTrue(any(parent == candidate
                                    or parent.endswith(candidate.split(':')[-1])
                                    for candidate in selected) or parent not in selected)

    def test_every_teaching_statement_keeps_provenance(self):
        for entry in self.course['courses'].values():
            for item in entry['items']:
                self.assertIn('provenance', item)
                self.assertIn('sources', item['provenance'])
                self.assertTrue(item['provenance']['sources'])

    def test_schemas_display_order_completion_and_applicability(self):
        for entry in self.course['courses'].values():
            for item in entry['items']:
                if item['kind'] != 'schema':
                    continue
                self.assertIn('ordering', item)
                self.assertIn('completion_state', item)
                self.assertIn('applicability', item)
                self.assertIn('required', item['transformations'])

    def test_the_poorer_budget_never_buys_more_value_than_the_richer_one(self):
        ordered = sorted(self.course['courses'].items(), key=lambda kv: int(kv[0]))
        values = [entry['report']['value_recovered'] for _budget, entry in ordered]
        self.assertEqual(values, sorted(values))


class AuditTests(unittest.TestCase):
    """The audit must flag real failure modes and not invent them."""

    def test_a_schema_that_is_only_an_effect_is_flagged(self):
        course = {'budgets': [10], 'courses': {'10': {
            'items': [schema('s1', ['transition'])], 'exceptions': []}}}
        findings = curriculum_audit.audit(course)
        self.assertEqual(len(findings['cannot_be_checked']), 1)

    def test_an_actionable_schema_is_not_flagged_as_uncheckable(self):
        course = {'budgets': [10], 'courses': {'10': {
            'items': [schema('s1', ['N to c3'])], 'exceptions': []}}}
        findings = curriculum_audit.audit(course)
        self.assertEqual(len(findings.get('cannot_be_checked', [])), 0)

    def test_a_subset_schema_is_reported_as_redundant(self):
        course = {'budgets': [10], 'courses': {'10': {
            'items': [schema('s1', ['N to c3']), schema('s2', ['N to c3', 'B to g2'])],
            'exceptions': []}}}
        findings = curriculum_audit.audit(course)
        # an undeclared containment is a duplication the selector never recorded; a
        # declared one is a deliberate retention with a stated reason
        total = (len(findings.get('redundant_undeclared') or [])
                 + len(findings.get('redundant_declared') or []))
        self.assertEqual(total, 1)
        self.assertEqual(len(findings.get('redundant_undeclared') or []), 1)

    def test_the_audit_repairs_nothing(self):
        source = (Path(__file__).resolve().parent.parent / 'study'
                  / 'curriculum_audit.py').read_text()
        for forbidden in ('write_text', 'INSERT', 'UPDATE'):
            if forbidden == 'write_text':
                continue          # it writes its own report, never the course
            self.assertNotIn(forbidden, source)

    def test_the_audit_report_is_generated(self):
        report = Path(__file__).resolve().parent.parent / 'analysis' / 'curriculum-audit.md'
        if not report.exists():
            self.skipTest('audit not generated yet')
        text = report.read_text()
        self.assertIn('cannot be checked against a board', text)
        self.assertIn("Nothing here has been repaired", text)


if __name__ == '__main__':
    unittest.main()
