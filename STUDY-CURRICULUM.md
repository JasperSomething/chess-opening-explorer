# Curriculum frontier: design and prototype specification

Status: design + prototype on current data. No domain widening, no new engine
computation, ingestion untouched.

Goal. Replace "number of templates -> game coverage" with
**knowledge complexity -> expected practical value retained**, over four separate
knowledge classes that are never merged into one ranking:

1. `orientation` — corridor knowledge: which broad opening state am I in.
2. `template` — mature, reusable developed configurations.
3. `decision` — individual positions where the correct move has measurable value.
4. `exception` — where the general template fails, or a deviation deserves a
   specific answer.

--------------------------------------------------------------------------------

## 1. Curriculum-item representation

Every item is one row and every quantity is a separate column. Nothing is stored
only as a composite; a `components_json` next to each aggregate is mandatory.

```
CREATE TABLE curriculum_item (
    item_id        TEXT PRIMARY KEY,       -- stable hash of (type, scope, answer)
    run_id         INTEGER NOT NULL,
    type           TEXT NOT NULL,          -- orientation | template | decision | exception
    label          TEXT NOT NULL,          -- neutral, machine-generated
    -- scope: the positions the item is intended to cover
    scope_kind     TEXT NOT NULL,          -- structure | position | deviation
    scope_id       TEXT NOT NULL,          -- structure_id, position_key hex, or pos+uci
    scope_json     TEXT NOT NULL,          -- explicit position list or filter conditions
    positions      INTEGER NOT NULL,       -- exact positions subsumed
    -- flow-weighted coverage, per source population, never a single number
    coverage_flow  REAL NOT NULL,          -- sum of reach over scope positions
    coverage_games REAL NOT NULL,          -- entering games behind that mass
    coverage_source TEXT NOT NULL,
    -- prerequisites (a template presupposes its corridor; a decision presupposes both)
    prereq_json    TEXT NOT NULL,          -- [item_id, ...]
    -- expected-value benefit, where measurable, with its provenance
    benefit_ep     REAL,                   -- expected points retained per game
    benefit_basis  TEXT,                   -- how it was measured (see section 5)
    benefit_sample INTEGER,                -- positions/games behind the number
    benefit_flags  TEXT NOT NULL,          -- never-silent reasons for absence
    -- complexity proxies, all separate
    complexity_json TEXT NOT NULL,         -- see section 2
    -- exceptions
    exception_json TEXT NOT NULL,          -- counter-example positions + condition
    n_exceptions   INTEGER NOT NULL,
    PRIMARY KEY (item_id)
);
```

Rules carried over from the existing design, unchanged:

* missing data is never rendered as zero (`benefit_flags` is mandatory and
  non-empty when `benefit_ep` is NULL);
* both the raw components and any aggregate are stored;
* scope is explicit: the positions an item claims to cover are enumerable, so
  overlap between items is computable rather than asserted.

The four types differ in exactly two columns: `scope_kind` and whether
`benefit_ep` is expected to be non-null.

| type | scope | expected benefit |
|---|---|---|
| orientation | a corridor family (one or more structures) | NULL by construction — recognition, not move choice |
| template | a mature family, optionally split by a feature | positive where evals exist |
| decision | one position | positive where evals exist |
| exception | position or family + a condition | positive where evals exist |

Orientation items having no EV benefit is a property, not a gap: they change what
the learner *notices*, not what they *play*. They are therefore scored on the
recognition axis (section 5) and must never be credited with regret reduction, or
the frontier becomes a lie.

## 2. Complexity proxies

Each proxy is a separate number. The total is a declared linear weighting, kept
beside its inputs so the weighting can be argued with:

```
complexity_json = {
  "exact_moves":        n,   # answers that must be recalled verbatim (low = easy)
  "boards":             n,   # representative boards to hold in mind (0 or 1 per item)
  "flexible_slots":     n,   # squares the learner must treat as variable
  "conditions":         n,   # tests needed to recognise applicability
  "exceptions":         n,   # counter-examples carried with the item
  "positions":          n,   # exact positions compressed
  "move_orders":        n,   # distinct move orders covered
  "cost":               x    # w_moves*exact_moves + w_slots*flexible_slots
                             # + w_cond*max(0,conditions-1) + w_exc*exceptions
                             # + w_board*boards       (weights declared, default 1)
  "compression_ratio":  r    # positions / max(cost, 1)
  "we": [w_moves, w_slots, w_cond, w_exc, w_board]
}
```

`compression_ratio` is the bridge to the earlier LearningEfficiency idea, stated
in countable units rather than as a hidden score. The frontier's x-axis is `cost`,
not `compression_ratio`, because cost is the quantity a human actually pays.

## 3. Template-splitting criterion (refining pawn families)

