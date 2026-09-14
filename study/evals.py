"""Engine evaluations: Lichess cloud first, local Stockfish only for real gaps.

Order of preference (per the research constraint):

  1. Lichess cloud evaluation, reused from the local cache when present, accepted
     only if its depth meets the quality threshold (default 30);
  2. local Stockfish, capped by an explicit per-run budget, single threaded.

Every evaluation is cached in the analysis database with its provenance (source,
depth, nodes, engine build) and is reused across runs, so a position is analysed
once. Scores are always stored in the point of view of the side to move of the
stored FEN; the conversion to "the mover's expected points after this move" is
done by the resolver and is covered by unit tests.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

import chess
import chess.engine

from study import metrics
from study.structures import pawn_masks  # noqa: F401  (imported for symmetry of use)

CLOUD_URL = 'https://lichess.org/api/cloud-eval'
USER_AGENT = 'opening-atlas-research/0.1 (local study analysis)'


def canonical_fen(board):
    """4-field FEN key, identical to explorer.key()/the ingestion databases."""
    return ' '.join(board.fen(en_passant='legal').split()[:4])


def board_for(fen):
    return chess.Board(fen + ' 0 1')


@dataclass
class EvalBudget:
    cloud_requests: int = 0
    cloud_max: int = 2000
    local_searches: int = 0
    local_max: int = 200
    local_depth: int = 18
    local_multipv: int = 5
    nodes_used: int = 0

    def cloud_ok(self):
        return self.cloud_requests < self.cloud_max

    def local_ok(self):
        return self.local_searches < self.local_max


class CloudClient:
    """Lichess cloud evaluation. Public endpoint; no token required."""

    def __init__(self, delay=0.6, timeout=30, budget=None):
        self.delay = delay
        self.timeout = timeout
        self.budget = budget or EvalBudget()
        self.last_request = 0.0
        self.errors = 0

    def fetch(self, board, multipv=5):
        """Return a dict with depth/pvs, or None when the position is not in the cloud."""
        if not self.budget.cloud_ok():
            return None
        wait = self.delay - (time.time() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        query = urllib.parse.urlencode({'fen': board.fen(), 'multiPv': multipv})
        request = urllib.request.Request(f'{CLOUD_URL}?{query}', headers={'User-Agent': USER_AGENT})
        self.last_request = time.time()
        self.budget.cloud_requests += 1
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None                      # not analysed in the cloud: a real gap
            if error.code == 429:
                retry = int(error.headers.get('Retry-After') or 60)
                time.sleep(min(retry, 120))
                return None
            self.errors += 1
            return None
        except Exception:
            self.errors += 1
            return None
        pvs = []
        for pv in payload.get('pvs', []):
            moves = pv.get('moves', '').split()
            if not moves:
                continue
            pvs.append({'uci': moves[0], 'line': moves, 'cp': pv.get('cp'), 'mate': pv.get('mate'),
                        'wdl': None})
        return {'source': 'lichess_cloud', 'engine': 'lichess-cloud',
                'depth': int(payload.get('depth', 0)), 'nodes': payload.get('knodes'),
                'time_ms': None, 'pov': 'side_to_move', 'pvs': pvs}


class LocalClient:
    """Stockfish through python-chess; one thread, WDL enabled."""

    def __init__(self, binary, budget=None):
        self.binary = binary
        self.budget = budget or EvalBudget()
        self.engine = None
        self.engine_id = None

    def start(self):
        if self.engine is None:
            self.engine = chess.engine.SimpleEngine.popen_uci(self.binary)
            self.engine.configure({'Threads': 1, 'Hash': 64, 'UCI_ShowWDL': True})
            self.engine_id = self.engine.id.get('name', 'stockfish')
        return self.engine

    def close(self):
        if self.engine is not None:
            self.engine.quit()
            self.engine = None

    def analyse(self, board, multipv=5, depth=18):
        if not self.budget.local_ok():
            return None
        engine = self.start()
        self.budget.local_searches += 1
        infos = engine.analyse(board, chess.engine.Limit(depth=depth), multipv=multipv)
        if isinstance(infos, dict):
            infos = [infos]
        pvs = []
        for info in infos:
            line = info.get('pv') or []
            if not line:
                continue
            score = info['score']
            relative = score.relative          # side to move of `board`
            cp = None if relative.is_mate() else relative.score()
            mate = relative.score() if relative.is_mate() else None
            wdl = None
            if hasattr(score, 'wdl'):
                try:
                    w = score.pov(board.turn).wdl(model='sf')
                    wdl = (w.wins, w.draws, w.losses)
                except Exception:
                    wdl = None
            pvs.append({'uci': line[0].uci(), 'line': [m.uci() for m in line],
                        'cp': cp, 'mate': mate, 'wdl': wdl})
        return {'source': 'local_stockfish', 'engine': self.engine_id, 'depth': depth,
                'nodes': None, 'time_ms': None, 'pov': 'side_to_move', 'pvs': pvs}


@dataclass
class MoveEval:
    uci: str
    ep_cp: float | None = None
    ep_wp: float | None = None
    mate: int | None = None
    source: str | None = None
    depth: int | None = None
    route: str | None = None      # parent_multipv | child_eval


def _ep_pair_from_score(cp, mate, wdl, flip):
    """Expected points for the mover, optionally flipped from the child's point of view."""
    if flip:
        wdl = None if wdl is None else (wdl[2], wdl[1], wdl[0])   # child's view -> parent mover
        cp = None if cp is None else -cp
        mate = None if mate is None else -mate
    ep_cp = metrics.ep_from_score(cp=cp, mate=mate, wdl=None)
    ep_wp = metrics.ep_from_wdl(*wdl) if wdl is not None else None
    return ep_cp, ep_wp


