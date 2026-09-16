'use strict';
/* Scandinavian Defence — generated course UI.
   Read-only frontend over /course.json (+ /evidence, /flow, /orders, /ui/*).
   It changes nothing about the curriculum: every teaching statement is rendered from
   the generated course, and all raw evidence stays behind the Evidence drawer. */

const PIECE_GLYPH = {K:'♔',Q:'♕',R:'♖',B:'♗',N:'♘',P:'♙',k:'♚',q:'♛',r:'♜',b:'♝',n:'♞',p:'♟'};
const PIECE_NAME = {P:'Pawn',N:'Knight',B:'Bishop',R:'Rook',Q:'Queen',K:'King'};
const FILES = ['a','b','c','d','e','f','g','h'];

const state = {
  course: null, budget: '25', route: {view:'overview', id:null},
  seen: new Set(JSON.parse(localStorage.getItem('scand-seen') || '[]')),
  flow: null, orders: null, review: null, reviewScore: {right:0,total:0},
  board: {fen:null, arrows:[], highlights:[], badge:'', selected:null, target:null},
};

/* ------------------------------------------------------------------ helpers */
const el = (tag, cls, html) => { const n=document.createElement(tag);
  if (cls) n.className=cls; if (html!=null) n.innerHTML=html; return n; };
const course = () => state.course.courses[state.budget];
const items = () => course().items;
const byKind = kind => items().filter(i => i.kind === kind);
const exceptions = () => (course().exceptions || []);
const plans = () => byKind('schema');
const structures = () => byKind('orientation');
const decisions = () => byKind('decision');

function pieceName(letter) { return PIECE_NAME[letter] || letter; }
function translateTitle(text) {
  return String(text || '')
    .replace(/\b([PNBRQK]) to ([a-h][1-8])/g, (m, l, sq) => `${pieceName(l)} to ${sq}`)
    .replace(/\bcastle k\b/g, 'castle kingside')
    .replace(/\bcastle q\b/g, 'castle queenside')
    .replace(/\bthe position leaves this structure\b/g, 'the position changes shape');
}
function shortTitle(item) {
  if (item.kind === 'orientation') return structureLabel(item);
  if (item.kind === 'decision') {
    // never show an internal board id to a learner: name the position by the structure
    // it belongs to, discovered from the same evidence endpoint the drawer uses
    const label = state.decisionLabels && state.decisionLabels[item.item_id];
    return label || 'Position to remember';
  }
  return translateTitle(item.title);
}
function structureLabel(item) {
  const facts = (item.distinctive || []);
  const pawns = facts.filter(f => f.fact.startsWith('bp ') || f.fact.startsWith('wp '));
  const pick = (pawns.length ? pawns : facts).slice(0, 3);
  if (!pick.length) return 'Structure';
  return pick.map(f => {
    const [code, , square] = f.fact.split(' ');
    const black = code[0] === 'b';
    const long = code[1] === 'p' ? '' : pieceName(code[1].toUpperCase());
    return `${black ? '…' : ''}${long}${square}`;
  }).join(' ');
}
function richestPlan() {
  /* open on the plan that shows the most: several transformations across several boards */
  return plans().slice().sort((a, b) =>
    (b.transformations.required.length - a.transformations.required.length)
    || (b.applicability.boards - a.applicability.boards))[0];
}
function sortedBySeverity(list) { return list.slice().sort((a,b)=> (b.occurrences||1)-(a.occurrences||1)); }

function pieceCode(fenChar) {
  /* FEN uses case for colour and a single letter for the piece ('r'); the asset files
     are named for both ('bR'). Mixing them up silently 404s every piece. */
  const upper = fenChar.toUpperCase();
  return (fenChar === upper ? 'w' : 'b') + upper;
}
function glyphFor(code) {
  return code[0] === 'w' ? code[1] : code[1].toLowerCase();
}
function pieceImage(code) {
  /* lichess's alpha piece set, served from /ui; code is 'wK' / 'bQ' style */
  const img = el('img', 'piece');
  img.src = `/ui/piece-alpha-${code}.svg`;
  img.alt = code;
  img.draggable = false;
  let retried = false;
  img.onerror = () => {
    if (!retried) {                    // a burst can drop a request; try once more
      retried = true;
      setTimeout(() => { img.src = `/ui/piece-alpha-${code}.svg?r=1`; }, 250);
      return;
    }
    img.replaceWith(el('span', 'glyph ' + code[0], PIECE_GLYPH[glyphFor(code)]));
  };
  return img;
}
function parseFen(fen) {
  const out = {};
  const rows = (fen || '').split(' ')[0].split('/');
  rows.forEach((row, r) => { let f = 0;
    for (const ch of row) {
      if (/[1-8]/.test(ch)) { f += Number(ch); continue; }
      out[FILES[f] + (8 - r)] = ch; f++;
    }});
  return out;
}
function turnOf(fen) { return ((fen || '').split(' ')[1] || 'w'); }
function squaresOf(pieces, letter) {
  return Object.entries(pieces).filter(([, p]) => p === letter).map(([sq]) => sq);
}
function distance(a, b) {
  const dx = Math.abs(a.charCodeAt(0) - b.charCodeAt(0));
  const dy = Math.abs(Number(a[1]) - Number(b[1]));
  return Math.max(dx, dy);
}
/* the same state test the generator uses: has this transformation happened, is it
   still available, or can it not be read off a board at all */
function transformationState(component, fen) {
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
  }
  if (kind === 'transform') return 'pending';
  return 'unknown';                                  // effects cannot be read off a board
}
function arrowFor(component, fen) {
  const pieces = parseFen(fen); const turn = turnOf(fen);
  const kind = component[0];
  if (kind === 'goal') {
    const letter = component[1].toLowerCase(); const to = component[2];
    const own = turn === 'w' ? letter.toUpperCase() : letter;
    if (pieces[to] === own) return {to, done:true};
    const from = squaresOf(pieces, own).sort((a,b)=>distance(a,to)-distance(b,to))[0];
    return from ? {from, to, done:false} : null;
  }
  if (kind === 'castle') {
    const colour = component[1]; const home = colour === 'w' ? 'e1' : 'e8';
    if (pieces[home]) return {from: home, to: colour === 'w' ? 'g1' : 'g8', castle:true};
    return {to: colour === 'w' ? 'g1' : 'g8', done:true};
  }
  return null;
}
function decodeBitmask(hex) {
  let bits = 0; try { bits = parseInt(hex, 16); } catch (e) { return []; }
  const out = [];
  for (let i = 0; i < 64; i++) if (bits >> i & 1)
    out.push(FILES[i % 8] + (Math.floor(i / 8) + 1));
  return out;
}

