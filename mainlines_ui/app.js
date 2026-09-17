'use strict';
/* Opening mainlines viewer.
   One entry per POSITION, not per taxonomy line: 1,466 named openings collapse to 668
   entries, each keeping every ECO code and alias. FENs are precomputed, so no chess
   engine is needed here. */

const FILES = 'abcdefgh';
const START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -';
const state = { data: null, sel: null, ply: 0, q: '', eco: '', mainsOnly: false,
                withMirror: false, open: new Set() };

const $ = id => document.getElementById(id);
const el = (tag, cls) => { const n = document.createElement(tag); if (cls) n.className = cls; return n; };
const num = n => (n || 0).toLocaleString();

/* ------------------------------------------------------------------ pieces */
function placement(fen) {
  const out = [];
  for (const row of fen.split(' ')[0].split('/')) {
    for (const ch of row) {
      if (ch >= '1' && ch <= '8') { for (let i = 0; i < +ch; i++) out.push(null); }
      else out.push(ch);
    }
  }
  return out;
}

function renderBoard() {
  const g = state.sel;
  const grid = $('grid');
  grid.textContent = '';
  const squares = placement(g && state.ply > 0 ? g.line[state.ply - 1].fen : START_FEN);
  const last = g && state.ply > 0 ? g.line[state.ply - 1].uci : null;
  const from = last ? last.slice(0, 2) : null;
  const to = last ? last.slice(2, 4) : null;

  for (let r = 0; r < 8; r++) {
    for (let c = 0; c < 8; c++) {
      const sq = el('div', 'sq ' + ((r + c) % 2 === 0 ? 'light' : 'dark'));
      const name = FILES[c] + (8 - r);
      if (name === from || name === to) sq.classList.add('last');
      const p = squares[r * 8 + c];
      if (p) {
        const img = el('img', 'piece');
        img.src = `/ui/piece-alpha-${(p === p.toUpperCase() ? 'w' : 'b') + p.toUpperCase()}.svg`;
        img.alt = p;
        img.draggable = false;
        img.onerror = () => { img.onerror = null; img.replaceWith(document.createTextNode(p)); };
        sq.appendChild(img);
      }
      if (r === 7 && g) { const cd = el('div', 'coord'); cd.textContent = FILES[c]; sq.appendChild(cd); }
      if (c === 0 && g) { const cd = el('div', 'coord'); cd.textContent = String(8 - r); sq.appendChild(cd); }
      grid.appendChild(sq);
    }
  }
  $('ply-badge').textContent = g
    ? `${state.ply}/${g.line.length} plies · ${state.ply ? g.line[state.ply - 1].san : 'start'}`
    : '';
  $('plynote').textContent = g
    ? (state.ply === g.line.length ? 'end of mainline'
       : `click ▶ for ${g.line[state.ply] ? g.line[state.ply].san : ''}`)
    : '';
  for (const b of ['first', 'prev', 'next', 'last']) $(b).disabled = !g;
}

/* ------------------------------------------------------------------- moves */
function renderMoves() {
  const g = state.sel;
  const box = $('moves');
  box.textContent = '';
  if (!g) return;
  let pair = null;
  g.line.forEach((m, i) => {
    if (i % 2 === 0) {
      pair = el('span');
      const no = el('span', 'no'); no.textContent = (i / 2 + 1) + '.';
      pair.appendChild(no); box.appendChild(pair);
    }
    const s = el('span', 'mv' + (i + 1 === state.ply ? ' cur' : '') + (m.tax ? '' : ' ext'));
    s.textContent = m.san;
    s.title = `ply ${m.ply} · ${m.uci}` + (m.tax ? ' · taxonomy line' : ' · derived continuation');
    s.onclick = () => { state.ply = i + 1; renderBoard(); renderMoves(); panelStats(); };
    pair.appendChild(s);
  });
}

/* ------------------------------------------------------------------- panel */
function panelStats() {
  const g = state.sel;
  if (!g) return;
  $('p-eco').textContent = g.ecos[0] + (g.ecos.length > 1 ? ` +${g.ecos.length - 1}` : '');
  $('p-name').textContent = g.name;
  $('p-family').textContent =
    `${g.base}${g.is_family_main ? ' · family mainline' : ''} · ` +
    (g.n_collapsed === 1 ? '1 taxonomy line' : `${g.n_collapsed} taxonomy lines collapse here`);

  const chips = $('eco-chips');
  chips.textContent = '';
  for (const e of g.ecos) { const c = el('span', 'chip'); c.textContent = e; chips.appendChild(c); }

  const m = g.line[state.ply - 1];
  $('stats').innerHTML = '';
  const add = (k, v, good) => {
    const d = el('div', 'stat' + (good ? ' good' : ''));
    d.innerHTML = `<div class="k">${k}</div><div class="v">${v}</div>`;
    $('stats').appendChild(d);
  };
  add('games', num(g.games));
  add('plies', g.line.length);
  add('codes', g.ecos.length);
  if (m) { add('position', num(m.pos_games)); add('this move', num(m.move_games)); }
}