class EvalStore:
    """SQLite-backed evaluation cache (table `eval`)."""

    def __init__(self, db, cloud=None, local=None, cloud_threshold=30, budget=None):
        self.db = db
        self.cloud = cloud
        self.local = local
        self.cloud_threshold = cloud_threshold
        self.budget = budget or EvalBudget()
        self.stats = {'cloud_hits': 0, 'local_hits': 0, 'fetched': 0, 'missing': 0}

    # ---------------------------------------------------------------- cache
    def cached(self, fen, multipv, min_depth=0, sources=('lichess_cloud', 'local_stockfish')):
        placeholders = ','.join('?' * len(sources))
        row = self.db.execute(
            f'''SELECT source, depth, pvs_json, engine, nodes FROM eval
                WHERE fen=? AND multipv=? AND depth>=? AND source IN ({placeholders})
                ORDER BY depth DESC LIMIT 1''',
            (fen, multipv, min_depth, *sources)).fetchone()
        if not row:
            return None
        return {'source': row[0], 'depth': row[1], 'pvs': json.loads(row[2]),
                'engine': row[3], 'nodes': row[4]}

    def store(self, fen, multipv, result):
        if not result:
            return
        meets = 1 if (result['source'] == 'local_stockfish'
                      or result['depth'] >= self.cloud_threshold) else 0
        self.db.execute(
            '''INSERT OR REPLACE INTO eval(fen, multipv, source, depth, engine, nodes, time_ms,
                                           pov, pvs_json, meets_threshold, fetched)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (fen, multipv, result['source'], result['depth'], result.get('engine') or '',
             result.get('nodes'), result.get('time_ms'), result['pov'],
             json.dumps(result['pvs']), meets, time.strftime('%Y-%m-%dT%H:%M:%S%z')))
        self.db.commit()

    # ------------------------------------------------------------ retrieval
    def get(self, board, multipv=5, allow_local=True, min_depth=None):
        """Cached-or-fetched evaluation of a position, cloud preferred."""
        fen = canonical_fen(board)
        min_depth = self.cloud_threshold if min_depth is None else min_depth
        hit = self.cached(fen, multipv, min_depth=min_depth)
        if hit:
            self.stats['cloud_hits' if hit['source'] == 'lichess_cloud' else 'local_hits'] += 1
            return hit
        # Try the cloud at any depth first, then re-fetch if it was too shallow.
        shallow = self.cached(fen, multipv, min_depth=0, sources=('lichess_cloud',))
        if shallow and shallow['depth'] >= min_depth:
            return shallow
        if self.cloud is not None:
            result = self.cloud.fetch(board, multipv=multipv)
            if result:
                self.store(fen, multipv, result)
                self.stats['fetched'] += 1
                if result['depth'] >= min_depth:
                    return result
        if not allow_local or self.local is None:
            self.stats['missing'] += 1
            return shallow                       # may be None or a too-shallow cloud eval
        result = self.local.analyse(board, multipv=multipv,
                                    depth=self.budget.local_depth)
        if result:
            self.store(fen, multipv, result)
            self.stats['fetched'] += 1
            return result
        self.stats['missing'] += 1
        return None

    def resolve_moves(self, board, moves, multipv=5, allow_local=True, local_parent=False):
        """Per-move values from the mover's perspective.

        moves: iterable of UCI strings that were actually played at `board`.
        Returns {uci: MoveEval} plus the parent evaluation used as the reference.
        """
        moves = list(dict.fromkeys(moves))
        parent = self.get(board, multipv=max(multipv, min(len(moves) + 1, 5)),
                          allow_local=allow_local)
        out = {}
        parent_pvs = {pv['uci']: pv for pv in (parent or {}).get('pvs', [])}
        for uci in moves:
            pv = parent_pvs.get(uci)
            if pv is None:
                continue
            ep_cp, ep_wp = _ep_pair_from_score(pv.get('cp'), pv.get('mate'), pv.get('wdl'), flip=False)
            out[uci] = MoveEval(uci, ep_cp, ep_wp, pv.get('mate'), (parent or {}).get('source'),
                                (parent or {}).get('depth'), 'parent_multipv')
        missing = [u for u in moves if u not in out]
        for uci in missing:
            child = board.copy(stack=False)
            try:
                child.push_uci(uci)
            except Exception:
                continue
            result = self.get(child, multipv=multipv, allow_local=allow_local)
            if not result or not result['pvs']:
                continue
            pv = result['pvs'][0]
            ep_cp, ep_wp = _ep_pair_from_score(pv.get('cp'), pv.get('mate'), pv.get('wdl'), flip=True)
            out[uci] = MoveEval(uci, ep_cp, ep_wp, (None if pv.get('mate') is None else -pv['mate']),
                                result['source'], result['depth'], 'child_eval')
        if local_parent and self.local is not None:
            # One local multi-PV search gives cp + WDL for every move at once.
            local = self.local.analyse(board, multipv=min(len(moves), 8),
                                       depth=self.budget.local_depth)
            if local:
                self.store(canonical_fen(board), min(len(moves), 8), local)
                for pv in local['pvs']:
                    if pv['uci'] in moves:
                        ep_cp, ep_wp = _ep_pair_from_score(pv.get('cp'), pv.get('mate'),
                                                           pv.get('wdl'), flip=False)
                        out[pv['uci']] = MoveEval(pv['uci'], ep_cp, ep_wp, pv.get('mate'),
                                                  local['source'], local['depth'], 'parent_multipv')
        return out, parent
