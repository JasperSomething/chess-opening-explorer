"""Metric definitions and pure computations.

All formulas live here so the design document and the code cannot drift apart.
Expected points (EP) is the common currency, in [0, 1] from the perspective of a
named player:

    EP_wdl(w, d, l) = (w + d / 2) / (w + d + l)          -- prefer this
    EP_cp(cp)       = 1 / (1 + 10 ** (-cp / 400))        -- fallback when only cp is known
    EP_mate(mate)   = 1.0 if mate > 0 else 0.0           -- forced mate for the side to move

Regret of a distribution over moves at a position, for the player to move:

    Regret(P, π) = Σ_m π(m) · max(0, EP_best − EP(m))    -- expected loss, 0 <= r <= 1

KnowledgeGap(P) = Regret(P, π_lichess) − Regret(P, π_expert)

StudyPriority(P) = ReachProbability(P) · KnowledgeGap(P)

Components are always returned; no caller may persist only the product.
"""
import math
from dataclasses import dataclass, field

# Sample-size safeguards (configurable by the caller).
MIN_EXPERT_GAMES = 30          # local 2200+ games required to treat a share as evidence
MIN_LICHESS_GAMES = 100        # Lichess games required to treat a share as evidence
MIN_MOVE_SHARE = 0.005         # moves below this share in both sources are not evaluated
MIN_MOVE_GAMES = 5             # nor are moves with fewer games than this
MIN_EVAL_COVERAGE = 0.85       # share of the two distributions that must carry evaluations
JS_EPSILON = 0.0               # no smoothing: empirical zeros are real zeros

# Deviation classification thresholds, in expected-points conceded by the mover.
PLAYABLE_MAX_LOSS = 0.02       # within engine/eval noise: a playable alternative
INACCURATE_MAX_LOSS = 0.10     # material but not immediately decisive


def ep_from_wdl(w, d, l):
    total = w + d + l
    if total <= 0:
        return None
    return (w + d / 2) / total


def ep_from_cp(cp):
    return 1.0 / (1.0 + 10.0 ** (-cp / 400.0))


def ep_from_mate(mate):
    return 1.0 if mate > 0 else 0.0


def ep_from_score(cp=None, mate=None, wdl=None):
    """EP for the side to move. Prefers WDL, then mate, then centipawns."""
    if wdl is not None:
        ep = ep_from_wdl(wdl[0], wdl[1], wdl[2])
        if ep is not None:
            return ep
    if mate is not None:
        return ep_from_mate(mate)
    if cp is not None:
        return ep_from_cp(cp)
    return None


def flip_ep(ep):
    """Convert EP from one player's perspective to the opponent's."""
    return None if ep is None else 1.0 - ep


def flip_wdl(wdl):
    """Child WDL (from the child's side to move) -> expected points for the parent's mover.

    The child's wins are the parent-mover's losses, so:
        EP_parent_mover = child_losses + child_draws / 2   (normalised)
    """
    if wdl is None:
        return None
    w, d, l = wdl
    total = w + d + l
    if total <= 0:
        return None
    return (l + d / 2.0) / total


def wilson_interval(k, n, z=1.96):
    """Wilson score interval for a share; returns (low, high)."""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def normalise(counts):
    total = sum(counts.values())
    if total <= 0:
        return {}, 0
    return {k: v / total for k, v in counts.items()}, total


def filter_moves(dist, min_share=MIN_MOVE_SHARE, min_games=MIN_MOVE_GAMES, counts=None):
    """Keep moves that are worth an engine evaluation.

    A move is dropped only if it is rare in *both* senses: below min_share and
    below min_games. Returns (kept_dict, dropped_mass).
    """
    counts = counts if counts is not None else {}
    kept, dropped = {}, 0.0
    for uci, share in dist.items():
        games = counts.get(uci, 0)
        if share >= min_share or games >= min_games:
            kept[uci] = share
        else:
            dropped += share
    return kept, dropped