/* -------------------------------------------------------------------- board */
function drawBoard(opts) {
  const {fen} = opts;
  const wrap = el('div', 'board');
  if (fen) wrap.dataset.fen = fen;
  const grid = el('div', 'grid');
  const pieces = parseFen(fen);
  const highlights = opts.highlights || {};   // square -> kind
  for (let r = 0; r < 8; r++) for (let f = 0; f < 8; f++) {
    const name = FILES[f] + (8 - r);
    const sq = el('div', 'sq ' + (((r + f) % 2) ? 'dark' : 'light'));
    if (pieces[name]) sq.appendChild(pieceImage(pieceCode(pieces[name])));
    if (highlights[name]) sq.classList.add('hl-' + highlights[name]);
    if (state.board.selected === name) sq.classList.add('sel');
    if (opts.targets && opts.targets.includes(name)) sq.classList.add('target');
    if (f === 0) sq.appendChild(el('span', 'coord', String(8 - r)));
    else if (r === 7) sq.appendChild(el('span', 'coord', name[0]));
    sq.dataset.square = name;
    if (opts.onSquare) sq.addEventListener('click', () => opts.onSquare(name));
    grid.appendChild(sq);
  }
  wrap.appendChild(grid);
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'overlay'); svg.setAttribute('viewBox', '0 0 100 100');
  const centre = sq => [ (sq.charCodeAt(0) - 97) * 12.5 + 6.25, (8 - Number(sq[1])) * 12.5 + 6.25 ];
  for (const arrow of (opts.arrows || [])) {
    if (!arrow.from || !arrow.to) {
      if (arrow.to) {   // completed: just a ring on the destination
        const [x,y] = centre(arrow.to);
        const c = document.createElementNS(svg.namespaceURI,'circle');
        c.setAttribute('cx',x); c.setAttribute('cy',y); c.setAttribute('r',9.5);
        c.setAttribute('fill','none');
        c.setAttribute('stroke', arrow.style === 'optional' ? '#d29b3f' : '#15781b');
        c.setAttribute('stroke-width', 1.6); c.setAttribute('stroke-dasharray','3 2');
        svg.appendChild(c);
      }
      continue;
    }
    const [x1,y1] = centre(arrow.from), [x2,y2] = centre(arrow.to);
    const colour = arrow.done ? '#7f7a73' : (arrow.style === 'optional' ? '#d29b3f' : '#15781b');
    const line = document.createElementNS(svg.namespaceURI,'line');
    line.setAttribute('x1',x1); line.setAttribute('y1',y1);
    line.setAttribute('x2',x2); line.setAttribute('y2',y2);
    line.setAttribute('stroke', colour);
    line.setAttribute('stroke-width', arrow.done ? 1.5 : 2.6);
    line.setAttribute('stroke-linecap','round');
    line.setAttribute('opacity', arrow.done ? 0.5 : 1);
    if (arrow.done) line.setAttribute('stroke-dasharray','4 3');
    line.setAttribute('marker-end', arrow.done ? 'url(#head-done)' : 'url(#head)');
    svg.appendChild(line);
  }
  const defs = document.createElementNS(svg.namespaceURI,'defs');
  defs.innerHTML =
    '<marker id="head" viewBox="0 0 10 10" refX="7" refY="5" markerWidth="4" markerHeight="4" orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" fill="#15781b"/></marker>' +
    '<marker id="head-done" viewBox="0 0 10 10" refX="7" refY="5" markerWidth="3.4" markerHeight="3.4" orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" fill="#7f7a73"/></marker>';
  svg.appendChild(defs);
  wrap.appendChild(svg);
  if (opts.badge) wrap.appendChild(el('div','badge',opts.badge));
  if (opts.caption) wrap.appendChild(el('div','badge',opts.caption));
  return wrap;
}
function highlightStyles() {
  const style = document.createElement('style');
  style.textContent = `
    .sq.hl-pawn { background-image: linear-gradient(rgba(198,214,84,.80), rgba(198,214,84,.80)); }
    .sq.hl-piece { box-shadow: inset 0 0 0 4px rgba(46,158,55,.92); }
    .sq.hl-flexible { box-shadow: inset 0 0 0 2.5px rgba(54,146,231,.75); border-radius: 50%; }
    .sq.hl-distinct { box-shadow: inset 0 0 0 4px rgba(46,158,55,.95); }`;
  return style;
}

/* ----------------------------------------------------------------- evidence */
function openEvidence(tabs, title, sourceItem) {
  state.evidence = {tabs, title, item: sourceItem};
  document.getElementById('drawer-title').textContent = title;
  const bar = document.getElementById('drawer-tabs'); bar.innerHTML = '';
  const body = document.getElementById('drawer-body');
  tabs.forEach((tab, i) => {
    const b = el('button', i === 0 ? 'active' : '', tab.label);
    b.onclick = () => { [...bar.children].forEach(c => c.className = ''); b.className = 'active';
      renderEvidence(body, tab); };
    bar.appendChild(b);
  });
  renderEvidence(body, tabs[0]);
  document.getElementById('drawer').classList.add('open');
  document.getElementById('scrim').classList.add('open');
}
function closeEvidence() {
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('scrim').classList.remove('open');
}
function renderEvidence(body, tab) {
  body.innerHTML = '';
  if (tab.kind === 'async') {
    body.appendChild(el('p', 'muted', 'Loading…'));
    Promise.resolve().then(tab.load).then(inner => {
      body.innerHTML = '';
      renderEvidence(body, {...inner, kind: inner.kind || 'table'});
    }).catch(() => { body.innerHTML = '<p class="muted">Could not load this evidence.</p>'; });
    return;
  }
  if (tab.kind === 'html') { body.appendChild(el('div', null, tab.html)); return; }
  if (tab.kind === 'json') { body.appendChild(el('pre', null, JSON.stringify(tab.data, null, 1))); return; }
  if (tab.kind === 'table') {
    const t = el('table');
    t.innerHTML = '<tr>' + tab.columns.map(c => `<th>${c}</th>`).join('') + '</tr>';
    for (const row of tab.rows)
      t.innerHTML += '<tr>' + row.map(c => `<td>${c == null ? '—' : c}</td>`).join('') + '</tr>';
    body.appendChild(t);
    if (tab.note) body.appendChild(el('p', 'muted', tab.note));
  }
}
function evidenceTabFromDistributions(item, distributions, position) {
  const moveRows = (moves) => moves.map(m => [m.san || m.uci, m.games,
    `${(100 * m.white / Math.max(1, m.games)).toFixed(0)}/${(100 * m.draws / Math.max(1, m.games)).toFixed(0)}/${(100 * m.black / Math.max(1, m.games)).toFixed(0)}`]);
  return {
    masters: distributions.local2200 || [], ordinary: distributions.existing_local_lichess_cache
      || distributions.lichess || [], engine: item.engine || [], position,
  };
}
function wdl(row) {
  const total = Math.max(1, row.games);
  return `${(100*row.white/total).toFixed(0)}/${(100*row.draws/total).toFixed(0)}/${(100*row.black/total).toFixed(0)}`;
}
function boardTabs(positionKey) {
  if (!positionKey) return [];
  const load = async (which) => {
    const data = await (await fetch('/evidence?position_key=' + positionKey)).json();
    const dist = data.distributions || {};
    if (which === 'masters' || which === 'ordinary') {
      const rows = which === 'masters' ? (dist.local2200 || [])
        : (dist.existing_local_lichess_cache || dist.lichess || []);
      const total = rows.reduce((n, r) => n + r.games, 0);
      return {label: which === 'masters' ? 'Masters' : 'Ordinary players', kind: 'table',
        columns: ['move', 'games', 'W/D/L %'],
        rows: rows.map(r => [r.san || r.uci, r.games.toLocaleString(), wdl(r)]),
        note: `${rows.length} moves shown, ${total.toLocaleString()} games in total at this position.`};
    }
    const engine = data.engine || [];
    return {label: 'Engine', kind: 'table', columns: ['source', 'depth', 'multipv', 'principal variation'],
      rows: engine.map(e => [e.source, e.depth, e.multipv,
        ((JSON.parse(e.pvs_json || '[]')[0] || {}).uci) || '—']),
      note: engine.length ? 'MultiPV lines as stored by the research pipeline.'
        : 'no engine evaluation stored for this position'};
  };
  return [
    {label: 'Masters', kind: 'async', load: () => load('masters')},
    {label: 'Ordinary players', kind: 'async', load: () => load('ordinary')},
    {label: 'Engine', kind: 'async', load: () => load('engine')},
  ];
}