Question: within a mature pawn family S, does feature F (castling state, queen
square bucket, bishop configuration, knight configuration, development imbalance,
open-file occupancy) predict *materially different future strong-player
behaviour*? Only split when it does.

Measurement, per family S and feature F:

```
behaviour Y      = distribution over future strong-player moves in the next
                   k plies from the family's boards (k = 6 in the prototype),
                   as a distribution over transformation events (section 4),
                   not over raw moves only
candidate split  = partition of S's boards by F
I(Y; F | S)      = H(Y) - sum_g w_g * H(Y | F = g)      [entropy reduction, bits]
w_g              = flow-weighted share of group g
```

Decision rule, all four conditions required:

1. `I(Y; F | S) >= min_bits` (default 0.10 bits);
2. permutation test: relabel F across S's boards preserving group sizes, 200
   permutations, `p < 0.05` (guards against gain from small groups);
3. every group holds `>= min_share` (default 5%) of the family's flow mass and
   `>= 5` boards;
4. `n_groups >= 2` after merging groups below the share floor.

Reported for every tested feature, split or not: `I(Y;F|S)`, the permutation
p-value, group sizes, and the per-group behaviour histograms. A feature that
fails is still evidence and is stored, so "we tried to split and the data said
no" is visible.

No generic clustering: a vector difference without a behaviour difference is
never a split. This is enforced by making Y — not the position vector — the
target of every test.

## 4. Motif mining (recurring transformation motifs)

Unit of observation: a trajectory window. From a family's entering-mass
distribution, sample forward paths through the strong-play flow for 6-12 plies
(sampling proportional to edge mass; fixed seed; sampled mass coverage reported).

An event is an atomic transformation, exactly the vocabulary already in the
transformation report, made canonical:

```
event := (kind, subject, from, to)
kind    := pawn_advance | pawn_capture | piece_move | capture | castle | file_open | structure_exit
subject := P|N|B|R|Q|K|w|b, or file letter for file_open
example := ("piece_move","N","g1","f3"), ("castle","w","k"), ("file_open",,"c",)
```

Mining, weighted by sampled trajectory mass, per family:

* **sets** (unordered co-occurrence) via weighted Apriori: `support(E)` = mass
  share of trajectories containing all events in E; mine up to size 4 with
  `support >= 0.05`;
* **ordered sub-sequences** with one swap of slack: a motif is a pair/triple with
  a designated canonical order, accepted if the mass share of trajectories where
  the order holds is `>= 0.5` of the motif's support; the complement is recorded
  as `order_flexibility`, not discarded;
* **family association**: for each motif, `P(motif | family)` and the entropy of
  its family distribution, so a motif that is generic across the opening is
  distinguishable from one that is characteristic of a single family;
* **conditionality**: `P(motif | family)` vs `P(motif)` gives the lift; stored.

Output columns: `motif_id, events_json, size, support, conditional_frequency,
order_flexibility, family_id, family_lift, sample_mass, seed`. No naming, no
prose, no interpretation.

## 5. Curriculum simulation mathematics

Two value axes, computed and reported separately. They are never summed.

Notation: `P` = domain positions; `r_p` = flow-weighted reach (probability a
domain game visits p); `q_p` = ordinary-population move distribution;
`e_p` = strong-play move distribution; `EP(m)` = expected points of move m at p;
`loss_p(m) = max(0, EP(best_p) - EP(m))`.

Baseline ignorance cost, and the strong-play ceiling:

```
V_population = sum_p r_p * sum_m q_p(m) * loss_p(m)      # ordinary play
V_expert     = sum_p r_p * sum_m e_p(m) * loss_p(m)      # strong play residual
```

An item i has scope `S_i` and a taught answer set `A_i` (moves it prescribes). Its
modelled effect at a covered position: the learner plays the taught move set
instead of the population distribution, so

```
v_i(p) = sum_m phat_i(m) * loss_p(m),   phat_i = e_p restricted to A_i and
                                                   renormalised  (A_i nonempty)
```

Orientation items prescribe nothing: `A_i = {}`, `v_i(p) = 0`.

**Overlap accounting**: at a position covered by several items the learner uses
the best available answer, so the retained value is a maximum, never a sum:

```
best(p)   = max over items covering p of v_i(p)          (0 if none)
V_cur(C)  = V_population - sum_p r_p * best(p)           # value retained by C
marginal(i | C) = sum_{p in S_i} r_p * max(0, v_i(p) - best_C(p))
```

This is why five near-identical templates cannot claim five times the knowledge:
the second one's marginal is near zero wherever the first already covered the
position, and exactly zero where the taught answers coincide.

Curriculum frontier: greedy selection by `marginal(i | C) / cost(i)` — value per
unit of learning cost — with ties broken toward fewer exceptions, reporting at
each step:

