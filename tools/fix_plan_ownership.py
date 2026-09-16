"""Fix the plan arrow/state ownership bug in course_ui/app.js.

The UI inferred which colour a plan's transformation belongs to from the board's side to
move. Plans are one-sided (applicability.sides = ["black_to_move"]), so whenever the shown
position had the other side to move the arrows were drawn from the wrong colour's pieces:
a Black plan (Bf5/Nf6/e6) rendered as White's bishop, knight and pawn pointing at Black's
goal squares, which the pieces cannot legally reach.

Also fixes the castle branch, which read component[1] for the colour -- that element holds
the string "castle", so every castle arrow was drawn from e8 as if Black were castling --
and the state check that reported a transformation "done" when the owning side simply had
no piece of that type on the board.
"""
from pathlib import Path

path = Path('course_ui/app.js')
source = path.read_text()
before = source

# 1. a helper that decides ownership from the plan's own sides, falling back to the mover
helper = '''function ownerFor(sides, fen) {
  /* A plan listed for one colour belongs to that colour, so its pieces are the ones to
     read. A plan listed for both belongs to whoever is to move. Inferring this from the
     side to move alone -- which is what this used to do -- draws a Black plan's arrows
     from White's pieces whenever the shown position happens to have White to move. */
  const list = sides || [];
  if (list.length === 1 && list[0]) {
    const first = list[0].charAt(0);
    if (first === 'w' || first === 'b') return first;
  }
  return turnOf(fen);
}
function castleTarget(side, flank) {
  /* component[2] is the flank ('k' or 'q'), never the colour */
  const kingside = flank !== 'q';
  if (side === 'w') return kingside ? 'g1' : 'c1';
  return kingside ? 'g8' : 'c8';
}
'''
anchor = '/* the same state test the generator uses: has this transformation happened, is it'
assert anchor in source
source = source.replace(anchor, helper + anchor, 1)

# 2. state check takes the owner
old_state = """function transformationState(component, fen) {
  const pieces = parseFen(fen); const turn = turnOf(fen);
  const kind = component[0];
  if (kind === 'goal') {
    const letter = component[1].toLowerCase(); const to = component[2];
    const own = turn === 'w' ? letter.toUpperCase() : letter;
    if (pieces[to] === own) return 'done';
    const from = squaresOf(pieces, own);
    if (!from.length) return 'done';
    return 'pending';
  }
  if (kind === 'castle') {
    const colour = component[1]; const home = colour === 'w' ? 'e1' : 'e8';
    const king = pieces[home];
    if (!king || king.toLowerCase() !== 'k') return 'done';
    return 'pending';
  }"""
new_state = """function transformationState(component, fen, side) {
  const pieces = parseFen(fen); const turn = side || turnOf(fen);
  const kind = component[0];
  if (kind === 'goal') {
    const letter = component[1].toLowerCase(); const to = component[2];
    const own = turn === 'w' ? letter.toUpperCase() : letter;
    if (pieces[to] === own) return 'done';
    const from = squaresOf(pieces, own);
    /* the owning side having no such piece left means this cannot be read off the
       board -- it is not the same thing as having completed the transformation */
    if (!from.length) return 'unknown';
    return 'pending';
  }
  if (kind === 'castle') {
    const home = turn === 'w' ? 'e1' : 'e8';
    const king = pieces[home];
    if (!king || king.toLowerCase() !== 'k') return 'done';
    return 'pending';
  }"""
assert old_state in source
source = source.replace(old_state, new_state, 1)

# 3. arrows take the owner, and the castle branch reads the flank and the plan's colour
old_arrow = """function arrowFor(component, fen) {
  const pieces = parseFen(fen); const turn = turnOf(fen);
  const kind = component[0];"""
new_arrow = """function arrowFor(component, fen, side) {
  const pieces = parseFen(fen); const turn = side || turnOf(fen);
  const kind = component[0];"""
assert old_arrow in source
source = source.replace(old_arrow, new_arrow, 1)

old_castle = """  if (kind === 'castle') {
    const colour = component[1]; const home = colour === 'w' ? 'e1' : 'e8';
    if (pieces[home]) return {from: home, to: colour === 'w' ? 'g1' : 'g8', castle:true};
    return {to: colour === 'w' ? 'g1' : 'g8', done:true};
  }"""
new_castle = """  if (kind === 'castle') {
    const target = castleTarget(turn, component[2]);
    const home = turn === 'w' ? 'e1' : 'e8';
    if (pieces[home]) return {from: home, to: target, castle:true};
    return {to: target, done:true};
  }"""
assert old_castle in source
source = source.replace(old_castle, new_castle, 1)

# 4. the plan view resolves the owner once and passes it everywhere
old_valid = """  const valid = fetched.filter(row => row.fen);
  const scored = valid.map(row => ({...row,
    pending: item.transformations.required.filter(t => transformationState(t.component, row.fen) === 'pending').length
      + item.transformations.optional.filter(t => transformationState(t.component, row.fen) === 'pending').length}));"""
new_valid = """  const valid = fetched.filter(row => row.fen);
  const sides = (item.applicability && item.applicability.sides) || [];
  const ownerAt = f => ownerFor(sides, f);
  const scored = valid.map(row => ({...row,
    pending: item.transformations.required.filter(t => transformationState(t.component, row.fen, ownerAt(row.fen)) === 'pending').length
      + item.transformations.optional.filter(t => transformationState(t.component, row.fen, ownerAt(row.fen)) === 'pending').length}));"""
assert old_valid in source
source = source.replace(old_valid, new_valid, 1)

count = source.count('arrowFor(t.component, fenNow)')
assert count == 2, count
source = source.replace('arrowFor(t.component, fenNow)',
                        'arrowFor(t.component, fenNow, planOwner)')

count = source.count("transformationState(t.component, fenNow) === 'done'")
assert count == 2, count
source = source.replace("transformationState(t.component, fenNow) === 'done'",
                        "transformationState(t.component, fenNow, planOwner) === 'done'")

# the evidence drawer reports states against the plan's own colour too; and the review
# question derives its answer set from those states. Both are handled before the blanket
# replacement below, because they carry their own owner expressions.
old_evidence = """transformationState(t.component, state.board.fen || '')"""
new_evidence = """transformationState(t.component, state.board.fen || '',
          ownerFor(item.applicability && item.applicability.sides, state.board.fen || ''))"""
assert old_evidence in source
source = source.replace(old_evidence, new_evidence, 1)

old_review = """        .filter(t => transformationState(t.component, fen) === 'pending')"""
new_review = """        .filter(t => transformationState(t.component, fen,
          ownerFor(answer.applicability && answer.applicability.sides, fen)) === 'pending')"""
assert old_review in source
source = source.replace(old_review, new_review, 1)

count = source.count('transformationState(t.component, fen)')
assert count == 2, count
source = source.replace('transformationState(t.component, fen)',
                        'transformationState(t.component, fen, planOwner)')

# define planOwner where the plan view knows both the item and the board it settled on
old_plan_owner = """  let fen = chosen.fen;
  let currentPositionKey = chosen.key;
  panel.innerHTML = '';"""
new_plan_owner = """  let fen = chosen.fen;
  const planOwner = ownerAt(fen);
  let currentPositionKey = chosen.key;
  panel.innerHTML = '';"""
assert old_plan_owner in source
source = source.replace(old_plan_owner, new_plan_owner, 1)


path.write_text(source)
print(f'app.js: {len(before)} -> {len(source)} bytes')
print('ownerFor call sites:', source.count('ownerFor('))
