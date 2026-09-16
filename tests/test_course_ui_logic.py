"""Regression tests for the course UI's transformation logic.

These exercise the pure functions in course_ui/app.js -- the ones that decide whose piece
belongs to a plan and what a transformation's state is -- by extracting them from the file
and running them under node. app.js has no DOM in that region, so it runs standalone.

The bug being guarded against: the UI took a plan's owner from the board's side to move.
Plans are one-sided (applicability.sides = ["black_to_move"]), so whenever the displayed
position had the other side to move, the plan's arrows were drawn from the opposite
colour's pieces -- a Black plan (Bf5/Nf6/e6) rendered as White's bishop, knight and pawn
pointing at Black's goal squares, which those pieces cannot legally reach.
"""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / 'course_ui' / 'app.js'
START = 'const FILES'
END = '/* -------------------------------------------------------------------- board */'

TESTS = r'''
const assert = require('assert');

/* the exact position from the user's screenshot: Black's plan, White to move */
const USER_FEN = 'rn1qkbnr/pp2pppp/2p3b1/8/3P4/6N1/PPP2PPP/R1BQKBNR w KQkq -';
const BLACK_PLAN = ['black_to_move'];

const owner = ownerFor(BLACK_PLAN, USER_FEN);
assert.strictEqual(owner, 'b', 'a black_to_move plan belongs to Black');

const pieces = parseFen(USER_FEN);
const isBlack = sq => (pieces[sq] || '').toLowerCase() === pieces[sq] && pieces[sq];

/* the three arrows from that plan must start from Black's pieces */
const bishop = arrowFor(['goal', 'B', 'f5'], USER_FEN, owner);
assert.strictEqual(bishop.from, 'g6', 'Bf5 comes from the black bishop on g6');
assert.strictEqual(bishop.to, 'f5');
assert.ok(isBlack(bishop.from), 'the arrow starts from a black piece');

const knight = arrowFor(['goal', 'N', 'f6'], USER_FEN, owner);
assert.strictEqual(knight.from, 'g8', 'Nf6 comes from the black knight on g8');
assert.ok(isBlack(knight.from), 'the arrow starts from a black piece');

const pawn = arrowFor(['goal', 'P', 'e6'], USER_FEN, owner);
assert.strictEqual(pawn.from, 'e7', 'e6 comes from the black pawn on e7');
assert.ok(isBlack(pawn.from), 'the arrow starts from a black piece');

/* and none of them may start from White, which is what the bug did */
for (const arrow of [bishop, knight, pawn]) {
  assert.ok(!isBlack(arrow.from) === false, 'never drawn from White');
}

/* ownership is what fixed it: without a side the code falls back to the mover */
const withoutOwner = arrowFor(['goal', 'B', 'f5'], USER_FEN);
assert.ok(isBlack(withoutOwner.from) === false,
  'falling back to the side to move does pick a White piece, which is why the plan side must be passed');

/* a plan listed for both colours belongs to whoever is to move */
assert.strictEqual(ownerFor(['black_to_move', 'white_to_move'], USER_FEN), 'w');
assert.strictEqual(ownerFor([], USER_FEN), 'w');
assert.strictEqual(ownerFor(['white_to_move'], USER_FEN), 'w', 'one-sided white plan');

/* state: a Black goal already met must read done, and not be judged against White */
const BLACK_PAWN_HOME = 'rnbqkbnr/pp3ppp/2p1p3/8/3P4/8/PPP2PPP/R1BQKBNR w KQkq -';
assert.strictEqual(transformationState(['goal', 'P', 'e6'], BLACK_PAWN_HOME, 'b'), 'done');
assert.strictEqual(transformationState(['goal', 'P', 'e6'], BLACK_PAWN_HOME), 'pending',
  'the old fallback judged a Black goal against White');

/* an owner with no piece of that type is unreadable, not completed */
const NO_BLACK_KNIGHTS = 'r1bqkb1r/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq -';
assert.strictEqual(transformationState(['goal', 'N', 'f6'], NO_BLACK_KNIGHTS, 'b'), 'unknown');

/* castling: component[2] is the flank, the colour comes from the plan */
assert.strictEqual(castleTarget('w', 'k'), 'g1');
assert.strictEqual(castleTarget('w', 'q'), 'c1');
assert.strictEqual(castleTarget('b', 'k'), 'g8');
assert.strictEqual(castleTarget('b', 'q'), 'c8');

const START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -';
const whiteCastle = arrowFor(['castle', 'castle', 'k'], START_FEN, 'w');
assert.strictEqual(whiteCastle.from, 'e1', 'White castles from e1, not from Black e8');
assert.strictEqual(whiteCastle.to, 'g1');
const blackCastle = arrowFor(['castle', 'castle', 'k'], START_FEN, 'b');
assert.strictEqual(blackCastle.from, 'e8');
assert.strictEqual(blackCastle.to, 'g8');
const whiteQueenside = arrowFor(['castle', 'castle', 'q'], START_FEN, 'w');
assert.strictEqual(whiteQueenside.to, 'c1');

/* a castled White king must not be reported as still pending because Black's king is home */
const WHITE_CASTLED = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQ1RK1 b kq -';
assert.strictEqual(transformationState(['castle', 'castle', 'k'], WHITE_CASTLED, 'w'), 'done');
assert.strictEqual(transformationState(['castle', 'castle', 'k'], WHITE_CASTLED), 'pending',
  'the old fallback looked at e8 and saw Black still uncastled');

console.log('ok');
'''


