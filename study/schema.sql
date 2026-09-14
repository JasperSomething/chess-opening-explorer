-- Opening Atlas analysis layer (research/study database).
--
-- This database is derived, read-only against the ingestion databases
-- (data/lumbra-2200.sqlite, data/lumbra.sqlite, data/lumbra-2200-complete.sqlite,
-- data/lumbra-lichess.sqlite). It never writes to them.
--
-- Design rules encoded here:
--   * every metric component is persisted individually; no table stores only a
--     final score (see metric / structure_stat / deviation);
--   * every source's coverage state is explicit (complete | partial | absent |
--     zero) so "no data" is never confused with "zero games";
--   * engine evaluations are cached with their provenance (source, depth, nodes,
--     engine build) and are reusable across runs;
--   * provenance/reverse edges are derived here, not by modifying importers.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_run (
    run_id      INTEGER PRIMARY KEY,
    domain      TEXT NOT NULL,
    started     TEXT NOT NULL,
    finished    TEXT,
    notes       TEXT,
    config_json TEXT NOT NULL
);

-- ----------------------------------------------------------------- positions
CREATE TABLE IF NOT EXISTS position (
    position_key     BLOB PRIMARY KEY,          -- 34-byte lossless key
    fen              TEXT NOT NULL,             -- 4-field canonical key (as used by existing DBs)
    full_fen         TEXT NOT NULL,
    domain           TEXT NOT NULL,
    ply              INTEGER NOT NULL,          -- ply from the standard start
    min_ply          INTEGER,                   -- min ply over all paths (transposition-aware)
    role             TEXT NOT NULL,             -- white_to_move | black_to_move
    is_entry         INTEGER NOT NULL DEFAULT 0,
    structure_id     TEXT NOT NULL,
    white_pawns      TEXT NOT NULL,             -- 64-bit pawn bitboard as 16 hex chars
    black_pawns      TEXT NOT NULL,             -- (hex, because bit 63 overflows SQLite INTEGER)
    pieces_json      TEXT NOT NULL,             -- 12 bitboards: [colour][piece]
    castling_rights  TEXT NOT NULL,             -- 64-bit castling-rights mask as hex
    white_castled    TEXT NOT NULL,             -- none | kingside | queenside | both_legacy
    black_castled    TEXT NOT NULL,
    dev_white        INTEGER NOT NULL,          -- pieces off home squares (excluding castled K/R)
    dev_black        INTEGER NOT NULL,
    dev_status       TEXT NOT NULL,             -- undeveloped | partial | developed
    run_id           INTEGER
);
CREATE INDEX IF NOT EXISTS position_structure ON position(structure_id);
CREATE INDEX IF NOT EXISTS position_ply ON position(ply);

-- Per-source position statistics. coverage_state is authoritative about absence:
--   complete  - the source indexes this position and its games are fully counted
--   partial   - the source is still importing; counts are a lower bound
--   absent    - the source cannot know this position (e.g. not in the API cache)
--   zero      - the source fully covers the region and recorded no games here
CREATE TABLE IF NOT EXISTS position_source (
    position_key  BLOB NOT NULL,
    source        TEXT NOT NULL,        -- lichess | masters | local2200 | local2200complete | allgames
    games         INTEGER NOT NULL,
    white         INTEGER NOT NULL,
    draws         INTEGER NOT NULL,
    black         INTEGER NOT NULL,
    entry_games   INTEGER,              -- domain-entry games in this source (reach denominator)
    reach_prob    REAL,                 -- games / entry_games
    coverage_state TEXT NOT NULL,
    retrieved     TEXT NOT NULL,
    PRIMARY KEY (position_key, source)
);

-- Per-source move distributions (empirical denominator excludes terminal games).
CREATE TABLE IF NOT EXISTS move_source (
    position_key BLOB NOT NULL,
    source       TEXT NOT NULL,
    uci          TEXT NOT NULL,
    san          TEXT NOT NULL,
    games        INTEGER NOT NULL,
    white        INTEGER NOT NULL,
    draws        INTEGER NOT NULL,
    black        INTEGER NOT NULL,
    share        REAL NOT NULL,          -- games / sum(games) at this position+source
    PRIMARY KEY (position_key, source, uci)
);

