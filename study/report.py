"""Render an inspectable study report from the analysis database.

Reads the analysis DB only (no engine calls, no ingestion writes) and writes
markdown plus CSV under analysis/. The main line to each ranked position is the
most likely move order (Viterbi over the expert move shares), which is what a
reader needs to see in order to judge whether the ranking is useful.
"""
import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

import chess

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import db as studydb  # noqa: E402
from study.structures import render_pawns  # noqa: E402


def viterbi_paths(db, source, entry_key):
    """Most likely move order to every position in the flow graph."""
    moves = {}
    for row in db.execute('SELECT parent_key, child_key, uci, games FROM provenance WHERE source=?',
                          (source,)):
        moves.setdefault(row['parent_key'], []).append((row['child_key'], row['uci'], row['games']))
    position_ply = {row['position_key']: row['ply']
                    for row in db.execute('SELECT position_key, ply FROM position')}
    san = {}
    for row in db.execute('SELECT position_key, uci, san FROM move_source WHERE source=?', (source,)):
        san[(row['position_key'], row['uci'])] = row['san']

    best = {entry_key: (0.0, None, None)}          # key -> (logprob, parent, uci)
    by_ply = {}
    for key, ply in position_ply.items():
        by_ply.setdefault(ply, []).append(key)
    for ply in sorted(by_ply):
        for parent in by_ply[ply]:
            if parent not in best:
                continue
            edges = moves.get(parent, [])
            total = sum(g for _c, _u, g in edges)
            if not total:
                continue
            base = best[parent][0]
            for child, uci, games in edges:
                if child not in position_ply or position_ply[child] <= ply:
                    continue
                score = base + math.log(games / total)
                if child not in best or score > best[child][0]:
                    best[child] = (score, parent, uci)
    lines = {}
    for key in best:
        seq, node = [], key
        while node in best and best[node][1] is not None:
            _score, parent, uci = best[node]
            seq.append(san.get((parent, uci), uci))
            node = parent
        lines[key] = ' '.join(reversed(seq))
    return lines


def best_engine_move(db, fen):
    """Best move from the deepest cached evaluation of this position."""
    row = db.execute('SELECT source, depth, pvs_json FROM eval WHERE fen=? '
                     'ORDER BY depth DESC LIMIT 1', (fen,)).fetchone()
    if not row or not row['pvs_json']:
        return '—'
    pvs = json.loads(row['pvs_json'])
    if not pvs:
        return '—'
    board = chess.Board(fen + ' 0 1')
    uci = pvs[0]['uci']
    try:
        san = board.san(chess.Move.from_uci(uci))
    except Exception:
        san = uci
    return f"{san} ({row['source'].replace('lichess_cloud', 'cloud')} d{row['depth']})"