def extract_pure_region(source):
    """The slice of app.js that has no DOM dependency, so it can run under node.

    The slice starts at parseFen because everything above it touches localStorage and the
    document. FILES is defined up there, so its declaration is carried over verbatim --
    taken from the source rather than restated, so the harness cannot drift from app.js.
    """
    files = re.search(r'^const FILES = .*$', source, re.M)
    start = source.index('function parseFen(fen) {')
    end = source.index(END)
    return files.group(0) + '\n' + source[start:end]


class CourseUiLogicTest(unittest.TestCase):

    def test_transformation_ownership_and_state(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        source = APP.read_text()
        region = extract_pure_region(source)
        for needed in ('parseFen', 'turnOf', 'squaresOf', 'distance', 'ownerFor',
                       'castleTarget', 'transformationState', 'arrowFor'):
            self.assertIn(f'function {needed}(', region,
                          f'{needed} must live in the DOM-free region of app.js')
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / 'logic_test.js'
            script.write_text(region + TESTS)
            result = subprocess.run([node, str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         f'node test failed:\n{result.stdout}\n{result.stderr}')
        self.assertIn('ok', result.stdout)

    def test_plan_view_passes_the_plan_side_everywhere(self):
        """Every call that reads a transformation in the plan view must pass the owner.

        The invariant is that an owner is resolved explicitly -- via planOwner, ownerAt or
        ownerFor -- rather than left to the function's fallback, which reads the side to
        move. That fallback is exactly what drew a one-sided plan's arrows from the wrong
        colour's pieces.
        """
        source = APP.read_text()
        plan_view = source[source.index('async function renderPlan'):]
        plan_view = plan_view[:plan_view.index('/* ------')]
        found = 0
        for match in re.finditer(r'transformationState\(t\.component, ([^)]+(?:\([^)]*\))?)\)',
                                 plan_view):
            found += 1
            passed = match.group(1)
            self.assertTrue(any(token in passed for token in ('planOwner', 'ownerAt', 'ownerFor')),
                            f'transformationState called without an owner: {match.group(0)}')
        self.assertGreaterEqual(found, 5, 'expected several state checks in the plan view')

    def test_arrow_calls_pass_the_plan_side(self):
        source = APP.read_text()
        for match in re.finditer(r'arrowFor\(t\.component, fenNow[^)]*\)', source):
            self.assertIn('planOwner', match.group(0),
                          f'arrowFor called without the plan side: {match.group(0)}')


if __name__ == '__main__':
    unittest.main()
