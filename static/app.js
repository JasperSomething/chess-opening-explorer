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
      "/api/position?moves=" + history.slice(0, cursor).join(",") + "&reference=" + $("#reference").value,
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
  const imp = data.import;
  const complete = imp.phase === "complete" && !imp.sample;
  $("#coverage").textContent = complete
    ? "Lumbra complete · " + fmt(imp.accepted) + " games · " + fmt(imp.retained_positions) + " opening positions"
    : "Lumbra import · " + ({candidates: "Pass 1/2: finding positions", exact: "Pass 2/2: counting games", finalizing: "Building opening graph", complete: "Sample only", not_started: "Waiting to start"}[imp.phase] || imp.phase) + " · " + fmt(imp.games) + " games" + (imp.progress_percent ? " · " + imp.progress_percent.toFixed(1) + "% of this pass" : "");
  const strong = data.strong_import;
  if (strong && !complete) {
    $("#coverage").textContent = "2200+ first · " + (strong.phase === "complete" ? "Complete" : "Pass " + (strong.phase === "candidates" ? "1/2" : "2/2")) + " · " + fmt(strong.games) + " records scanned · " + fmt(strong.accepted) + " qualifying games";
  }
  const selectedStrong = $("#reference").value === "2200";
  const selectedField = selectedStrong ? "lumbra2200" : "lumbra";
  const selectedLabel = selectedStrong ? "2200+" : "All games";
  const selectedPhase = selectedStrong && strong && !complete ? strong.phase : imp.phase;
  $("#reference-count").textContent = selectedLabel;
  $("#reference-percent").textContent = selectedLabel + " %";
  const countLabel = (n) => n === null ? "Pending" : fmt(n);
  $("#notice").textContent =
    (strong && !complete ? (strong.phase === "complete" ? "2200+ counts are ready. The all-games import continues separately. " : "Processing both-2200+ games first. Counts appear during its second pass and remain partial until complete. ") : complete ? "Local import complete. " : "Import in progress: counts appear in pass 2 and remain partial until complete. ") +
    selectedLabel + ": " + countLabel(data.local_totals[selectedStrong ? 1 : 0]) +
    (data.sources.lichess ? " · Lichess loaded" : " · Lichess statistics pending");
  $("#moves").replaceChildren();
  for (let m of data.moves) {
    if (!$("#legal").checked && !m[selectedField] && !m.lichess) continue;
    let tr = document.createElement("tr");
    tr.className = m.major ? "major" : "";
    let move = document.createElement("td"),
      button = document.createElement("button");
    button.textContent = m.san + (m.major ? " ●" : "");
    button.onclick = () => play(m.uci);
    move.append(button);
    tr.append(move);
    for (const [n, percent] of [[m[selectedField], false], [m[selectedField + "_percent"], true], [m.lichess, false]]) {
      const td = document.createElement("td");
      td.textContent = n === null || n === undefined
        ? (selectedPhase === "candidates" && tr.children.length < 3 ? "Pending" : "—")
        : percent ? n.toFixed(1) + "%" : fmt(n);
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
    td.colSpan = 5;
    td.textContent = data.moves.length
      ? "No recorded moves. Enable legal moves or use the board."
      : "No legal moves.";
    tr.append(td);
    $("#moves").append(tr);
  }
  $("#fen").textContent = data.key;
  $("#details").textContent =
    "Lumbra reference: " + (data.local_retained ? "retained opening position. " : "not yet in the completed retained graph. ") + "Canonical FEN omits move counters and nonlegal en-passant targets. " +
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
$("#reference").onchange = () => load();
setInterval(() => { if (!busy && (data?.import?.phase !== "complete" || !data?.sources?.lichess)) load(); }, 30000);
$("#legal").onchange = () => render();
$("#cancel-promotion").onclick = () => $("#promotion").close();
document.addEventListener("keydown", (e) => {
  if (
    $("#promotion").open ||
    ["INPUT", "BUTTON", "SELECT"].includes(document.activeElement.tagName)
  )
    return;
  if (e.key === "ArrowLeft") jump(cursor - 1);
  if (e.key === "ArrowRight") jump(cursor + 1);
});
load();
