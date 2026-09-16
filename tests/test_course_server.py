import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import course_server  # noqa: E402

DB = ROOT / 'data' / 'atlas-analysis.sqlite'
COURSE = ROOT / 'analysis' / 'course.json'
UI = ROOT / 'course_ui'


class FlowEndpointTests(unittest.TestCase):
    """The opening map reads the existing flow graph; it must not invent transitions."""

    @classmethod
    def setUpClass(cls):
        if not DB.exists():
            raise unittest.SkipTest('analysis database not present')
        cls.flow = course_server.flow(DB)

    def test_edges_are_masses_between_known_structures(self):
        ids = {entry['id'] for entry in self.flow['structures']}
        self.assertTrue(self.flow['edges'])
        for edge in self.flow['edges']:
            self.assertIn(edge['from'], ids)
            self.assertIn(edge['to'], ids)
            self.assertGreater(edge['mass'], 0)
            self.assertLessEqual(edge['mass'], 1.0)

    def test_the_main_trunk_transition_is_present(self):
        # 1.e4 d5 is the opening's trunk; if the map cannot show it, the map is wrong
        moves = {edge['move'] for edge in self.flow['edges']}
        self.assertIn('e4d5', moves)

    def test_every_structure_carries_a_board_for_the_miniatures(self):
        with_board = [entry for entry in self.flow['structures'] if entry.get('board')]
        self.assertTrue(with_board)
        self.assertTrue(any(entry['board'].get('fen') for entry in with_board))


class OrderEndpointTests(unittest.TestCase):
    """Alternative move orders come from the same graph, and must actually differ."""

    @classmethod
    def setUpClass(cls):
        if not DB.exists():
            raise unittest.SkipTest('analysis database not present')
        cls.family = None
        if COURSE.exists():
            import json
            course = json.loads(COURSE.read_text())
            entry = course['courses'][sorted(course['courses'])[len(course['courses']) // 2]]
            structures = [item for item in entry['items'] if item['kind'] == 'orientation']
            cls.family = structures[0]['provenance']['family'] if structures else None

    def test_the_main_line_starts_on_the_busiest_move(self):
        # game counts along a line are not monotone (a later position can be busier than
        # the edge taken to reach it), but the *same* position's busiest move must win
        if not self.family:
            self.skipTest('no course generated')
        data = course_server.orders(self.family, DB)
        self.assertTrue(data['orders'])
        main = data['orders'][0]
        self.assertEqual(main['label'], 'main line')
        self.assertTrue(main['moves'])
        for alternative in data['orders'][1:]:
            self.assertGreaterEqual(main['moves'][0]['games'],
                                    alternative['moves'][0]['games'])

    def test_an_alternative_order_really_differs(self):
        if not self.family:
            self.skipTest('no course generated')
        data = course_server.orders(self.family, DB)
        if len(data['orders']) < 2:
            self.skipTest('no alternative available for this family')
        first = [move['uci'] for move in data['orders'][0]['moves']]
        second = [move['uci'] for move in data['orders'][1]['moves']]
        self.assertNotEqual(first, second)

    def test_a_missing_family_is_an_error_not_a_crash(self):
        self.assertIn('error', course_server.orders('', DB))
        self.assertEqual(course_server.orders('does-not-exist', DB)['orders'], [])


class EvidenceEndpointTests(unittest.TestCase):
    def test_evidence_returns_the_distributions_behind_a_board(self):
        if not DB.exists() or not COURSE.exists():
            self.skipTest('data or course missing')
        import json
        course = json.loads(COURSE.read_text())
        entry = course['courses'][sorted(course['courses'])[len(course['courses']) // 2]]
        structure = [item for item in entry['items'] if item['kind'] == 'orientation'][0]
        key = structure['representative_board']['position_key']
        data = course_server.evidence(key, DB)
        self.assertIn('position', data)
        self.assertIn('distributions', data)
        self.assertTrue(data['distributions'])

    def test_a_bad_key_is_reported_rather_than_raising(self):
        self.assertIn('error', course_server.evidence('zzz', DB))
        self.assertIn('error', course_server.evidence('', DB))


class FrontendTests(unittest.TestCase):
    """The redesigned UI must exist, be served, and not leak research terminology."""

    def test_the_shell_loads_the_new_stylesheet_and_app(self):
        html = (UI / 'index.html').read_text()
        self.assertIn('/ui/styles.css', html)
        self.assertIn('/ui/app.js', html)

    def test_the_board_is_sized_from_the_square_not_the_viewport(self):
        import re
        css = re.sub(r'/\*.*?\*/', '', (UI / 'styles.css').read_text(), flags=re.S)
        # a viewport-sized glyph overflowed the squares and cropped the board, so no
        # declaration may size the board or its glyph from the viewport
        for declaration in re.findall(r'font-size:[^;]+;', css):
            self.assertNotIn('vmin', declaration)
            self.assertNotIn('vw', declaration)
        self.assertIn('--board-size', css)

    def test_learner_facing_labels_do_not_show_internal_terminology(self):
        app = (UI / 'app.js').read_text()
        # the navigation labels come from shortTitle(); hashes must never reach it
        self.assertIn('function shortTitle', app)
        self.assertNotIn("item.title : ''", app)
        # research vocabulary stays available, but only inside the evidence drawer
        for term in ('entropy', 'burden'):
            self.assertIn(term, app)                    # present in evidence tabs
        self.assertIn('Why am I being taught this?', app)

    def test_the_server_serves_static_assets_read_only(self):
        import inspect
        source = inspect.getsource(course_server.Handler._static)
        for forbidden in ('open(', 'write', 'POST'):
            self.assertNotIn(forbidden, source)
        self.assertIn('UI / name', source)


if __name__ == '__main__':
    unittest.main()