/* ---------------------------------------------------------------------- nav */
function renderNav() {
  const nav = document.getElementById('nav'); nav.innerHTML = '';
  const ns = el('h3', null, 'Course');
  nav.appendChild(ns);
  const sections = [
    {key:'overview', label:'Overview', count:null},
    {key:'structures', label:'Structures', count:structures().length},
    {key:'plans', label:'Plans & Setups', count:plans().length},
    {key:'critical', label:'Positions to Remember', count:decisions().length},
    {key:'deviations', label:'Deviations', count:groupedDeviations().length},
    {key:'review', label:'Review', count:null},
  ];
  for (const section of sections) {
    const box = el('div', 'section');
    const button = el('button', state.route.view === section.key ? 'active' : '',
      `<span>${section.label}</span>${section.count != null ? `<span class="count">${section.count}</span>` : ''}`);
    button.onclick = () => go(section.key, null);
    box.appendChild(button);
    if (state.route.view === section.key) {
      const list = el('div', 'items');
      const source = section.key === 'structures' ? structures()
        : section.key === 'plans' ? plans()
        : section.key === 'critical' ? decisions()
        : section.key === 'deviations' ? groupedDeviations().map(g => g.parent) : [];
      for (const item of source) {
        const b = el('button', (state.route.id === item.item_id ? 'active ' : '')
          + (state.seen.has(item.item_id) ? 'seen' : ''),
          `<span class="dot"></span><span>${shortTitle(item)}</span>`);
        b.onclick = () => go(section.key, item.item_id);
        list.appendChild(b);
      }
      if (list.children.length) box.appendChild(list);
    }
    nav.appendChild(box);
  }
  const total = items().length;
  const done = items().filter(i => state.seen.has(i.item_id)).length;
  const p = el('div', 'progress');
  p.innerHTML = `<div class="label"><span>Concepts seen</span><span>${done} / ${total}</span></div>
                 <div class="bar"><i style="width:${total ? (100*done/total) : 0}%"></i></div>`;
  nav.appendChild(p);
}