function renderMirror() {
  const g = state.sel;
  const box = $('mirror-block');
  box.textContent = '';
  if (!g) return;
  const cp = g.counterpart;
  if (!cp) return;
  const other = state.data.groups[cp.group];
  const d = el('div', 'mirror');
  d.innerHTML =
    `<div class="k">colour-complex counterpart</div>
     <div class="mn">${other.eco} ${other.name}</div>
     <div class="mm">mirrored through ply <b>${cp.ply}</b> here and ply <b>${cp.counterpart_ply}</b> there
       · ${num(cp.games)} games there
       · ${cp.same_base ? 'same opening, opposite colours' : 'opposite-colour complex'}</div>`;
  const btn = el('button', 'btn small');
  btn.textContent = 'open the twin ▶';
  btn.onclick = () => { state.ply = cp.counterpart_ply; select(cp.group, cp.counterpart_ply); };
  d.appendChild(btn);
  const f = el('div', 'mono'); f.textContent = cp.mirror_fen; d.appendChild(f);
  box.appendChild(d);
}

function renderNotes() {
  const g = state.sel;
  const box = $('notes');
  box.textContent = '';
  if (!g) return;
  const add = (html, warn) => { const d = el('div', 'note' + (warn ? ' warn' : '')); d.innerHTML = html; box.appendChild(d); };
  const derived = g.n_plies - g.tax_plies;
  if (derived > 0) {
    add(`<b>${g.tax_plies}</b> plies from the published taxonomy line, then <b>${derived}</b> derived by the most-played 2200+ continuation. Derived plies carry a dotted underline.`);
  } else {
    add(`This is the published taxonomy line unchanged. No continuation was derived: the resulting position is not in the frequent-position index, or the most-played move there has under 100 games at 2200+.`);
  }
  const d = g.divergence;
  if (d && d.ply >= 2 && d.top_games > 0) {
    add(`The taxonomy line is not the most-played choice. At ply <b>${d.ply}</b> it plays <code>${d.san}</code> (${num(d.move_games)} games) while the 2200+ corpus prefers <code>${d.top_san}</code> (${num(d.top_games)} games).`, true);
  } else if (d && d.ply === 1 && derived > 0) {
    add(`The taxonomy line's first move <code>${d.san}</code> is not the most-played first move at 2200+ (<code>${d.top_san}</code>, ${num(d.top_games)} games). The derived part still follows the corpus from the taxonomy position onward.`, true);
  }
  add(`Counts are games reaching that <b>position</b> — transpositions are pooled, so a count can rise mid-line. They are not games following this exact move order.`, true);
  if (!g.counterpart) {
    add(`No colour-complex counterpart in this index: mirroring the position (colours swapped, ranks flipped) does not reproduce any line here, or it only matched at a depth where the coincidence is meaningless.`);
  }
  add(`Colour twins require <b>mutual</b> agreement: A pairs with B only if B also mirrors back to A. Without that, shallow coincidences pair unrelated openings.`);
  add(`Source: Lumbra OTB Complete 2026-07-08, both players rated 2200+, frequent-position index (min 100 games). ${num(2823189)} games, 46,044 positions. No engine evaluation and no speed/rating stratification.`);
}

function renderAliases() {
  const g = state.sel;
  const box = $('alias-block');
  box.textContent = '';
  if (!g) return;
  const label = el('div', 'label');
  label.innerHTML = `also indexed as <span class="hint">${g.aliases.length} other name${g.aliases.length === 1 ? '' : 's'} for this same position</span>`;
  box.appendChild(label);
  const list = el('div', 'aliases');
  for (const a of g.aliases) { const d = el('div'); d.textContent = a; list.appendChild(d); }
  box.appendChild(list);
}

function renderLine() {
  const g = state.sel;
  if (!g) { $('line').textContent = ''; return; }
  const parts = [];
  g.line.forEach((m, i) => { parts.push(i % 2 === 0 ? `${i / 2 + 1}.${m.san}` : m.san); });
  $('line').textContent = parts.join(' ');
}

/* -------------------------------------------------------------------- list */
function matches(g) {
  if (state.mainsOnly && !g.is_family_main) return false;
  if (state.withMirror && !g.counterpart) return false;
  if (state.eco && !g.eco.startsWith(state.eco)) return false;
  const q = state.q.trim().toLowerCase();
  if (!q) return true;
  if (g.name.toLowerCase().includes(q)) return true;
  if (g.ecos.some(e => e.toLowerCase().includes(q))) return true;
  return g.aliases.some(a => a.toLowerCase().includes(q));
}

function mark(text, q) {
  const i = text.toLowerCase().indexOf(q.toLowerCase());
  if (i < 0) return document.createTextNode(text);
  const frag = document.createDocumentFragment();
  frag.append(text.slice(0, i));
  const m = el('mark'); m.textContent = text.slice(i, i + q.length);
  frag.append(m, text.slice(i + q.length));
  return frag;
}