-- Derived reverse edges: how a position is reached. Built by replaying the
-- existing (position, move) data; no importer is modified.
CREATE TABLE IF NOT EXISTS provenance (
    child_key  BLOB NOT NULL,
    parent_key BLOB NOT NULL,
    uci        TEXT NOT NULL,
    source     TEXT NOT NULL,
    games      INTEGER,
    PRIMARY KEY (child_key, parent_key, uci, source)
);
CREATE INDEX IF NOT EXISTS provenance_child ON provenance(child_key);
CREATE INDEX IF NOT EXISTS provenance_parent ON provenance(parent_key);

-- ---------------------------------------------------------------- structures
CREATE TABLE IF NOT EXISTS structure (
    structure_id     TEXT PRIMARY KEY,   -- hash of the exact (white,black) pawn placement
    white_pawns      TEXT NOT NULL,
    black_pawns      TEXT NOT NULL,
    centre_config    INTEGER NOT NULL,   -- 8-bit mask: white c4 d4 e4 f4 | black c5 d5 e5 f5
    open_files       INTEGER NOT NULL,   -- files with no pawns of either colour
    semi_open_white  INTEGER NOT NULL,   -- files with no white pawn but black pawns
    semi_open_black  INTEGER NOT NULL,
    islands_white    INTEGER NOT NULL,
    islands_black    INTEGER NOT NULL,
    isolated_white   INTEGER NOT NULL,
    isolated_black   INTEGER NOT NULL,
    doubled_white    INTEGER NOT NULL,
    doubled_black    INTEGER NOT NULL,
    backward_white   INTEGER NOT NULL,
    backward_black   INTEGER NOT NULL,
    backward_approximate INTEGER NOT NULL DEFAULT 1,  -- definition is conservative
    passed_white     INTEGER NOT NULL,
    passed_black     INTEGER NOT NULL,
    material         TEXT NOT NULL        -- e.g. 8/8 -> KQRRBBNNPPPPPPPP with counts
);

CREATE TABLE IF NOT EXISTS structure_stat (
    structure_id   TEXT NOT NULL,
    source         TEXT NOT NULL,
    run_id         INTEGER NOT NULL,
    n_positions    INTEGER NOT NULL,
    n_edges_in     INTEGER NOT NULL,      -- distinct incoming edges
    n_parents      INTEGER NOT NULL,      -- distinct parent positions
    coverage_lower REAL NOT NULL,         -- max reach over its positions (lower bound)
    coverage_upper REAL NOT NULL,         -- min(1, sum of reach) (upper bound)
    peak_games     INTEGER NOT NULL,
    path_count     REAL,                  -- distinct move-order paths reaching the state
    persistence_median_plies REAL,
    persistence_mean_plies   REAL,
    n_development_signatures INTEGER,
    attractor_score REAL,
    components_json TEXT NOT NULL,
    PRIMARY KEY (structure_id, source, run_id)
);

-- ---------------------------------------------------------- engine evaluations
CREATE TABLE IF NOT EXISTS eval (
    fen           TEXT NOT NULL,
    multipv       INTEGER NOT NULL,
    source        TEXT NOT NULL,     -- lichess_cloud | local_stockfish
    depth         INTEGER NOT NULL,
    engine        TEXT NOT NULL,
    nodes         INTEGER,
    time_ms       INTEGER,
    pov           TEXT NOT NULL,     -- side to move of `fen`; scores are in this POV
    pvs_json      TEXT NOT NULL,     -- [{uci,san,cp,mate,wdl:{w,d,l}}]
    meets_threshold INTEGER NOT NULL, -- depth >= configured threshold
    fetched       TEXT NOT NULL,
    PRIMARY KEY (fen, multipv, source, depth)
);
CREATE INDEX IF NOT EXISTS eval_fen ON eval(fen);

-- ------------------------------------------------------------------- metrics
CREATE TABLE IF NOT EXISTS metric (
    run_id          INTEGER NOT NULL,
    position_key    BLOB NOT NULL,
    role            TEXT NOT NULL,
    expert_source   TEXT NOT NULL,
    eval_source     TEXT NOT NULL,
    eval_depth      INTEGER,
    eval_coverage   REAL NOT NULL,      -- share of the two distributions with evals
    n_moves         INTEGER NOT NULL,
    lichess_games   INTEGER NOT NULL,
    expert_games    INTEGER NOT NULL,
    regret_lichess_wp REAL,
    regret_expert_wp  REAL,
    gap_wp          REAL,
    regret_lichess_cp REAL,
    regret_expert_cp  REAL,
    gap_cp          REAL,
    js_divergence   REAL,
    reach_prob      REAL,
    reach_source    TEXT,
    entry_games     INTEGER,
    study_priority_wp REAL,
    study_priority_cp REAL,
    confidence      TEXT NOT NULL,      -- high | medium | low
    flags           TEXT NOT NULL,      -- comma separated reasons (never silent)
    PRIMARY KEY (run_id, position_key)
);