/* ------------------------------------------------------------------- router */
function go(view, id) {
  state.route = {view, id: id || null};
  location.hash = `#/${view}${id ? '/' + encodeURIComponent(id) : ''}`;
  if (id) state.seen.add(id);
  localStorage.setItem('scand-seen', JSON.stringify([...state.seen]));
  render();
}
function readHash() {
  const parts = (location.hash || '#/overview').replace(/^#\//, '').split('/');
  state.route = {view: parts[0] || 'overview', id: parts[1] ? decodeURIComponent(parts[1]) : null};
}
async function render() {
  renderNav();
  const main = document.getElementById('main'); const panel = document.getElementById('panel');
  main.innerHTML = ''; panel.innerHTML = '';
  const view = state.route.view;
  if (view === 'overview') return renderOverview(main, panel);
  if (view === 'structures') return renderStructure(main, panel, state.route.id || structures()[0]?.item_id);
  if (view === 'plans') return renderPlan(main, panel, state.route.id || richestPlan()?.item_id);
  if (view === 'critical') return renderDecision(main, panel, state.route.id || decisions()[0]?.item_id);
  if (view === 'deviations') return renderDeviation(main, panel, state.route.id || groupedDeviations()[0]?.parent.item_id);
  if (view === 'review') return renderReview(main, panel);
}

/* ----------------------------------------------------------------- overview */
function corridorPath() {
  /* the opening corridor, read from the flow graph: always take the highest-mass edge
     until a structure that is taught in the course is reached */
  const edges = ((state.flow && state.flow.edges) || []);
  const taught = new Set(structures().map(s => s.provenance.family));
  const byFrom = {};
  for (const edge of edges) (byFrom[edge.from] = byFrom[edge.from] || []).push(edge);
  const all = (state.flow && state.flow.structures) || [];
  const start = all.slice().sort((a, b) => (a.ply_mean || 0) - (b.ply_mean || 0))[0];
  if (!start) return [];
  const path = [{id: start.id, board: start.board, ply: start.ply_mean, mass: start.entry_mass}];
  let node = start.id;
  for (let i = 0; i < 4; i++) {
    if (taught.has(node)) break;
    const outgoing = (byFrom[node] || []).slice().sort((a, b) => b.mass - a.mass)[0];
    if (!outgoing) break;
    const meta = all.find(s => s.id === outgoing.to);
    path.push({id: outgoing.to, board: meta && meta.board, ply: meta ? meta.ply_mean : null,
               mass: outgoing.mass, move: outgoing.move});
    node = outgoing.to;
  }
  return path;
}
function renderOverview(main, panel) {
  document.getElementById('crumbs').innerHTML = '<b>Overview</b>';
  const wrap = el('div', 'map');
  const ns = structures().slice().sort((a,b) => (a.recognition.ply_mean||0)-(b.recognition.ply_mean||0));

  const corridor = corridorPath().filter(step => !ns.some(n => n.provenance.family === step.id));
  if (corridor.length) {
    const col = el('div', 'col');
    col.appendChild(el('div', 'col-label', corridor.length > 1 ? 'Opening → corridor' : 'Opening'));
    for (const step of corridor) {
      const node = el('div', 'node');
      if (step.board && step.board.fen) node.appendChild(miniBoard(step.board.fen));
      node.appendChild(el('div', 'name', 'Starting position'));
      node.appendChild(el('div', 'meta', step.move
        ? `…${step.move} — ${(step.mass*100).toFixed(0)}% of games carry on this way`
        : 'the first moves of the opening'));
      col.appendChild(node);
    }
    wrap.appendChild(col);
  }

  const mature = el('div', 'col');
  mature.appendChild(el('div', 'col-label', 'Mature structures you will learn'));
  const grid = el('div', 'node-grid');
  for (const item of ns) {
    const node = el('div', 'node');
    node.appendChild(miniBoard(item.representative_board.fen));
    node.appendChild(el('div', 'name', shortTitle(item)));
    node.appendChild(el('div', 'meta',
      `${(item.flow_mass*100).toFixed(0)}% of games reach it · usually by move ${Math.round((item.recognition.ply_mean||0)/2)}`));
    node.onclick = () => go('structures', item.item_id);
    grid.appendChild(node);
  }
  mature.appendChild(grid);
  wrap.appendChild(mature);

  const edgeCol = el('div', 'col');
  edgeCol.appendChild(el('div', 'col-label', 'Where games go next'));
  const edges = ((state.flow && state.flow.edges) || [])
    .filter(e => ns.some(n => n.provenance.family === e.from) && ns.some(n => n.provenance.family === e.to))
    .sort((a,b) => b.mass - a.mass).slice(0, 6);
  const edgesBox = el('div', 'edges');
  for (const edge of edges) {
    const from = ns.find(n => n.provenance.family === edge.from);
    const to = ns.find(n => n.provenance.family === edge.to);
    const row = el('div', 'row');
    row.innerHTML = `<span class="bar" style="width:${Math.max(10, edge.mass*220)}px"></span>
      <span>${shortTitle(from)} <span class="muted">→</span> ${shortTitle(to)}
      <span class="muted">${(edge.mass*100).toFixed(0)}%</span></span>`;
    edgesBox.appendChild(row);
  }
  edgeCol.appendChild(edgesBox);
  wrap.appendChild(edgeCol);
  main.appendChild(wrap);
  main.appendChild(el('div', 'muted',
    'Structures that reconverge are reached through more than one route; click any board to open its lesson.'));

  panel.appendChild(el('div', 'kicker', 'The opening'));
  panel.appendChild(el('h2', null, 'What this course covers'));
  panel.appendChild(el('p', 'lead',
    `${structures().length} structures, ${plans().length} plans and ${decisions().length} positions to remember — `
    + 'everything below was generated from how strong players and ordinary players actually play '
    + 'this opening.'));
  panel.appendChild(el('h4', null, 'How to use it'));
  const rows = el('ul', 'rows');
  for (const [title, text] of [
    ['Structures', 'Learn to recognise where you are. No moves to memorise.'],
    ['Plans & Setups', 'The centrepiece: what you are trying to achieve and how your pieces get there.'],
    ['Positions to Remember', 'The few exact positions where a specific move matters.'],
    ['Deviations', 'What changes when the opponent plays something else.'],
    ['Review', 'Short questions drawn from this same material.'],
  ]) rows.appendChild(el('li', null, `<b>${title}</b> <span class="muted">${text}</span>`));
  panel.appendChild(rows);
}

function miniBoard(fen) {
  const wrap = el('div', 'mini');
  const pieces = parseFen(fen);
  for (let r = 0; r < 8; r++) for (let f = 0; f < 8; f++) {
    const name = FILES[f] + (8 - r);
    const cell = el('div', ((r + f) % 2) ? 'dark' : 'light');
    if (pieces[name]) cell.appendChild(pieceImage(pieceCode(pieces[name])));
    wrap.appendChild(cell);
  }
  return wrap;
}

/* ---------------------------------------------------------------- structure */
function renderStructure(main, panel, itemId) {
  const item = structures().find(i => i.item_id === itemId) || structures()[0];
  if (!item) { main.appendChild(el('div','empty','No structures in this budget.')); return; }
  document.getElementById('crumbs').innerHTML = `<b>Structures</b> · ${shortTitle(item)}`;
  const fen = item.representative_board.fen;
  const highlights = {};
  const skeleton = (item.recognition.look_for.pawn_structure || {});
  for (const sq of decodeBitmask(skeleton.white)) highlights[sq] = 'pawn';
  for (const sq of decodeBitmask(skeleton.black)) highlights[sq] = 'pawn';
  const occupancy = item.recognition.look_for.recurring_occupancy || [];
  const characteristic = [], flexible = [];
  for (const row of occupancy) {
    const squares = row.squares || [];
    if (squares[0] && squares[0].share >= 0.6) {
      characteristic.push({piece: row.piece, square: squares[0].square, share: squares[0].share});
      if (squares[0].square && highlights[squares[0].square] !== 'pawn')
        highlights[squares[0].square] = 'piece';
    }
    for (const alt of squares.slice(1))
      if (alt.share >= 0.08 && alt.share < 0.6) flexible.push({piece: row.piece, square: alt.square, share: alt.share});
  }
  for (const alt of flexible.slice(0, 8))
    if (!highlights[alt.square]) highlights[alt.square] = 'flexible';

  const area = el('div', 'board-area');
  area.appendChild(drawBoard({fen, highlights,
    badge: 'representative position', caption: null}));
  area.appendChild(el('div', 'legend',
    '<span><i class="pawn"></i> defining pawns</span>' +
    '<span><i class="piece"></i> characteristic piece</span>' +
    '<span><i class="flex"></i> flexible</span>'));
  const controls = el('div', 'board-controls');
  const evidence = el('button', 'btn', 'Show evidence');
  evidence.onclick = () => openEvidence(structureEvidence(item), 'Why this structure', item);
  controls.appendChild(evidence);
  area.appendChild(controls);
  main.appendChild(area);

  panel.appendChild(el('div', 'kicker', 'Recognise the position'));
  panel.appendChild(el('h2', null, shortTitle(item)));
  panel.appendChild(el('p', 'lead', `About ${(item.flow_mass*100).toFixed(0)}% of games reach this structure. `
    + 'Nothing here needs memorising — you only need to know it when you see it.'));

  panel.appendChild(el('h4', null, 'Recognise this'));
  const facts = el('ul', 'rows');
  for (const row of (item.distinctive || []).slice(0, 4)) {
    const [code, , square] = row.fact.split(' ');
    const black = code[0] === 'b';
    const label = code[1] === 'p' ? 'pawn' : pieceName(code[1].toUpperCase()).toLowerCase();
    const frequency = row.present_in_family >= 0.95 ? 'always here' : 'usually here';
    facts.appendChild(el('li', null, `<b>${black ? "Black" : "White"}'s ${label} on ${square}</b>`
      + `<span class="meta">${frequency}</span>`));
  }
  panel.appendChild(facts);

  if (flexible.length) {
    panel.appendChild(el('h4', null, 'Usually flexible'));
    const list = el('ul', 'rows');
    for (const row of flexible.slice(0, 5))
      list.appendChild(el('li', null, `${row.piece} — ${row.square} <span class="meta">often</span>`));
    panel.appendChild(list);
  }

  const related = plans().filter(p => (p.applicability.families || []).includes(item.provenance.family));
  panel.appendChild(el('h4', null, 'Common continuation'));
  if (related.length) {
    const links = el('div', 'links');
    for (const plan of related.slice(0, 4)) {
      const chip = el('button', 'chip', shortTitle(plan));
      chip.onclick = () => go('plans', plan.item_id);
      links.appendChild(chip);
    }
    panel.appendChild(links);
  } else panel.appendChild(el('p', 'muted', 'No plan in this budget applies here yet.'));
}

function structureEvidence(item) {
  const look = item.recognition.look_for || {};
  return [
    {label: 'Recognition', kind: 'table', columns: ['piece', 'most likely square', 'share'],
      rows: (look.recurring_occupancy || []).map(r => [r.piece, r.squares[0]?.square,
        r.squares[0] ? (r.squares[0].share * 100).toFixed(0) + '%' : '—'])},
    {label: 'Vs alternatives', kind: 'table', columns: ['fact', 'here', 'elsewhere', 'lift'],
      rows: (item.distinctive || []).map(d => [d.fact, (d.present_in_family*100).toFixed(0)+'%',
        (d.present_elsewhere*100).toFixed(0)+'%', d.lift.toFixed(2)]),
      note: 'Suppressed as non-distinguishing: ' + (item.suppressed_facts || []).join(', ')},
    ...boardTabs(item.representative_board.position_key),
    {label: 'Selection', kind: 'json', data: item.provenance},
  ];
}

/* --------------------------------------------------------------------- plan */
async function renderPlan(main, panel, itemId) {
  const item = plans().find(i => i.item_id === itemId) || plans()[0];
  if (!item) { main.appendChild(el('div','empty','No plans in this budget.')); return; }
  document.getElementById('crumbs').innerHTML = `<b>Plans &amp; Setups</b> · ${shortTitle(item)}`;
  panel.appendChild(el('div', 'kicker', 'Plan'));
  panel.appendChild(el('h2', null, shortTitle(item)));
  panel.appendChild(el('div', 'muted', 'Loading the board…'));
  state.seen.add(item.item_id);

  const boardKeys = Object.keys(item.boards_map || {});
  const candidates = boardKeys.slice(0, 3);
  const fetched = await Promise.all(candidates.map(async key => {
    const data = await (await fetch('/evidence?position_key=' + key)).json();
    return {key, fen: data.position ? data.position.fen : null};
  }));
  const valid = fetched.filter(row => row.fen);
  const scored = valid.map(row => ({...row,
    pending: item.transformations.required.filter(t => transformationState(t.component, row.fen) === 'pending').length
      + item.transformations.optional.filter(t => transformationState(t.component, row.fen) === 'pending').length}));
  const chosen = (scored.sort((a,b)=>b.pending-a.pending)[0]) || valid[0] || candidates[0] && {key:candidates[0], fen:null};
  if (!chosen || !chosen.fen) { panel.innerHTML = '<p class="muted">Could not load a board for this plan.</p>'; return; }
  let fen = chosen.fen;
  let currentPositionKey = chosen.key;
  panel.innerHTML = '';

  const required = item.transformations.required || [];
  const optional = item.transformations.optional || [];
  const consequences = item.transformations.expected_consequence || [];

  const area = el('div', 'board-area');
  const boardHolder = el('div');
  const checklist = el('div', 'checklist');
  const controls = el('div', 'board-controls');
  const stepLabel = el('span', 'muted', '');
  let orderIndex = 0, orders = null, step = 0, inExample = false, exampleLength = 0;

  function arrowsFor(fenNow, highlightComponent) {
    const arrows = [];
    for (const t of required) {
      const arrow = arrowFor(t.component, fenNow);
      if (arrow) arrows.push({...arrow,
        done: transformationState(t.component, fenNow) === 'done',
        emphasise: highlightComponent && sameComponent(t.component, highlightComponent)});
    }
    for (const t of optional) {
      const arrow = arrowFor(t.component, fenNow);
      if (arrow) arrows.push({...arrow, done: transformationState(t.component, fenNow) === 'done',
        style: 'optional'});
    }
    return arrows;
  }
  function sameComponent(a, b) { return a.length === b.length && a.every((v, i) => v === b[i]); }

  function redraw(highlightComponent) {
    boardHolder.innerHTML = '';
    boardHolder.appendChild(drawBoard({fen,
      arrows: arrowsFor(fen, highlightComponent),
      badge: inExample ? `move ${step + 1} of ${exampleLength}` : 'one position this plan applies to'}));
    checklist.innerHTML = '';
    const rows = el('ul', 'rows');
    for (const t of required) {
      const st = transformationState(t.component, fen);
      const li = el('li', 'clickable',
        `<span class="tick ${st === 'done' ? 'done' : 'todo'}">${st === 'done' ? '✓' : '○'}</span>` +
        `<span>${translateTitle(t.text).replace(/ to /, ' to ')}</span>`);
      li.onmouseenter = () => redraw(t.component);
      li.onmouseleave = () => redraw(null);
      rows.appendChild(li);
    }
    for (const t of optional) {
      const st = transformationState(t.component, fen);
      rows.appendChild(el('li', null,
        `<span class="tick ${st === 'done' ? 'done' : 'todo'}">${st === 'done' ? '✓' : '○'}</span>` +
        `<span class="muted">often: ${translateTitle(t.text)}</span>`));
    }
    checklist.appendChild(el('h4', null, 'Plan'));
    checklist.appendChild(rows);
  }
  redraw(null);

  const showExample = el('button', 'btn primary', 'Show example');
  const another = el('button', 'btn', 'Another move order');
  const prev = el('button', 'btn small', '‹');
  const next = el('button', 'btn small', '›');
  controls.appendChild(showExample); controls.appendChild(another);
  controls.appendChild(prev); controls.appendChild(next); controls.appendChild(stepLabel);

  async function loadOrders() {
    if (!orders) {
      const family = (item.applicability.families || [])[0];
      orders = family ? (await (await fetch('/orders?family=' + encodeURIComponent(family))).json()).orders : [];
    }
    return orders || [];
  }
  showExample.onclick = async () => {
    const list = await loadOrders();
    if (!list.length) { stepLabel.textContent = 'no example line available'; return; }
    step = 0; inExample = true; exampleLength = list[orderIndex].moves.length;
    fen = list[orderIndex].moves[0].fen; redraw(null);
    stepLabel.textContent = `${list[orderIndex].label} · step 1 of ${exampleLength}`;
  };
  function stepBy(delta) {
    const list = orders || [];
    if (!list.length) return;
    const moves = list[orderIndex].moves;
    inExample = true; exampleLength = moves.length;
    step = Math.max(0, Math.min(moves.length - 1, step + delta));
    fen = moves[step].fen; redraw(null);
    stepLabel.textContent = `${list[orderIndex].label} · step ${step + 1} of ${moves.length} · ${moves[step].uci}`;
  }
  prev.onclick = async () => { await loadOrders(); stepBy(-1); };
  next.onclick = async () => { await loadOrders(); stepBy(1); };
  another.onclick = async () => {
    const list = await loadOrders();
    if (!list.length) return;
    orderIndex = (orderIndex + 1) % list.length; step = 0;
    inExample = true; exampleLength = list[orderIndex].moves.length;
    fen = list[orderIndex].moves[0].fen; redraw(null);
    stepLabel.textContent = `${list[orderIndex].label} · step 1 of ${list[orderIndex].moves.length}`;
  };

  area.appendChild(boardHolder);
  area.appendChild(controls);
  area.appendChild(el('div', 'legend',
    '<span><i class="arrow"></i> to do</span>' +
    '<span><i class="piece"></i> already done (dashed)</span>' +
    '<span><i style="background:#d9b25f"></i> often, not required</span>'));
  main.appendChild(area);
  main.appendChild(checklist);

  panel.appendChild(el('p', 'lead',
    `Applies to ${item.applicability.boards} positions in this course. `
    + 'Positions differ, so the arrows show what is still to be done wherever you start from.'));
  const ordering = item.ordering || {};
  panel.appendChild(el('h4', null, 'Order'));
  panel.appendChild(el('p', null, ordering.constrained
    ? 'These moves usually happen in a fixed order.'
    : 'Flexible order — ' + required.map(t => translateTitle(t.text)).join(', ')
      + ' commonly occur in different orders.'));
  if (consequences.length) {
    panel.appendChild(el('h4', null, 'Usually leads to'));
    const list = el('ul', 'rows');
    for (const c of consequences) list.appendChild(el('li', null, c.text));
    panel.appendChild(list);
  }
  if (item.retained_because) panel.appendChild(el('div', 'note', item.retained_because));
  const evidence = el('button', 'btn', 'Why am I being taught this?');
  evidence.onclick = () => openEvidence(planEvidence(item, currentPositionKey), shortTitle(item), item);
  panel.appendChild(evidence);
  panel.appendChild(el('h4', null, 'Related'));
  const links = el('div', 'links');
  for (const family of (item.applicability.families || []).slice(0, 3)) {
    const structure = structures().find(s => s.provenance.family === family);
    if (!structure) continue;
    const chip = el('button', 'chip', shortTitle(structure));
    chip.onclick = () => go('structures', structure.item_id);
    links.appendChild(chip);
  }
  panel.appendChild(links);
}

function planEvidence(item, positionKey) {
  const pending = (item.transformations.required || []).length;
  const tabs = [
    {label: 'Selection', kind: 'html', html:
      `<p><b>Why this plan:</b> it covers ${item.positions} positions with flow mass `
      + `${item.flow_mass.toFixed(4)} and cost ${item.marginal_burden ?? item.burden} marginal units.</p>`
      + (item.retained_because ? `<p>${item.retained_because}</p>` : '')
      + `<p class="muted">Ordering: flexibility ${item.ordering.flexibility?.toFixed(2)}, `
      + `${item.coherence.distinct_orderings} distinct orders, entropy ${item.coherence.ordering_entropy_bits.toFixed(2)} bits, `
      + `${item.coherence.conditional_branches} conditional branches.</p>`},
    {label: 'Transformation detail', kind: 'table', columns: ['transformation', 'class', 'state'],
      rows: (item.transformations.required || []).map(t => [translateTitle(t.text), 'required',
        transformationState(t.component, state.board.fen || '')])
        .concat((item.transformations.optional || []).map(t => [translateTitle(t.text), 'common', '—']))
        .concat((item.transformations.expected_consequence || []).map(t => [t.text, 'effect', 'not testable']))},
    {label: 'Underlying boards', kind: 'json', data: Object.keys(item.boards_map || {})},
    ...boardTabs(positionKey),
    {label: 'Provenance', kind: 'json', data: item.provenance},
  ];
  return tabs;
}

/* ------------------------------------------------------------ critical move */
function renderDecision(main, panel, itemId) {
  const item = decisions().find(i => i.item_id === itemId) || decisions()[0];
  if (!item) { main.appendChild(el('div', 'empty',
    'No memorisation positions at this budget — the plans already cover them.')); return; }
  document.getElementById('crumbs').innerHTML = '<b>Positions to Remember</b>';
  state.seen.add(item.item_id);
  const fen = item.board.fen;
  const recommended = item.prescribes.moves[0];
  let chosen = null;

  const area = el('div', 'board-area');
  const boardHolder = el('div');
  const feedback = el('div', 'feedback');
  const controls = el('div', 'board-controls');

  function redraw() {
    boardHolder.innerHTML = '';
    const pieces = parseFen(fen);
    const from = chosen;
    const targets = chosen ? legalTargetsFor(fen, chosen) : [];
    boardHolder.appendChild(drawBoard({fen, targets,
      arrows: chosen && state.board.target ? [{from: chosen, to: state.board.target}] : [],
      badge: 'your move to find', onSquare: onSquare}));
  }
  function legalTargetsFor(fen, square) {
    /* no rules engine here: offer squares reachable by the chosen piece in a straight
       line or a knight hop, so the learner can express any move they have in mind */
    const pieces = parseFen(fen); const piece = pieces[square];
    if (!piece) return [];
    const out = [];
    const files = FILES.indexOf(square[0]); const rank = Number(square[1]);
    for (const [df, dr] of [[1,0],[-1,0],[0,1],[0,-1],[1,1],[1,-1],[-1,1],[-1,-1]]){
      for (let k=1;k<8;k++){
        const f=files+df*k, r=rank+dr*k;
        if (f<0||f>7||r<1||r>8) break;
        out.push(FILES[f]+r);
      }}
    for (const [df, dr] of [[1,2],[2,1],[-1,2],[-2,1],[1,-2],[2,-1],[-1,-2],[-2,-1]]){
      const f=files+df, r=rank+dr;
      if (f>=0&&f<=7&&r>=1&&r<=8) out.push(FILES[f]+r);
    }
    return out;
  }
  function onSquare(square) {
    if (chosen && state.board.target) return;
    if (chosen && !state.board.target) { state.board.target = square; redraw(); reveal(); return; }
    if (parseFen(fen)[square]) {
      chosen = square; state.board.selected = square; state.board.target = null; redraw();
    }
  }
  function reveal() {
    const move = chosen && state.board.target ? chosen + state.board.target : null;
    const right = move === recommended;
    feedback.innerHTML = '';
    feedback.appendChild(el('h4', null, move === recommended ? 'Correct' : 'Not quite'));
    const rows = el('ul', 'rows');
    rows.appendChild(el('li', null, `<span class="tick done">✓</span> Your move: <b>${move || '—'}</b>`));
    rows.appendChild(el('li', null, `<span class="tick done">✓</span> Worth remembering: <b>${recommended}</b>`));
    feedback.appendChild(rows);
    feedback.appendChild(el('p', null,
      `The plans in this course still left ${item.why_admitted.residual_before.toFixed(4)} of value `
      + `on this position, and this move removes ${item.why_admitted.value_gained.toFixed(4)} of it `
      + `(${(item.why_admitted.share_of_residual*100).toFixed(0)}% of what was left).`));
    const owner = plans().find(p => (p.boards_map || {})[item.board.position_key]);
    if (owner) {
      const chip = el('button', 'chip', 'Plan: ' + shortTitle(owner));
      chip.onclick = () => go('plans', owner.item_id);
      feedback.appendChild(chip);
    }
    const ev = el('button', 'btn', 'Why am I being taught this?');
    ev.onclick = () => openEvidence(criticalEvidence(item, owner), 'Why remember this', item);
    feedback.appendChild(ev);
  }
  redraw();
  controls.appendChild(el('span', 'muted', 'Click the piece, then its destination.'));
  area.appendChild(boardHolder); area.appendChild(controls); area.appendChild(feedback);
  main.appendChild(area);

  panel.appendChild(el('div', 'kicker', 'Position to remember'));
  panel.appendChild(el('h2', null, 'This position is worth remembering'));
  panel.appendChild(el('p', 'lead',
    'Unlike the plans above, here a single specific move matters — pick it on the board first, '
    + 'then check yourself.'));
  panel.appendChild(el('h4', null, 'Common human move'));
  const common = el('p', 'muted', 'loading…');
  panel.appendChild(common);
  fetch('/evidence?position_key=' + item.board.position_key).then(r => r.json()).then(data => {
    const rows = ((data.distributions && data.distributions.local2200) || []).slice(0, 3);
    common.innerHTML = rows.length
      ? rows.map(m => `<b>${m.san || m.uci}</b> <span class="muted">${m.games} games</span>`).join(' · ')
      : 'no strong-play moves recorded at this position';
  });
}

function criticalEvidence(item, owner) {
  const tabs = [
    {label: 'Why here', kind: 'html', html:
      `<p>Exact move: <b>${item.prescribes.moves[0]}</b>.</p>`
      + `<p>Left over after the plans: ${item.why_admitted.residual_before.toFixed(5)}; `
      + `this move removes ${item.why_admitted.value_gained.toFixed(5)}.</p>`
      + `<p class="muted">Admission rule: ${item.why_admitted.rule}.</p>`
      + (owner ? `<p>General plan that applies here: ${owner.title}.</p>` : '')},
    ...boardTabs(item.board.position_key),
    {label: 'Provenance', kind: 'json', data: item.provenance},
    {label: 'Board key', kind: 'json', data: item.board.position_key},
  ];
  return tabs;
}

/* -------------------------------------------------------------- deviations */
function groupedDeviations() {
  const groups = {};
  for (const item of exceptions()) {
    if (item.kind !== 'exception') continue;
    const parentId = item.modifies;
    groups[parentId] = groups[parentId] || {parent: null, items: []};
    groups[parentId].items.push(item);
  }
  const out = [];
  for (const [parentId, entry] of Object.entries(groups)) {
    const parent = items().find(i => i.item_id === parentId);
    if (!parent) continue;
    out.push({parent, items: sortedBySeverity(entry.items),
      occurrences: entry.items.reduce((n, i) => n + (i.occurrences || 1), 0)});
  }
  return out.sort((a, b) => b.occurrences - a.occurrences);
}
function renderDeviation(main, panel, parentId) {
  const groups = groupedDeviations();
  const group = groups.find(g => g.parent.item_id === parentId) || groups[0];
  if (!group) { main.appendChild(el('div', 'empty', 'No deviations at this budget.')); return; }
  document.getElementById('crumbs').innerHTML = `<b>Deviations</b> · under ${shortTitle(group.parent)}`;
  let expanded = false;
  let current = group.items[0];

  const area = el('div', 'board-area');
  const boardHolder = el('div');
  function redraw() {
    boardHolder.innerHTML = '';
    const fen = current.board.fen;
    const pieces = parseFen(fen);
    const arrow = {to: null};
    boardHolder.appendChild(drawBoard({fen, badge: 'what happens instead',
      arrows: [], highlights: {}}));
  }
  redraw();
  area.appendChild(boardHolder);
  main.appendChild(area);
  main.appendChild(el('div', 'muted', 'The board shows the position where the plan changes.'));

  panel.appendChild(el('div', 'kicker', 'Deviation'));
  panel.appendChild(el('h2', null, `If the opponent plays it differently`));
  panel.appendChild(el('p', 'lead', `Under the plan <b>${shortTitle(group.parent)}</b>, `
    + `${group.items.length} position${group.items.length === 1 ? '' : 's'} in this budget `
    + 'change what you should do.'));
  const list = el('ul', 'rows');
  const shown = expanded ? group.items : group.items.slice(0, 3);
  for (const item of shown) {
    const li = el('li', 'clickable' + (item === current ? ' active' : ''),
      `<span>Instead of the usual move, <b>${item.prescribes.moves[0]}</b> is the common choice</span>`
      + `<span class="meta">${(item.why_selected.observed_share*100).toFixed(0)}% of games</span>`);
    li.onclick = () => { current = item; renderDeviation(main, panel, parentId); };
    list.appendChild(li);
  }
  panel.appendChild(list);
  if (group.items.length > 3) {
    const more = el('button', 'btn', expanded ? 'Fewer deviations' : `More deviations (${group.items.length - 3})`);
    more.onclick = () => { expanded = !expanded; renderDeviation(main, panel, parentId); };
    panel.appendChild(more);
  }
  panel.appendChild(el('h4', null, 'What changes'));
  panel.appendChild(el('p', null,
    `The usual plan covers only ${(current.why_selected.schema_share_of_strong_play*100).toFixed(0)}% `
    + `of what is played here, and the common move is <b>${current.prescribes.moves[0]}</b> `
    + `(${(current.why_selected.observed_share*100).toFixed(0)}% of games). `
    + 'So this is where the plan is not enough on its own.'));
  const ev = el('button', 'btn', 'Why am I being taught this?');
  ev.onclick = () => openEvidence([
    {label: 'Why kept', kind: 'html', html:
      `<p>Kept because it changes the recommended move and is both dominant and reached.</p>`
      + `<p>Plan coverage here: ${(current.why_selected.schema_share_of_strong_play*100).toFixed(0)}%; `
      + `threshold ${(current.why_selected.threshold*100).toFixed(0)}%.</p>`
      + `<p>Differs from the plan's moves: ${(current.why_selected.differs_from_prescription||[]).join(', ')}</p>`},
    ...boardTabs(current.board.position_key),
    {label: 'Provenance', kind: 'json', data: current.provenance},
  ], 'Why this deviation', current);
  panel.appendChild(ev);
}

/* ------------------------------------------------------------------- review */
function renderReview(main, panel) {
  document.getElementById('crumbs').innerHTML = '<b>Review</b>';
  const box = el('div', 'qbox');
  main.appendChild(box);
  panel.appendChild(el('div', 'kicker', 'Review'));
  panel.appendChild(el('h2', null, 'Check yourself'));
  panel.appendChild(el('p', null, 'Questions are generated from the same course material — '
    + 'a structure to recognise, a plan to recall, or a position where one move matters.'));
  const score = el('div', 'score', '');
  panel.appendChild(score);

  const types = ['recognition', 'plan', 'decision'];
  const type = types[Math.floor(Math.random() * types.length)];
  if (type === 'recognition') {
    const all = structures();
    const answer = all[Math.floor(Math.random() * all.length)];
    const options = shuffle([answer, ...shuffle(all.filter(s => s !== answer)).slice(0, 3)]);
    box.appendChild(el('div', 'tag', 'Recognition'));
    box.appendChild(el('div', 'prompt', 'Which structure is this?'));
    box.appendChild(drawBoard({fen: answer.representative_board.fen}));
    const choices = el('div', 'choices');
    for (const option of options) {
      const b = el('button', 'choice', shortTitle(option));
      b.onclick = () => {
        [...choices.children].forEach(c => c.disabled = true);
        b.classList.add(option === answer ? 'correct' : 'wrong');
        if (option !== answer) [...choices.children].forEach(c => {
          if (c.textContent === shortTitle(answer)) c.classList.add('correct'); });
        panel.appendChild(el('p', 'muted', option === answer
          ? 'Correct — that is the recurring structure.' :
          `It is ${shortTitle(answer)}. The tell is the pawn placement, not the pieces.`));
      };
      choices.appendChild(b);
    }
    box.appendChild(choices);
  } else if (type === 'plan') {
    const all = plans().filter(p => Object.keys(p.boards_map || {}).length);
    const answer = all[Math.floor(Math.random() * all.length)];
    const key = Object.keys(answer.boards_map)[0];
    box.appendChild(el('div', 'tag', 'Plan recall'));
    box.appendChild(el('div', 'prompt', 'What still needs doing here?'));
    const holder = el('div', null, 'loading…');
    box.appendChild(holder);
    fetch('/evidence?position_key=' + key).then(r => r.json()).then(data => {
      const fen = data.position.fen;
      holder.innerHTML = ''; holder.appendChild(drawBoard({fen}));
      const pending = answer.transformations.required
        .filter(t => transformationState(t.component, fen) === 'pending')
        .map(t => translateTitle(t.text));
      const correct = pending.length ? pending : answer.transformations.required.map(t => translateTitle(t.text));
      const distractors = shuffle(plans().filter(p => p !== answer)).slice(0, 3)
        .map(p => p.transformations.required.map(t => translateTitle(t.text)));
      const choices = el('div', 'choices');
      for (const option of shuffle([correct, ...distractors])) {
        const b = el('button', 'choice', option.join(' + ') || 'nothing left to do');
        b.onclick = () => {
          [...choices.children].forEach(c => c.disabled = true);
          b.classList.add(option === correct ? 'correct' : 'wrong');
          if (option !== correct) [...choices.children].forEach(c => {
            if (c.textContent === (correct.join(' + ') || 'nothing left to do')) c.classList.add('correct'); });
        };
        choices.appendChild(b);
      }
      box.appendChild(choices);
      const reveal = el('button', 'btn small', 'Show the plan');
      reveal.onclick = () => go('plans', answer.item_id);
      box.appendChild(reveal);
    });
  } else {
    const all = decisions();
    if (!all.length) { box.appendChild(el('div', 'muted', 'No memorisation positions at this budget.')); return; }
    const answer = all[Math.floor(Math.random() * all.length)];
    box.appendChild(el('div', 'tag', 'Exact decision'));
    box.appendChild(el('div', 'prompt', 'What would you play here?'));
    const choices = el('div', 'choices');
    const moves = shuffle([answer.prescribes.moves[0], ...['e4','d4','c4','g3']
      .map((pre, i) => pre + (5 + i))]).slice(0, 4);
    for (const move of moves) {
      const b = el('button', 'choice', move);
      b.onclick = () => {
        [...choices.children].forEach(c => c.disabled = true);
        b.classList.add(move === answer.prescribes.moves[0] ? 'correct' : 'wrong');
      };
      choices.appendChild(b);
    }
    box.appendChild(drawBoard({fen: answer.board.fen}));
    box.appendChild(choices);
    const reveal = el('button', 'btn small', 'Open this position');
    reveal.onclick = () => go('critical', answer.item_id);
    box.appendChild(reveal);
  }
  score.textContent = 'Answers are scored by the lesson pages themselves; this is a quick check, not a test.';
}
function shuffle(list) { return list.slice().sort(() => Math.random() - 0.5); }

/* --------------------------------------------------------------------- boot */
function budgetSwitch() {
  const holder = document.getElementById('budget-switch'); holder.innerHTML = '';
  const labels = {10: 'Essentials', 25: 'Full course', 50: 'Extended'};
  for (const b of state.course.budgets) {
    const key = String(b);
    const btn = el('button', 'btn small' + (key === state.budget ? ' primary' : ''),
      `${labels[key] || key + ' units'}`);
    btn.title = `${b} learning units`;
    btn.onclick = () => { state.budget = key; localStorage.setItem('scand-budget', key);
      budgetSwitch(); render(); };
    holder.appendChild(btn);
  }
}
async function boot() {
  document.head.appendChild(highlightStyles());
  state.course = await (await fetch('/course.json')).json();
  state.budget = localStorage.getItem('scand-budget') || '25';
  if (!state.course.courses[state.budget]) state.budget = '25';
  readHash();
  window.addEventListener('hashchange', () => { readHash(); render(); });
  document.getElementById('drawer-close').onclick = closeEvidence;
  document.getElementById('scrim').onclick = closeEvidence;
  document.getElementById('evidence-button').onclick = () => {
    const current = currentItem();
    if (current) openEvidence(defaultEvidence(current), 'Why this is taught', current);
  };
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeEvidence();
  });
  try { state.flow = await (await fetch('/flow')).json(); } catch (e) { state.flow = {edges:[]}; }
  // give the memorisation positions learner-facing names, derived from their structure
  state.decisionLabels = {};
  await Promise.all(decisions().map(async (item, index) => {
    try {
      const data = await (await fetch('/evidence?position_key=' + item.board.position_key)).json();
      const structureId = data.position && data.position.structure_id;
      const match = structures().find(s => s.provenance.family === structureId);
      state.decisionLabels[item.item_id] = match
        ? `Position in ${structureLabel(match)}`
        : `Position to remember ${index + 1}`;
    } catch (e) { state.decisionLabels[item.item_id] = `Position to remember ${index + 1}`; }
  }));
  if (decisions().length) renderNav();
  budgetSwitch();
  document.getElementById('budget-note').textContent =
    `${items().length} concepts · ${course().report.flow_coverage ? (course().report.flow_coverage*100).toFixed(0) + '% of games' : ''}`;
  render();
}
function currentItem() {
  const {view, id} = state.route;
  const pool = view === 'structures' ? structures() : view === 'plans' ? plans()
    : view === 'critical' ? decisions() : [];
  return pool.find(i => i.item_id === id) || pool[0] || null;
}
function defaultEvidence(item) {
  if (item.kind === 'orientation') return structureEvidence(item);
  if (item.kind === 'schema') return planEvidence(item, Object.keys(item.boards_map || {})[0]);
  if (item.kind === 'decision') return criticalEvidence(item, null);
  return [{label: 'Provenance', kind: 'json', data: item.provenance}];
}
boot();