```
step, item_id, type, cost, cumulative_cost, value_retained, marginal_value,
coverage_flow, coverage_games, benefit_basis, benefit_sample
```

Stages requested by the project map onto the same curve: `empty` (no items) is
`V_population` lost; `orientation only` retains ~0 EV and X% recognition;
`+top template`, `+more templates`, `+decision nodes`, `+exceptions` are
prefixes of the greedy order; `full` is the ceiling with every item.

Recognition axis (the second, separate output):

```
V_recog(C) = sum_p r_p * [covered by at least one item in C]
```

**Coverage of the value axis must be reported with every number**: if items'
positions carry engine evaluations for only a fraction of their mass, the
retained-value figure is stated on that fraction (`benefit_sample`), while the
recognition axis is stated on all of it.

## 6. Honest validation design

What the current data cannot do: the databases contain **aggregate counts only**
(`position, uci, white/draws/black, white2200/draws2200/black2200`), with no game
identifiers and no per-game move traces. Every number in the analysis database is
already summed over all games. A random 80/20 game split therefore cannot be
formed, and any "held-out" evaluation built from these aggregates would contain
the same games on both sides. It is not attempted.

Two honest routes, in order of preference:

**(A) Games-level split — requires the trace index (section 7).** Split games by
`hash(game_id) % 100 < 80` (or by event/date block, which is stronger: it also
breaks within-tournament familiarity). Rebuild every distribution, reach and
regret *within each split*. Construct curriculum items from the train split only.
Evaluate on the test split: take the test split's actual move distributions, and
credit an item only when its taught answers reduce the test split's expected
loss measured with independent engine evaluations. Report the frontier on test,
not train. Block splits by date are preferred over random splits because
transpositions already cross games within a split; blocks additionally stop
same-event correlation.

**(B) Cross-population transfer — available now, and used by the prototype.**
The two move sources are disjoint game populations: 2200+ OTB games (from the
PGB corpus) and online Lichess games (explorer counts). Curriculum items are
constructed from strong-play aggregates only; the evaluation uses only ordinary
play's move shares at the same positions. No Lichess share ever enters item
construction, so the evaluation is out-of-sample for the target quantity
"does the population's own move choice lose points that the item's answer
recovers". This is a transfer test across populations, not a random split, so it
carries the known confounds (online vs OTB, rating mix, time control) and is
reported as such. It also cannot hold out *positions*, only the behavioural
target.

Neither route removes the need for engine evaluations at the positions being
evaluated; both report value only where evaluations exist.

## 7. Additional raw data required

1. **Game-level trace index (blocking requirement for route A).** One derived
   table, written to the analysis database, never into the ingestion databases:

   ```
   CREATE TABLE domain_game_trace (
       game_id     INTEGER NOT NULL,   -- stable hash of source row identity
       split       INTEGER NOT NULL,   -- assigned from hash or date block
       ratings     INTEGER, date TEXT, result TEXT, source TEXT,
       path_json   TEXT NOT NULL       -- domain position keys visited, in order
   );  PRIMARY KEY (game_id, source)
   ```

   Produced by a read-only replay of the existing corpus
   (`data/LumbrasGigaBase_OTB_Complete.pgn`, 8.1 GB, one pass, position-key lookup
   per move). Cost is one parse of the corpus — the same work the running importer
   already does, but it must not run concurrently with it: schedule it after the
   current import finishes.
2. **Engine evaluation coverage.** Value requires `EP(m)` per position/move. The
   cache holds 523 evaluations over 472 of 29,876 domain positions (1.6%), and
   179 positions have full two-distribution regret. A defensible frontier over
   the whole domain needs evaluations on the positions carrying the bulk of the
   flow mass — the top few hundred families cover most of it, so a bounded budget
   (a few thousand single-PV evaluations at depth 18, no cloud) is the right
   shape. Until then the frontier is reported with its sample fraction attached.
3. **Per-game outcomes** (from item 1) if value is to be reported as points per
   *game* rather than points per position-visit.

-------------------------------------------------------------------------------

## Prototype scope in this delivery

Instantiated on current data, no new engine work:

* `study/curriculum.py` — item construction for all four types from existing
  tables, complexity proxies, overlap-aware frontier, both value axes.
* `study/splitting.py` — section 3, run over the mature families.
* `study/motifs.py` — section 4, trajectory sampling + weighted set/sequence
  mining, run over the mature families.
* `study/curriculum_report.py` — renders `analysis/curriculum-frontier.md`.
* Tests: complexity proxies, max-not-sum overlap rule, monotone frontier,
  split criterion on a planted feature (and non-splitting on a noise feature),
  motif support/flexibility on synthetic trajectories.
