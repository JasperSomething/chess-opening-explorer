const $ = (s) => document.querySelector(s),
  symbols = {
    K: "♚",
    Q: "♛",
    R: "♜",
    B: "♝",
    N: "♞",
    P: "♟",
    k: "♚",
    q: "♛",
    r: "♜",
    b: "♝",
    n: "♞",
    p: "♟",
  };
let history = [],
  cursor = 0,
  data,
  selected = null,
  flipped = false,
  requestId = 0,
  busy = false;
const fmt = (n) => (n === null || n === undefined ? "—" : n.toLocaleString());
async function load() {
  busy = true;
  const id = ++requestId;
  try {
    const r = await fetch(
      "/api/position?moves=" + history.slice(0, cursor).join(","),
    );
    if (!r.ok) throw Error(await r.text());
    const next = await r.json();
    if (id !== requestId) return;
    data = next;
    selected = null;
    $("#error").textContent = "";
    render();
  } catch (e) {
    $("#error").textContent = "Unable to load position: " + e.message;
  } finally {
    if (id === requestId) busy = false;
  }
}
function play(uci) {
  if (busy) return;
  history = history.slice(0, cursor);
  history.push(uci);
  cursor++;
  load();
}
function jump(n) {
  if (busy) return;
  cursor = Math.max(0, Math.min(history.length, n));
  load();
}
function squareClick(square) {
  if (selected) {
    let options = data.moves.filter((m) => m.uci.startsWith(selected + square));
    if (options.length === 1) {
      play(options[0].uci);
      return;
    }
    if (options.length > 1) {
      $("#promotion-options").replaceChildren(
        ...options.map((m) => {
          let b = document.createElement("button");
          b.textContent = m.san;
          b.onclick = () => {
            $("#promotion").close();
            play(m.uci);
          };
          return b;
        }),
      );
      $("#promotion").showModal();
      return;
    }
  }
  selected = selected === square ? null : square;
  renderBoard();
}
function renderBoard() {
  const nodes = [];
  for (let row = 0; row < 8; row++)
    for (let col = 0; col < 8; col++) {
      let file = flipped ? 7 - col : col,
        rank = flipped ? row : 7 - row,
        square = "abcdefgh"[file] + (rank + 1),
        p = data.pieces[square];
      let b = document.createElement("button");
      b.className =
        ((file + rank) % 2 ? "light" : "dark") +
        (p ? (p === p.toUpperCase() ? " piece-white" : " piece-black") : "") +
        (selected === square ? " selected" : "") +
        (selected && data.moves.some((m) => m.uci.startsWith(selected + square))
          ? " destination"
          : "");
      b.setAttribute(
        "aria-label",
        square +
          (p
            ? " " +
              (p === p.toUpperCase() ? "white " : "black ") +
              {
                k: "king",
                q: "queen",
                r: "rook",
                b: "bishop",
                n: "knight",
                p: "pawn",
              }[p.toLowerCase()]
            : " empty"),
      );
      b.textContent = p ? symbols[p] : "";
      if (col === 0 || row === 7) {
        let c = document.createElement("span");
        c.className = "coord";
        c.textContent =
          (col === 0 ? rank + 1 : "") + (row === 7 ? "abcdefgh"[file] : "");
        b.append(c);
      }
      b.onclick = () => squareClick(square);
      nodes.push(b);
    }
  $("#board").replaceChildren(...nodes);
}
function render() {
  renderBoard();
  $("#turn").textContent = data.turn + " to move";
  $("#ply").textContent = "Ply " + cursor;
  $("#start").disabled = $("#back").disabled = cursor === 0;
  $("#forward").disabled = cursor === history.length;
  $("#line").replaceChildren();
  if (!cursor) $("#line").textContent = "Starting position";
  data.sans.forEach((san, i) => {
    let b = document.createElement("button");
    b.textContent = (i % 2 === 0 ? Math.floor(i / 2) + 1 + ". " : "") + san;
    b.className = i === cursor - 1 ? "active" : "";
    b.onclick = () => jump(i + 1);
    $("#line").append(b);
  });
  $("#transposition").textContent =
    data.parents > 1
      ? "↗ Shared position · " + data.parents + " recorded incoming positions"
      : "Move history belongs to this line; statistics belong to the position.";
  let s = data.status;
  $("#coverage").textContent =
    (s.complete ? "Complete graph" : "Partial database") +
    " · " +
    fmt(s.masters_fetched) +
    " / " +
    fmt(s.positions) +
    " Masters positions";
  $("#notice").textContent = !data.sources.masters
    ? "This position has not been fetched. Legal moves remain available."
    : !data.sources.lichess
      ? "Masters loaded. Lichess statistics are pending."
      : fmt(data.sources.masters.total) +
        " master games · " +
        fmt(data.sources.lichess.total) +
        " Lichess games";
  $("#moves").replaceChildren();
  for (let m of data.moves) {
    if (!$("#legal").checked && !m.masters && !m.lichess) continue;
    let tr = document.createElement("tr");
    tr.className = m.major ? "major" : "";
    let move = document.createElement("td"),
      button = document.createElement("button");
    button.textContent = m.san + (m.major ? " ●" : "");
    button.onclick = () => play(m.uci);
    move.append(button);
    tr.append(move);
    for (let n of [m.masters, m.lichess]) {
      let td = document.createElement("td");
      td.textContent = fmt(n);
      tr.append(td);
    }
    let td = document.createElement("td");
    td.textContent = m.percent === null ? "—" : m.percent.toFixed(1) + "%";
    let bar = document.createElement("div");
    bar.className = "bar";
    let fill = document.createElement("i");
    fill.style.width = (m.percent || 0) + "%";
    bar.append(fill);
    td.append(bar);
    tr.append(td);
    $("#moves").append(tr);
  }
  if (!$("#moves").children.length) {
    let tr = document.createElement("tr"),
      td = document.createElement("td");
    td.colSpan = 4;
    td.textContent = data.moves.length
      ? "No recorded moves. Enable legal moves or use the board."
      : "No legal moves.";
    tr.append(td);
    $("#moves").append(tr);
  }
  $("#fen").textContent = data.key;
  $("#details").textContent =
    "Canonical FEN omits move counters and nonlegal en-passant targets. " +
    Object.entries(data.sources)
      .map(([s, v]) => s + " fetched " + v.fetched)
      .join(" · ") +
    ". Recorded next moves: " +
    fmt(data.continuation_total) +
    ". Games ending here do not enter the frequency denominator.";
}
$("#back").onclick = () => jump(cursor - 1);
$("#forward").onclick = () => jump(cursor + 1);
$("#start").onclick = () => jump(0);
$("#flip").onclick = () => {
  flipped = !flipped;
  renderBoard();
};
$("#legal").onchange = () => render();
$("#cancel-promotion").onclick = () => $("#promotion").close();
document.addEventListener("keydown", (e) => {
  if (
    $("#promotion").open ||
    ["INPUT", "BUTTON"].includes(document.activeElement.tagName)
  )
    return;
  if (e.key === "ArrowLeft") jump(cursor - 1);
  if (e.key === "ArrowRight") jump(cursor + 1);
});
load();