def renormalise(dist):
    total = sum(dist.values())
    if total <= 0:
        return {}, 0.0
    return {k: v / total for k, v in dist.items()}, total


@dataclass
class RegretResult:
    regret: float | None
    best_uci: str | None
    best_ep: float | None
    mass_covered: float
    n_moves: int
    per_move: list = field(default_factory=list)   # (uci, prob, ep, loss, flags)
    method: str = 'wp'

    def as_components(self):
        return {
            'regret': self.regret,
            'best_uci': self.best_uci,
            'best_ep': self.best_ep,
            'mass_covered': self.mass_covered,
            'n_moves': self.n_moves,
            'method': self.method,
        }


def expected_regret(dist, evals, method='wp'):
    """Expected loss of a move distribution against engine evaluations.

    dist  : {uci: probability} (need not sum to 1; it is renormalised)
    evals : {uci: {'ep_wp': float|None, 'ep_cp': float|None, 'mate': int|None}}
    method: 'wp' uses WDL-derived EP, 'cp' uses centipawn-derived EP.
    """
    key = 'ep_wp' if method == 'wp' else 'ep_cp'
    usable = {}
    for uci, p in dist.items():
        ev = evals.get(uci)
        ep = ev.get(key) if ev else None
        if ep is not None and p > 0:
            usable[uci] = (p, ep)
    if not usable:
        return RegretResult(None, None, None, 0.0, 0, [], method)
    norm, total = renormalise({u: p for u, (p, _) in usable.items()})
    best_uci, best_ep = max(((u, ep) for u, (_, ep) in usable.items()), key=lambda kv: kv[1])
    regret, per_move = 0.0, []
    for uci, p in norm.items():
        ep = usable[uci][1]
        loss = max(0.0, best_ep - ep)
        regret += p * loss
        per_move.append((uci, p, ep, loss))
    per_move.sort(key=lambda t: -t[1])
    return RegretResult(regret, best_uci, best_ep, total, len(norm), per_move, method)


def knowledge_gap(regret_lichess, regret_expert, require_positive=True):
    """LichessExpectedRegret - ExpertExpectedRegret.

    A negative value means the Lichess population played *better* than the expert
    distribution on this position (possible, and worth seeing rather than hiding).
    """
    if regret_lichess is None or regret_expert is None:
        return None
    gap = regret_lichess - regret_expert
    return gap


def kl_divergence(p, q):
    total = 0.0
    for k in set(p) | set(q):
        pk, qk = p.get(k, 0.0), q.get(k, 0.0)
        if pk > 0:
            if qk <= 0:
                return float('inf')
            total += pk * math.log(pk / qk, 2)
    return total


def js_divergence(p, q):
    """Jensen-Shannon divergence in bits, plus the overlap coefficient.

    Descriptive signal only: two distributions can diverge sharply while every
    move is objectively equivalent, so this never feeds study value.
    """
    if not p or not q:
        return None, None
    m = {k: (p.get(k, 0.0) + q.get(k, 0.0)) / 2 for k in set(p) | set(q)}
    js = 0.5 * kl_divergence(p, m) + 0.5 * kl_divergence(q, m)
    if math.isinf(js):
        js = None
    overlap = sum(min(p.get(k, 0.0), q.get(k, 0.0)) for k in set(p) | set(q))
    return js, overlap


def study_priority(reach_prob, gap):
    if reach_prob is None or gap is None:
        return None
    return reach_prob * gap


def classify_deviation(concession, playable_max=PLAYABLE_MAX_LOSS, inaccurate_max=INACCURATE_MAX_LOSS):
    """normal/main | playable alternative | strategically inaccurate | punishable."""
    if concession is None:
        return 'unknown'
    if concession <= playable_max:
        return 'playable'
    if concession <= inaccurate_max:
        return 'inaccurate'
    return 'punishable'


def deviation_priority(prob, concession):
    if prob is None or concession is None:
        return None
    return prob * max(0.0, concession)