CREATE TABLE IF NOT EXISTS deviation (
    run_id         INTEGER NOT NULL,
    position_key   BLOB NOT NULL,
    uci            TEXT NOT NULL,
    san            TEXT NOT NULL,
    mover          TEXT NOT NULL,        -- opponent = black in the Scandinavian-as-White domain
    prob_lichess   REAL,
    prob_expert    REAL,
    prob_used      REAL NOT NULL,
    prob_source    TEXT NOT NULL,
    ep_best        REAL,
    ep_move        REAL,
    concession_wp  REAL,
    concession_cp  REAL,
    class          TEXT NOT NULL,        -- main | playable | inaccurate | punishable
    best_response_uci TEXT,
    best_response_san TEXT,
    best_response_ep  REAL,
    empirical_top_reply_uci TEXT,
    deviation_priority REAL,
    flags          TEXT NOT NULL,
    PRIMARY KEY (run_id, position_key, uci)
);

-- Reachability over the domain graph.
--
-- Reach probability must not be taken from raw position counts: this domain
-- transposes into non-Scandinavian move orders (e.g. 1.e4 d5 2.d4 e6 reaches the
-- same board as 1.e4 e6 2.d4 d5), so a position's game count can exceed the
-- entry's. The flow DP walks move by move from the entry and gives the
-- probability that a game which played 1.e4 d5 reaches the position; the raw
-- count ratio is kept next to it, and their disagreement measures how much of a
-- position's population arrives from outside the family.
CREATE TABLE IF NOT EXISTS position_flow (
    position_key     BLOB NOT NULL,
    source           TEXT NOT NULL,        -- whose move shares drive the flow
    reach_flow       REAL,                 -- probability a family game reaches it
    count_ratio      REAL,                 -- position games / entry games (may exceed 1)
    enter_mass       REAL,                 -- mass that first enters this position's structure here
    leakage          REAL,                 -- share of incoming mass leaving the domain
    ignored_back_mass REAL,                -- mass arriving after the first visit
    n_parents_contributing INTEGER,        -- parents whose edge feeds the flow

    n_parents_total  INTEGER,              -- distinct parents in the source's provenance
    path_count       REAL,                 -- distinct move-order paths (capped)
    min_ply          INTEGER,
    flags            TEXT NOT NULL,
    PRIMARY KEY (position_key, source)
);

-- Structural flow graph (research task A): mass transferred between pawn-structure
-- families by observed structural transitions. Pawn moves are irreversible, so this
-- graph is a DAG and each game crosses an edge at most once: mass is a probability.
CREATE TABLE IF NOT EXISTS structure_edge (
    source_key      TEXT NOT NULL,
    destination_key TEXT NOT NULL,
    uci             TEXT NOT NULL,
    source          TEXT NOT NULL,
    mass            REAL NOT NULL,     -- family-conditioned probability of this transition
    from_boards     INTEGER,
    from_entry_mass REAL,
    from_mean_ply   REAL,
    PRIMARY KEY (source_key, destination_key, uci, source)
);
CREATE INDEX IF NOT EXISTS structure_edge_source ON structure_edge(source_key);
CREATE INDEX IF NOT EXISTS structure_edge_dest ON structure_edge(destination_key);

-- Maturity components per pawn-structure family (research task B). Every component
-- is stored on its own; `maturity_index` is the mean of the depth-adjusted
-- residuals of the strategic components and is reported beside its parts.
CREATE TABLE IF NOT EXISTS structure_maturity (
    structure_id          TEXT NOT NULL,
    source                TEXT NOT NULL,
    boards                INTEGER,
    entry_mass            REAL,
    ply_mean              REAL,
    minors_white          REAL,
    minors_black          REAL,
    castled_white         REAL,
    castled_black         REAL,
    centre_left_white     REAL,
    centre_left_black     REAL,
    centre_files_white    REAL,
    centre_files_black    REAL,
    dwell_mean            REAL,
    retention_3           REAL,
    reconvergence         REAL,
    effective_successors  REAL,
    source_families       INTEGER,
    incoming_mass         REAL,
    residual_json         TEXT NOT NULL,
    maturity_index        REAL,     -- depth-adjusted residual (ahead of schedule)
    development_level     REAL,     -- absolute: minors + castling + centre commitment
    PRIMARY KEY (structure_id, source)
);