function select(id, ply) {
  state.sel = state.data.groups[id];
  state.ply = ply === undefined ? state.sel.line.length : ply;
  renderList(); renderBoard(); renderMoves(); panelStats(); renderMirror();
  renderAliases(); renderNotes(); renderLine();
  const cur = document.querySelector('.op.sel');
  if (cur) cur.scrollIntoView({ block: 'nearest' });
}

function renderList() {
  const box = $('list');
  box.textContent = '';
  const q = state.q.trim();
  let shown = 0;

  for (const fam of state.data.families) {
    const gs = fam.groups.map(i => state.data.groups[i]).filter(matches);
    if (!gs.length) continue;
    const open = !!q || state.mainsOnly || state.withMirror || state.open.has(fam.name) ||
                 gs.some(g => g.id === (state.sel && state.sel.id));

    const head = el('div', 'fam');
    const caret = el('span', 'caret'); caret.textContent = open ? '▾' : '▸';
    const eco = el('span', 'feco'); eco.textContent = fam.eco;
    const nm = el('span', 'nm'); nm.textContent = fam.name;
    const n = el('span', 'n'); n.textContent = fam.n;
    const g2 = el('span', 'g'); g2.textContent = num(fam.games);
    head.append(caret, eco, nm, n, g2);
    head.onclick = () => {
      if (state.open.has(fam.name)) state.open.delete(fam.name); else state.open.add(fam.name);
      renderList();
    };
    box.appendChild(head);

    if (!open) continue;
    for (const g of gs) {
      const row = el('div', 'op' + (g.is_family_main ? ' main' : '') +
                          (state.sel && state.sel.id === g.id ? ' sel' : ''));
      const e = el('span', 'eco'); e.textContent = g.eco;
      const nm2 = el('span', 'nm');
      nm2.appendChild(mark(g.name, q));
      if (g.n_collapsed > 1) {
        const b = el('span', 'badge-n'); b.textContent = '×' + g.n_collapsed;
        b.title = `${g.n_collapsed} taxonomy lines collapse into this position`;
        nm2.appendChild(b);
      }
      if (g.counterpart) {
        const t = el('span', 'badge-m'); t.textContent = '⇄';
        t.title = 'has a colour-complex counterpart';
        nm2.appendChild(t);
      }
      const g3 = el('span', 'g'); g3.textContent = num(g.games);
      row.append(e, nm2, g3);
      row.onclick = () => select(g.id);
      box.appendChild(row);
      shown++;
    }
  }
  if (!shown) { const d = el('div', 'op'); d.textContent = 'no matches'; box.appendChild(d); }
}

/* -------------------------------------------------------------------- boot */
async function init() {
  const res = await fetch('/mainlines.json');
  if (!res.ok) { $('source-line').textContent = 'mainlines.json missing'; return; }
  state.data = await res.json();
  const m = state.data.meta;
  $('source-line').textContent =
    `${state.data.groups.length} entries in ${state.data.families.length} families · ` +
    `${m.condensed} · ${num(m.games)} games rated 2200+ · derived ${m.generated}`;
  for (const v of [...new Set(state.data.groups.map(g => g.eco[0]))].sort()) {
    const o = document.createElement('option'); o.value = v; o.textContent = 'volume ' + v;
    $('eco').appendChild(o);
  }
  for (const id of ['first', 'prev', 'next', 'last']) $(id).disabled = true;
  $('search').addEventListener('input', e => { state.q = e.target.value; renderList(); });
  $('eco').addEventListener('change', e => { state.eco = e.target.value; renderList(); });
  $('mains-only').addEventListener('change', e => { state.mainsOnly = e.target.checked; renderList(); });
  $('with-mirror').addEventListener('change', e => { state.withMirror = e.target.checked; renderList(); });
  const step = d => {
    if (!state.sel) return;
    state.ply = Math.max(0, Math.min(state.sel.line.length, state.ply + d));
    renderBoard(); renderMoves(); panelStats();
  };
  $('first').onclick = () => { state.ply = 0; renderBoard(); renderMoves(); panelStats(); };
  $('prev').onclick = () => step(-1);
  $('next').onclick = () => step(1);
  $('last').onclick = () => { if (state.sel) { state.ply = state.sel.line.length; renderBoard(); renderMoves(); panelStats(); } };
  document.addEventListener('keydown', e => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    if (e.key === 'ArrowLeft') { step(-1); e.preventDefault(); }
    if (e.key === 'ArrowRight') { step(1); e.preventDefault(); }
    if (e.key === 'ArrowUp') { step(-2); e.preventDefault(); }
    if (e.key === 'ArrowDown') { step(2); e.preventDefault(); }
  });
  state.open.add(state.data.families[0].name);
  // default to the largest family's mainline rather than whatever sorts first by ECO
  const biggest = state.data.families.reduce((a, b) => (b.games > a.games ? b : a));
  state.open.add(biggest.name);
  select(biggest.groups[0]);
}

init();