def human_eval(cp, mate):
    if mate:
        return f'#{mate}' if mate > 0 else f'#-{abs(mate)}'
    if cp is None:
        return '?'
    return f'{cp / 100:+.2f}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=studydb.DEFAULT_ANALYSIS_DB)
    parser.add_argument('--run-id', type=int)
    parser.add_argument('--top', type=int, default=20)
    parser.add_argument('--deviations', type=int, default=20)
    parser.add_argument('--outdir', type=Path)
    args = parser.parse_args()

    db = sqlite3.connect('file:' + str(args.db.resolve()) + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    run_id = args.run_id or db.execute('SELECT MAX(run_id) FROM analysis_run '
                                       'WHERE finished IS NOT NULL').fetchone()[0]
    entry_key = db.execute('SELECT position_key FROM position WHERE is_entry=1').fetchone()[0]
    lines = viterbi_paths(db, 'local2200', entry_key)

    out = []
    out.append('# Opening Atlas — study report (Scandinavian 1.e4 d5)\n')
    out.append(f'Analysis run `{run_id}`. Positive `gap` means the Lichess population gives away '
               f'more than the strong-player distribution at that position. `reach` is the flow '
               f'probability that a game which played 1.e4 d5 arrives there; `count×` is the raw '
               f'position count divided by the entry population (may exceed 1 when non-Scandinavian '
               f'move orders transpose in).\n')

    rows = db.execute('''SELECT m.*, p.fen, p.ply, p.dev_status, f.n_parents_total, f.path_count,
                                f.count_ratio, f.flags AS flow_flags
                         FROM metric m
                         JOIN position p ON p.position_key = m.position_key
                         LEFT JOIN position_flow f ON f.position_key = m.position_key
                              AND f.source = m.expert_source
                         WHERE m.run_id=? AND m.role='white_to_move' AND m.study_priority_cp IS NOT NULL
                         ORDER BY m.study_priority_cp DESC LIMIT ?''',
                      (run_id, args.top)).fetchall()
    rows = [dict(row) for row in rows]

    out.append('## Top positions for White to study\n')
    out.append('| # | move order (most likely) | reach | count× | expert n | lichess n | '
               'regret L | regret E | gap | JS | best engine move | cover | conf |')
    out.append('|---|---|---|---|---|---|---|---|---|---|---|---|---|')
    for i, row in enumerate(rows, 1):
        out.append(
            f"| {i} | `{lines.get(row['position_key'], '?')}` | {row['reach_prob']:.4f} | "
            f"{row['count_ratio'] if row['count_ratio'] is not None else '—'} | "
            f"{row['expert_games']} ({row['expert_source']}) | {row['lichess_games']:,} | "
            f"{row['regret_lichess_cp']:.4f} | {row['regret_expert_cp']:.4f} | "
            f"**{row['gap_cp']:+.4f}** | "
            f"{(f'{row['js_divergence']:.3f}' if row['js_divergence'] is not None else '—')} | "
            f"`{best_engine_move(db, row['fen'])}` | {row['eval_coverage']:.2f} | {row['confidence']} |")

    out.append('\n### The same rows in full\n')
    for i, row in enumerate(rows, 1):
        out.append(f"{i}. `{row['fen']}`  \n   ply {row['ply']}, {row['dev_status']}, "
                   f"parents {row['n_parents_total']}, paths {row['path_count']}, "
                   f"eval {row['eval_source']} d{row['eval_depth']}, "
                   f"flags: {row['flags'] or '—'}"
                   + (f" · flow: {row['flow_flags']}" if row['flow_flags'] else ''))

    devs = db.execute('''SELECT d.*, p.fen, p.ply FROM deviation d
                         JOIN position p ON p.position_key = d.position_key
                         WHERE d.run_id=? AND d.class != 'main' AND d.deviation_priority IS NOT NULL
                         ORDER BY d.deviation_priority DESC LIMIT ?''',
                      (run_id, args.deviations)).fetchall()
    devs = [dict(row) for row in devs]
    out.append('\n## Opponent (Black) deviations worth expecting\n')
    out.append('| # | move order | deviates with | P(dev) | concession (EP) | class | '
               'engine punishment | priority |')
    out.append('|---|---|---|---|---|---|---|---|')
    for i, row in enumerate(devs, 1):
        out.append(f"| {i} | `{lines.get(row['position_key'], '?')}` | {row['san']} | "
                   f"{row['prob_used']:.3f} | {row['concession_cp']:.4f} | {row['class']} | "
                   f"`{row['best_response_uci'] or '—'}` | "
                   f"{row['deviation_priority']:.5f} |")

    out.append('\n## Structural compression (exact structures, no clustering)\n')
    total_positions = db.execute('SELECT COUNT(*) FROM position').fetchone()[0]
    total_structures = db.execute('SELECT COUNT(*) FROM structure').fetchone()[0]
    out.append(f'{total_positions:,} exact positions collapse to {total_structures:,} exact pawn '
               f'structures in this domain.\n')
    struct = db.execute('''SELECT s.structure_id, s.white_pawns, s.black_pawns, s.centre_config,
                                  s.material, COUNT(p.position_key) AS n_positions,
                                  SUM(CASE WHEN p.role='white_to_move' THEN 1 ELSE 0 END) AS n_white
                           FROM structure s JOIN position p ON p.structure_id = s.structure_id
                           GROUP BY s.structure_id ORDER BY n_positions DESC LIMIT 10''').fetchall()
    struct = [dict(row) for row in struct]
    reach = {}
    for row in db.execute('''SELECT p.structure_id, f.reach_flow FROM position p
                             JOIN position_flow f ON f.position_key=p.position_key
                             AND f.source='local2200' '''):
        reach[row['structure_id']] = max(reach.get(row['structure_id'], 0.0), row['reach_flow'] or 0.0)
    out.append('| # | boards | white to move | peak reach | material | pawn diagram |')
    out.append('|---|---|---|---|---|---|')
    for i, row in enumerate(struct, 1):
        diagram = render_pawns(int(row['white_pawns'], 16), int(row['black_pawns'], 16))
        out.append(f"| {i} | {row['n_positions']} | {row['n_white']} | "
                   f"{reach.get(row['structure_id'], 0):.4f} | {row['material']} | "
                   f"<pre>{diagram}</pre> |")

    out.append('\n## Structural attractors\n')
    out.append('Structures ranked by `coverage_lower x mean persistence x (1 + log10(1 + paths))`. '
               '`persistence` is the expected number of consecutive plies spent in the structure: '
               'a high mean with a low median marks a transit state that most games pass through '
               'briefly, while a high median marks a state games settle into.\n')
    attract = db.execute("""SELECT st.*, s.white_pawns, s.black_pawns, s.material
                            FROM structure_stat st JOIN structure s ON s.structure_id=st.structure_id
                            WHERE st.run_id=? AND st.source='local2200'
                            ORDER BY st.attractor_score DESC LIMIT 10""", (run_id,)).fetchall()
    attract = [dict(row) for row in attract]
    out.append('| # | score | coverage (lower–upper) | boards | ways in (edges/parents) | paths | '
               'persistence mean/median | dev. signatures | material | pawn diagram |')
    out.append('|---|---|---|---|---|---|---|---|---|---|')
    for i, row in enumerate(attract, 1):
        diagram = render_pawns(int(row['white_pawns'], 16), int(row['black_pawns'], 16))
        out.append(f"| {i} | {row['attractor_score']:.2f} | {row['coverage_lower']:.3f}–"
                   f"{row['coverage_upper']:.3f} | {row['n_positions']} | "
                   f"{row['n_edges_in']}/{row['n_parents']} | {int(row['path_count'])} | "
                   f"{(f'{row['persistence_mean_plies']:.2f}' if row['persistence_mean_plies'] else '—')}/"
                   f"{(f'{row['persistence_median_plies']:.1f}' if row['persistence_median_plies'] else '—')} | "
                   f"{row['n_development_signatures']} | {row['material']} | <pre>{diagram}</pre> |")

    run = db.execute('SELECT * FROM analysis_run WHERE run_id=?', (run_id,)).fetchone()
    config = json.loads(run['config_json'])
    out.append('\n## Run provenance\n')
    out.append(f"* domain: {run['domain']}, started {run['started']}, finished {run['finished']}")
    out.append(f"* engine: Stockfish 19, Threads=1, Hash=64, WDL on; depth "
               f"{config.get('depth')} for local fallback, cloud accepted at depth ≥ "
               f"{config.get('cloud_depth')}")
    evals = db.execute('SELECT source, COUNT(*) n, ROUND(AVG(depth),1) d FROM eval GROUP BY source'
                       ).fetchall()
    out.append('* evaluations cached: ' + ', '.join(f'{e["source"]} {e["n"]} (avg depth {e["d"]})'
                                                    for e in evals))
    out.append(f"* positions with metrics: "
               f"{db.execute('SELECT COUNT(*) FROM metric WHERE run_id=?', (run_id,)).fetchone()[0]}"
               f", deviations recorded: "
               f"{db.execute('SELECT COUNT(*) FROM deviation WHERE run_id=?', (run_id,)).fetchone()[0]}")

    outdir = args.outdir or (ROOT / 'analysis')
    outdir.mkdir(parents=True, exist_ok=True)
    target = outdir / f'study-report-run{run_id}.md'
    target.write_text('\n'.join(out) + '\n')
    print(f'report written to {target}')
    db.close()


if __name__ == '__main__':
    main()
