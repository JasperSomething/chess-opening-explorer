"""Server-free, resumable distribution of the approved engine campaign.

The phase-1 sheet selects 1,200 positions (`evaluation_candidate`, campaign
`phase1`). Each selected position becomes exactly one unit of engine work whose
size is fixed by its tier in `campaign.NODE_TIERS` and whose answer width is
`campaign.MULTIPV`. Those two constants are imported, never redefined or
rescaled: the search budget is nodes, and the depth the engine happens to reach
is metadata recorded beside the result — it is not part of the job identity and
never decides whether a job is finished.

Four pieces, all of them local files and one SQLite table:

1. **Job ledger** (`engine_job`) — one row per (fen, multipv, nodes_budget,
   engine_version). The primary key is a sha1 of that tuple, so re-planning the
   same sheet is a no-op: `INSERT OR IGNORE` and a count of what was already
   covered. Idempotency is the whole point of the table.
2. **Distribution** — `export_jobs` writes the pending queue as JSONL, a worker
   machine runs `run_worker` against its own copy of the analysis database (or
   an imported ledger) and writes JSONL results, `import_results` folds them
   back. No server, no broker, no network.
3. **Leases** — `claim_next` is a single conditional `UPDATE` (the job is leased
   only if it is pending or its lease has expired) with the row returned via
   SQLite's `RETURNING`, so two workers racing on the same database cannot both
   win a job. A crashed worker's leases return to `pending` after
   `lease_seconds`; `release_expired` does that explicitly. Killing the runner
   therefore loses at most the jobs currently leased, each of which is only a
   node budget, not a result.
4. **Topology benchmark** — the sizes above were budgeted at one thread. Before
   spending 1,200 jobs we measure how the machine actually scales: the same
   positions at the same node budget under N one-thread workers versus N/2
   two-thread workers, reported as aggregate nodes/second
   (`total_nodes / wall_seconds`, nothing else).

Engine calls are always injected. Every function that could start an engine
takes an `analyse` (or `analyse_factory`) callable whose signature is
`analyse(fen, nodes, multipv) -> stored-evaluation dict`, and the default
implementation is built from `study.campaign.analyse_at_nodes`. This is what
lets the unit tests run the whole scheduler, ledger and importer offline with a
fake.

Concurrency model for `run_worker`: **threads, not processes**. The work that
costs time happens inside the Stockfish subprocess; the Python side only pushes
a position over a pipe and waits, so the GIL is not the bottleneck. Threads also
keep one process per machine (one set of leases, one counter, one lock, an
injectable in-process `analyse`, no pickling), and each worker thread owns its
own SQLite connection and its own engine subprocess.

Distribution: `export_jobs` / `import_results` / `claim_next`. Nothing here
writes to an ingestion database; the analysis database is opened with
`study.db.connect(..., create=False)` and the only table created is
`engine_job`.
"""
import argparse
import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable, Iterable, Optional, Sequence

import chess

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import campaign, db as studydb, evals as evals_module  # noqa: E402

# Imported, never redefined: the campaign's node budgets and answer width.
NODE_TIERS = campaign.NODE_TIERS
MULTIPV = campaign.MULTIPV
DEFAULT_CAMPAIGN = 'phase1'

Analyse = Callable[[str, int, int], dict]
AnalyseFactory = Callable[[dict], Analyse]

SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_job (
    job_id          TEXT PRIMARY KEY,      -- sha1 of 'fen|multipv|nodes_budget|engine_version'
    fen             TEXT NOT NULL,
    multipv         INTEGER NOT NULL,
    nodes_budget    INTEGER NOT NULL,
    engine_version  TEXT NOT NULL,
    state           TEXT NOT NULL,         -- pending | leased | done | failed
    worker          TEXT,
    leased_at       TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    position_key    BLOB,
    tier            TEXT,
    campaign        TEXT,
    depth_achieved  INTEGER,
    nodes_actual    INTEGER,
    time_ms         INTEGER,
    result_json     TEXT,
    finished_at     TEXT,
    UNIQUE(fen, multipv, nodes_budget, engine_version)
);
CREATE INDEX IF NOT EXISTS engine_job_state ON engine_job(state, leased_at);
CREATE INDEX IF NOT EXISTS engine_job_tier ON engine_job(campaign, tier);
"""

CLAIM_ORDER = 'nodes_budget ASC, job_id ASC'   # cheapest tier first, deterministic


# ------------------------------------------------------------------- schema/key
def ensure_schema(db) -> None:
    """Create the job ledger. The only table this module owns."""
    db.executescript(SCHEMA)
    db.commit()


def job_id_for(fen: str, multipv: int, nodes_budget: int, engine_version: str) -> str:
    """Stable identity of a unit of engine work.

    The achieved depth is deliberately absent: the budget is the job, the depth
    is a by-product. Two runs at the same budget are the same job whatever depth
    the engine reached; changing the budget makes a new job.
    """
    identity = f'{fen}|{multipv}|{nodes_budget}|{engine_version}'
    return hashlib.sha1(identity.encode('utf-8')).hexdigest()


def _utc(epoch: Optional[float] = None) -> str:
    return datetime.fromtimestamp(time.time() if epoch is None else epoch,
                                 timezone.utc).isoformat(timespec='microseconds')


def binary_fingerprint(binary: str | Path) -> str:
    """Engine version string without starting the engine: name, size, mtime.

    Two builds of the same binary differ, and a rebuilt binary invalidates the
    ledger entry, which is what we want from an engine_version.
    """
    path = Path(binary)
    if not path.exists():
        return path.name
    stat = path.stat()
    return f'{path.name}:{stat.st_size}:{int(stat.st_mtime)}'


# ------------------------------------------------------------------------ plan
def plan_jobs(db, campaign: str = DEFAULT_CAMPAIGN, engine_version: Optional[str] = None,
              database: Any = None) -> dict:
    """Turn the selected candidate sheet into pending jobs. Idempotent.

    One job per selected `evaluation_candidate` row, with the node budget read
    from `campaign.NODE_TIERS` by tier and the answer width from
    `campaign.MULTIPV`. A job whose (fen, multipv, nodes_budget, engine_version)
    already exists in *any* state — pending, leased, done or failed — is not
    inserted again; those are reported as `skipped`. Planning the same sheet
    twice, or re-planning after a partial run, inserts nothing.

    `database` optionally points at another copy of the analysis database to read
    the candidate sheet from (a path or an open connection); the default is the
    connection being planned into. Nodes are never rescaled and depth is never a
    planning input.
    """
    if not engine_version:
        raise ValueError('engine_version is required: it is part of the job identity')
    ensure_schema(db)
    if database is None:
        source = db
    elif hasattr(database, 'execute'):
        source = database
    else:
        source = studydb.readonly(database)
    rows = source.execute('''SELECT position_key, fen, tier FROM evaluation_candidate
                             WHERE selected=1 AND campaign=?
                             ORDER BY CASE tier WHEN 'A' THEN 0 WHEN 'B' THEN 1 ELSE 2 END, -score''',
                          (campaign,)).fetchall()
    inserted = skipped = untiered = 0
    for row in rows:
        position_key, fen, tier = row[0], row[1], row[2]
        nodes = NODE_TIERS.get(tier)
        if nodes is None:
            untiered += 1
            continue
        job_id = job_id_for(fen, MULTIPV, nodes, engine_version)
        cursor = db.execute('''INSERT OR IGNORE INTO engine_job(
            job_id, fen, multipv, nodes_budget, engine_version, state, attempts,
            position_key, tier, campaign) VALUES(?,?,?,?,?, 'pending', 0, ?, ?, ?)''',
                            (job_id, fen, MULTIPV, nodes, engine_version,
                             position_key, tier, campaign))
        if cursor.rowcount:
            inserted += 1
        else:
            skipped += 1
    db.commit()
    total = db.execute('SELECT COUNT(*) FROM engine_job WHERE campaign=?', (campaign,)).fetchone()[0]
    return {'campaign': campaign, 'engine_version': engine_version, 'candidates': len(rows),
            'inserted': inserted, 'skipped': skipped, 'untiered': untiered, 'jobs': total}


# ------------------------------------------------------------------ distribution
def export_jobs(db, path: str | Path) -> dict:
    """Write every pending job as one JSON object per line."""
    ensure_schema(db)
    rows = db.execute(f'''SELECT job_id, fen, multipv, nodes_budget, engine_version, tier,
                                 position_key, campaign, attempts FROM engine_job
                          WHERE state='pending' ORDER BY {CLAIM_ORDER}''').fetchall()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        for row in rows:
            position_key = row['position_key']
            handle.write(json.dumps({
                'job_id': row['job_id'], 'fen': row['fen'], 'multipv': row['multipv'],
                'nodes_budget': row['nodes_budget'], 'engine_version': row['engine_version'],
                'tier': row['tier'], 'campaign': row['campaign'], 'attempts': row['attempts'],
                'position_key': bytes(position_key).hex() if position_key is not None else None,
            }) + '\n')
    return {'exported': len(rows), 'path': str(path)}


def import_jobs(db, path: str | Path, campaign: Optional[str] = None) -> dict:
    """Load a JSONL job bundle into a fresh ledger, exactly as exported.

    This is what lets the remote worker run without the analysis database: the
    approved sheet is exported once locally, shipped as a bundle, and imported into
    an empty ledger on the compute box. Job ids travel with the bundle, so the
    keying — sha1(fen|multipv|nodes_budget|engine_version) — is identical on both
    machines and a job done remotely is recognised as done locally.

    Idempotent: `INSERT OR IGNORE` against UNIQUE(fen, multipv, nodes_budget,
    engine_version) means importing the same bundle twice writes nothing the second
    time. Jobs already present in any state are counted as `already_present`, and a
    job that arrives marked done is not resurrected as pending.
    """
    ensure_schema(db)
    imported = already_present = malformed = 0
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                fields = (record['job_id'], record['fen'], int(record['multipv']),
                          int(record['nodes_budget']), record['engine_version'])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                malformed += 1
                continue
            position_key = record.get('position_key')
            if position_key:
                position_key = bytes.fromhex(position_key)
            cursor = db.execute('''INSERT OR IGNORE INTO engine_job(
                    job_id, fen, multipv, nodes_budget, engine_version, state, position_key,
                    tier, campaign, attempts)
                VALUES(?,?,?,?,?,'pending',?,?,?,?)''',
                                (*fields, position_key, record.get('tier'),
                                 campaign or record.get('campaign') or DEFAULT_CAMPAIGN,
                                 int(record.get('attempts') or 0)))
            if cursor.rowcount == 1:
                imported += 1
            else:
                already_present += 1
    db.commit()
    return {'imported': imported, 'already_present': already_present, 'malformed': malformed,
            'ledger_total': db.execute('SELECT COUNT(*) FROM engine_job').fetchone()[0]}


def import_results(db, path: str | Path) -> dict:
    """Fold a worker's JSONL results back into the ledger and the eval cache.

    Each line is `{job_id, depth_achieved, nodes_actual, time_ms, result}`. A job
    already marked done is counted as `already_done` and **nothing** is written
    for it — not the ledger row, not the eval cache — so importing the same file
    twice leaves the database bit-for-bit as it was after the first import.
    Unknown job ids and unparsable lines are counted, never silently dropped.
    """
    ensure_schema(db)
    store = evals_module.EvalStore(db)
    imported = already_done = unknown = malformed = eval_stored = 0
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                job_id = record['job_id']
            except (json.JSONDecodeError, KeyError):
                malformed += 1
                continue
            row = db.execute('SELECT fen, multipv, state FROM engine_job WHERE job_id=?',
                             (job_id,)).fetchone()
            if row is None:
                unknown += 1
                continue
            if row['state'] == 'done':
                already_done += 1
                continue
            result = record.get('result') or None
            db.execute('''UPDATE engine_job SET state='done', depth_achieved=?, nodes_actual=?,
                          time_ms=?, result_json=?, finished_at=?, worker=NULL, leased_at=NULL
                          WHERE job_id=?''',
                       (record.get('depth_achieved'),
                        record.get('nodes_actual'),
                        record.get('time_ms'),
                        json.dumps(result) if result is not None else None,
                        record.get('finished_at') or _utc(), job_id))
            db.commit()
            imported += 1
            if result:
                store.store(row['fen'], row['multipv'], result)
                eval_stored += 1
    return {'imported': imported, 'already_done': already_done, 'unknown': unknown,
            'malformed': malformed, 'eval_stored': eval_stored}


def claim_next(db, worker: str, lease_seconds: int = 900,
               now: Optional[float] = None) -> Optional[dict]:
    """Atomically lease one claimable job, or return None when there is none.

    Claimable means `state='pending'`, or `state='leased'` with a lease older than
    `lease_seconds` (a worker that died). The lease is taken by one conditional
    `UPDATE` whose target is chosen by the same predicate and returned with
    `RETURNING`, so the selection and the mutation cannot interleave between two
    workers: the second worker's predicate no longer matches the row.
    """
    now_iso = _utc(now)
    cutoff = _utc((time.time() if now is None else now) - lease_seconds)
    claimable = "(state='pending' OR (state='leased' AND leased_at <= ?))"
    rows = db.execute(
        f'''UPDATE engine_job
            SET state='leased', worker=?, leased_at=?, attempts=attempts+1
            WHERE job_id = (SELECT job_id FROM engine_job WHERE {claimable}
                            ORDER BY {CLAIM_ORDER} LIMIT 1)
              AND {claimable}
            RETURNING job_id, fen, multipv, nodes_budget, engine_version, tier, campaign,
                      position_key, attempts''',
        (worker, now_iso, cutoff, cutoff)).fetchall()
    db.commit()
    if not rows:
        return None
    job = dict(rows[0])
    job['worker'] = worker
    job['leased_at'] = now_iso
    return job


def release_expired(db, now: Optional[float] = None, lease_seconds: int = 900) -> int:
    """Return expired leases to `pending`. Returns how many were recovered."""
    cutoff = _utc((time.time() if now is None else now) - lease_seconds)
    cursor = db.execute('''UPDATE engine_job SET state='pending', worker=NULL, leased_at=NULL
                           WHERE state='leased' AND leased_at <= ?''', (cutoff,))
    db.commit()
    return cursor.rowcount


def stats(db) -> dict:
    """Ledger summary: queue depth and the node accounting that matters."""
    ensure_schema(db)
    counts = {'pending': 0, 'leased': 0, 'done': 0, 'failed': 0}
    for row in db.execute('SELECT state, COUNT(*) FROM engine_job GROUP BY state'):
        counts[row[0]] = row[1]
    nodes_done = db.execute(
        "SELECT COALESCE(SUM(nodes_actual), 0) FROM engine_job WHERE state='done'").fetchone()[0]
    nodes_remaining = db.execute(
        "SELECT COALESCE(SUM(nodes_budget), 0) FROM engine_job WHERE state IN ('pending','leased')"
    ).fetchone()[0]
    return {'pending': counts['pending'], 'leased': counts['leased'], 'done': counts['done'],
            'failed': counts['failed'], 'nodes_done': nodes_done,
            'nodes_remaining': nodes_remaining}


# ---------------------------------------------------------------- engine access
class _EngineProxy:
    """Forwards to a python-chess engine but pins Threads and Hash.

    `campaign.analyse_at_nodes` configures `Threads: 1` because that is how the
    campaign was budgeted. A topology benchmark needs the same search code with a
    different thread count, so the topology's value is asserted at the point the
    engine would be reconfigured — the analysis itself is still
    `campaign.analyse_at_nodes`, unchanged.
    """

    def __init__(self, engine, threads: int, hash_mb: int):
        self._engine = engine
        self._threads = threads
        self._hash_mb = hash_mb

    def configure(self, options: dict):
        merged = dict(options)
        merged['Threads'] = self._threads
        merged['Hash'] = self._hash_mb
        return self._engine.configure(merged)

    def __getattr__(self, name):
        return getattr(self._engine, name)


class ThreadLocalAnalyser:
    """`analyse(fen, nodes, multipv)` with one engine per calling thread.

    Each worker thread gets its own engine subprocess (python-chess engines are
    not thread-safe), configured at the topology's thread count and lazily
    started on first use. Every engine that is ever started is registered here,
    because python-chess runs the UCI protocol in a non-daemon thread: an engine
    that is not quit keeps the interpreter alive after the work is done, and an
    engine owned by a worker thread cannot be reached through thread-local
    storage once that thread has returned. `close()` therefore quits all of them.
    """

    def __init__(self, binary: str, threads_per_worker: int = 1, hash_mb: int = 64):
        self.binary = binary
        self.threads_per_worker = int(threads_per_worker)
        self.hash_mb = int(hash_mb)
        self._local = threading.local()
        self._engines: list = []
        self._lock = threading.Lock()

    def _engine(self):
        engine = getattr(self._local, 'engine', None)
        if engine is None:
            raw = chess.engine.SimpleEngine.popen_uci(self.binary)
            engine = _EngineProxy(raw, self.threads_per_worker, self.hash_mb)
            self._local.engine = engine
            with self._lock:
                self._engines.append(engine)
        return engine

    def __call__(self, fen: str, nodes: int, multipv: int = MULTIPV) -> dict:
        board = evals_module.board_for(fen)
        return campaign.analyse_at_nodes(self.binary, board, nodes, multipv=multipv,
                                         engine=self._engine())

    def close(self) -> None:
        with self._lock:
            engines, self._engines = self._engines, []
        for engine in engines:
            engine.quit()
        self._local.engine = None


def make_analyser(binary: str, threads_per_worker: int = 1, hash_mb: int = 64) -> Analyse:
    """Real analysis callable, built from `campaign.analyse_at_nodes`."""
    return ThreadLocalAnalyser(binary, threads_per_worker, hash_mb)


# -------------------------------------------------------------------- worker
class _Counter:
    """Shared job budget across worker threads."""

    def __init__(self, limit: Optional[int]):
        self.limit = limit
        self.value = 0
        self._lock = threading.Lock()

    def exhausted(self) -> bool:
        with self._lock:
            return self.limit is not None and self.value >= self.limit

    def take(self) -> None:
        with self._lock:
            self.value += 1


def _append_jsonl(path: Path, record: dict, lock: threading.Lock) -> None:
    with lock:
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(record) + '\n')


def run_worker(db_path: str | Path, worker_name: str, binary: str, workers: int = 1,
               threads_per_worker: int = 1, lease_seconds: int = 900,
               max_jobs: Optional[int] = None, analyse: Optional[Analyse] = None,
               results_path: Optional[str | Path] = None, max_attempts: int = 3,
               verbose: bool = False) -> dict:
    """Run `workers` concurrent workers against one analysis database.

    Each thread loops: `claim_next` (leasing a job so a crash costs at most the
    leases it holds) -> `analyse(fen, nodes_budget, multipv)` -> write the result
    into the ledger and into the existing `eval` cache. Killing the process at
    any point leaves the ledger consistent; re-running it picks up the pending
    queue, and leases left behind come back after `lease_seconds`.

    A job that raises is retried until `max_attempts` (the lease already counted
    the attempt) and then marked `failed`, with the error stored in
    `result_json`. `nodes_actual` charges what the engine reported and falls back
    to the node budget, which is what was actually asked for. `results_path`, if
    given, appends the same JSONL records `import_results` consumes.
    """
    if workers < 1:
        raise ValueError('workers must be at least 1')
    analyser = analyse or make_analyser(binary, threads_per_worker=threads_per_worker)
    bootstrap = studydb.connect(db_path, create=False)
    ensure_schema(bootstrap)
    bootstrap.close()
    counter = _Counter(max_jobs)
    totals = {'claimed': 0, 'done': 0, 'failed': 0, 'requeued': 0, 'nodes_done': 0}
    totals_lock = threading.Lock()
    write_lock = threading.Lock()
    results_file = Path(results_path) if results_path else None
    if results_file is not None:
        results_file.parent.mkdir(parents=True, exist_ok=True)

    def bump(key: str, amount: int = 1) -> None:
        with totals_lock:
            totals[key] += amount

    def loop(index: int) -> None:
        name = worker_name if workers == 1 else f'{worker_name}-{index}'
        db = studydb.connect(db_path, create=False)
        store = evals_module.EvalStore(db)
        try:
            while not counter.exhausted():
                job = claim_next(db, name, lease_seconds=lease_seconds)
                if job is None:
                    return
                counter.take()
                bump('claimed')
                record = {'job_id': job['job_id'], 'depth_achieved': None, 'nodes_actual': None,
                          'time_ms': None, 'result': None}
                try:
                    result = analyser(job['fen'], job['nodes_budget'], job['multipv'])
                    if not result:
                        raise RuntimeError('analysis returned no result')
                    depth = result.get('depth')
                    nodes_actual = result.get('nodes') or job['nodes_budget']
                    time_ms = result.get('time_ms')
                    db.execute('''UPDATE engine_job SET state='done', depth_achieved=?,
                                  nodes_actual=?, time_ms=?, result_json=?, finished_at=?,
                                  worker=NULL, leased_at=NULL
                                  WHERE job_id=? AND state='leased' ''',
                               (depth, nodes_actual, time_ms, json.dumps(result), _utc(),
                                job['job_id']))
                    db.commit()
                    store.store(job['fen'], job['multipv'], result)
                    bump('done')
                    bump('nodes_done', nodes_actual)
                    record.update({'depth_achieved': depth, 'nodes_actual': nodes_actual,
                                   'time_ms': time_ms, 'result': result})
                    if verbose:
                        print(f'{name}: {job["job_id"][:12]} tier {job["tier"]} '
                              f'nodes {nodes_actual:,} depth {depth}')
                except Exception as error:              # engine died, bad position, bad result
                    retry = job['attempts'] < max_attempts
                    state = 'pending' if retry else 'failed'
                    db.execute('''UPDATE engine_job SET state=?, worker=NULL, leased_at=NULL,
                                  result_json=? WHERE job_id=? AND state='leased' ''',
                               (state, json.dumps({'error': f'{type(error).__name__}: {error}'}),
                                job['job_id']))
                    db.commit()
                    bump('requeued' if retry else 'failed')
                    record['error'] = f'{type(error).__name__}: {error}'
                    if verbose:
                        print(f'{name}: {job["job_id"][:12]} failed ({record["error"]}), '
                              f'-> {state}')
                if results_file is not None:
                    _append_jsonl(results_file, record, write_lock)
        finally:
            db.close()

    if workers == 1:
        loop(0)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(loop, index) for index in range(workers)]
            for future in futures:
                future.result()
    if isinstance(analyser, ThreadLocalAnalyser):
        analyser.close()
    totals.update({'worker': worker_name, 'workers': workers,
                   'threads_per_worker': threads_per_worker})
    return totals


# ----------------------------------------------------------------- benchmark
def _mean(values: Sequence[Optional[int]]) -> Optional[float]:
    present = [value for value in values if value is not None]
    return (sum(present) / len(present)) if present else None


def _topology_label(topology: dict) -> str:
    return f"{topology['workers']}x{topology['threads_per_worker']}"


def _default_analyse_factory(binary: str) -> AnalyseFactory:
    def factory(topology: dict) -> Analyse:
        return make_analyser(binary, threads_per_worker=int(topology['threads_per_worker']))
    return factory


def benchmark_topologies(binary: str, topologies: Iterable[dict], fens: Sequence[str],
                         nodes_per: int = 1_000_000, repeat: int = 1,
                         analyse_factory: Optional[AnalyseFactory] = None,
                         progress: Optional[Callable[[str, int, int], None]] = None) -> dict:
    """Measure aggregate nodes/second of each topology on the same positions.

    Every fen is searched `repeat` times at exactly `nodes_per` nodes with
    multipv equal to `campaign.MULTIPV` — the benchmark runs at the campaign's
    answer width and may not rescale the requested budget. Wall time is measured
    from the first worker start to the last worker finish, so thread-pool
    construction is excluded from the rate. Aggregation is exactly
    `total_nodes / wall_seconds`; `total_nodes` is what the engines reported
    (the requested budget when the engine reports nothing).

    Results are sorted by aggregate nodes/second descending, with `best` naming
    the winner, and `cores`/`note` recording what `os.cpu_count()` saw.
    """
    if repeat < 1:
        raise ValueError('repeat must be at least 1')
    if not fens:
        raise ValueError('no fens to benchmark')
    factory = analyse_factory or _default_analyse_factory(binary)
    work = [fen for _ in range(repeat) for fen in fens]
    cores = os.cpu_count()
    entries = []
    for topology in topologies:
        workers = int(topology['workers'])
        threads = int(topology['threads_per_worker'])
        if workers < 1 or threads < 1:
            raise ValueError(f'invalid topology {topology}')
        label = _topology_label(topology)
        analyser = factory(topology)
        queue: Queue = Queue()
        for job in work:
            queue.put(job)
        starts: list[float] = []
        finishes: list[float] = []
        reported: list[dict] = []
        lock = threading.Lock()

        def spin() -> None:
            with lock:
                starts.append(time.perf_counter())
            while True:
                try:
                    fen = queue.get_nowait()
                except Empty:
                    break
                result = analyser(fen, nodes_per, MULTIPV)
                with lock:
                    reported.append(result if isinstance(result, dict) else {})
                    if progress is not None:
                        progress(label, len(reported), len(work))
            with lock:
                finishes.append(time.perf_counter())

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(spin) for _ in range(workers)]
            for future in futures:
                future.result()
        if isinstance(analyser, ThreadLocalAnalyser):
            analyser.close()
        wall = max(finishes) - min(starts)
        total_nodes = sum(int(result.get('nodes') or nodes_per) for result in reported)
        entry = {'workers': workers, 'threads_per_worker': threads, 'label': label,
                 'wall_seconds': wall, 'total_nodes': total_nodes,
                 'aggregate_nps': (total_nodes / wall) if wall > 0 else None,
                 'mean_depth_achieved': _mean([result.get('depth') for result in reported]),
                 'positions': len(reported)}
        entries.append(entry)
    entries.sort(key=lambda entry: entry['aggregate_nps'] if entry['aggregate_nps'] else 0.0,
                 reverse=True)
    best = entries[0]['label'] if entries else None
    return {'topologies': entries, 'best': best, 'cores': cores,
            'note': f'{cores} logical cores detected by os.cpu_count(); '
                    f'aggregate_nps = total_nodes / wall_seconds, measured from first start '
                    f'to last finish at {nodes_per} nodes and multipv {MULTIPV}',
            'nodes_per': nodes_per, 'multipv': MULTIPV, 'repeat': repeat, 'fens': len(fens)}


# ------------------------------------------------------------------------ CLI
def _candidate_fens(db, campaign_name: str, limit: int) -> list[str]:
    rows = db.execute('''SELECT fen FROM evaluation_candidate WHERE selected=1 AND campaign=?
                         ORDER BY -score''', (campaign_name,)).fetchall()
    seen, out = set(), []
    for row in rows:
        fen = row[0]
        if fen in seen:
            continue
        seen.add(fen)
        out.append(fen)
        if len(out) >= limit:
            break
    return out


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', type=Path, default=studydb.DEFAULT_ANALYSIS_DB)
    sub = parser.add_subparsers(dest='command', required=True)

    plan = sub.add_parser('plan', help='insert one pending job per selected candidate')
    plan.add_argument('--campaign', default=DEFAULT_CAMPAIGN)
    plan.add_argument('--engine-version', default=None,
                      help='engine identity; part of the job key (or give --binary)')
    plan.add_argument('--binary', default=None,
                      help='derive the engine version from this binary name/size/mtime')
    plan.add_argument('--write', action='store_true', help='actually insert (default is a dry run)')

    export = sub.add_parser('export', help='write the pending queue as JSONL')
    export.add_argument('--out', type=Path, required=True)

    imp = sub.add_parser('import', help='fold JSONL results back into the ledger')
    imp.add_argument('--in', dest='in_path', type=Path, required=True)

    imp_jobs = sub.add_parser('import-jobs',
                              help='load a shipped JSONL job bundle into a fresh ledger')
    imp_jobs.add_argument('--in', dest='in_path', type=Path, required=True)
    imp_jobs.add_argument('--campaign', default=None)

    sub.add_parser('stats', help='print the ledger summary')

    worker = sub.add_parser('worker', help='run the queue')
    worker.add_argument('--worker-name', default=os.uname().nodename)
    worker.add_argument('--binary', required=True)
    worker.add_argument('--workers', type=int, default=1)
    worker.add_argument('--threads-per-worker', type=int, default=1)
    worker.add_argument('--lease-seconds', type=int, default=900)
    worker.add_argument('--max-jobs', type=int, default=None)
    worker.add_argument('--results', type=Path, default=None)

    bench = sub.add_parser('benchmark', help='compare worker topologies by aggregate nodes/sec')
    bench.add_argument('--binary', required=True)
    bench.add_argument('--nodes', type=int, default=1_000_000)
    bench.add_argument('--fens', type=int, default=8)
    bench.add_argument('--repeat', type=int, default=1)
    bench.add_argument('--campaign', default=DEFAULT_CAMPAIGN)
    bench.add_argument('--topology', action='append', default=None,
                       help='WxT, e.g. 4x1 or 2x2; repeatable (default: cores x1 and cores/2 x2)')

    args = parser.parse_args(argv)
    if args.command == 'plan':
        engine_version = args.engine_version or (binary_fingerprint(args.binary) if args.binary
                                                 else None)
        if not engine_version:
            parser.error('plan needs --engine-version (or --binary): it is part of the job key')
        db = studydb.connect(args.db, create=False)
        if not args.write:
            # A dry run must not write anything, not even the ledger: check for the
            # table instead of creating it, and treat "no ledger" as "nothing covered".
            has_ledger = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                    "AND name='engine_job'").fetchone() is not None
            rows = db.execute('''SELECT fen, tier FROM evaluation_candidate
                                 WHERE selected=1 AND campaign=?''', (args.campaign,)).fetchall()
            would = skipped = 0
            for fen, tier in rows:
                nodes = NODE_TIERS.get(tier)
                if nodes is None:
                    continue
                exists = None
                if has_ledger:
                    exists = db.execute('SELECT 1 FROM engine_job WHERE job_id=?',
                                        (job_id_for(fen, MULTIPV, nodes, engine_version),)).fetchone()
                would += 1 if exists is None else 0
                skipped += 1 if exists is not None else 0
            report = {'dry_run': True, 'would_insert': would, 'already_covered': skipped,
                      'candidates': len(rows), 'engine_version': engine_version,
                      'ledger_present': has_ledger,
                      'nodes_per_tier': NODE_TIERS, 'multipv': MULTIPV}
        else:
            report = plan_jobs(db, args.campaign, engine_version)
            report['dry_run'] = False
        print(json.dumps(report, indent=1))
        db.close()
    elif args.command == 'export':
        db = studydb.connect(args.db, create=False)
        print(json.dumps(export_jobs(db, args.out), indent=1))
        db.close()
    elif args.command == 'import':
        db = studydb.connect(args.db, create=False)
        print(json.dumps(import_results(db, args.in_path), indent=1))
        db.close()
    elif args.command == 'import-jobs':
        db = studydb.connect(args.db, create=False)
        print(json.dumps(import_jobs(db, args.in_path, args.campaign), indent=1))
        db.close()
    elif args.command == 'stats':
        db = studydb.connect(args.db, create=False)
        print(json.dumps(stats(db), indent=1))
        db.close()
    elif args.command == 'worker':
        totals = run_worker(args.db, args.worker_name, args.binary, workers=args.workers,
                            threads_per_worker=args.threads_per_worker,
                            lease_seconds=args.lease_seconds, max_jobs=args.max_jobs,
                            results_path=args.results, verbose=True)
        print(json.dumps(totals, indent=1))
    elif args.command == 'benchmark':
        db = studydb.connect(args.db, create=False)
        fens = _candidate_fens(db, args.campaign, args.fens)
        db.close()
        if args.topology:
            topologies = []
            for text in args.topology:
                workers, _, threads = text.partition('x')
                topologies.append({'workers': int(workers), 'threads_per_worker': int(threads or 1)})
        else:
            cores = os.cpu_count() or 1
            topologies = [{'workers': cores, 'threads_per_worker': 1},
                          {'workers': max(1, cores // 2), 'threads_per_worker': 2}]
        report = benchmark_topologies(args.binary, topologies, fens, nodes_per=args.nodes,
                                      repeat=args.repeat)
        print(json.dumps(report, indent=1))


if __name__ == '__main__':
    main()
