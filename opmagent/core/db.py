"""SQLite access shared by all servers."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from importlib import resources
from pathlib import Path


def schema_sql() -> str:
    return resources.files("opmagent.core").joinpath("schema.sql").read_text()


def connect(path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)  # autocommit; callers serialize access
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 5000")
    if con.execute("SELECT 1 FROM sqlite_master WHERE name = 'decks'").fetchone():
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'res_model_versions'").fetchone():
            con.close()
            raise OldSchemaError(
                f"{path} was created before 0.3 (no reservoir-engineering models: SCAL, rock, contacts and regions "
                "moved from the geo model to the res model). Create a fresh database: move this one aside and run "
                "`opmagent init-db` (or `opmagent setup <workspace>` for a new workspace), then rebuild the studies.")
        migrate(con)
    return con


class OldSchemaError(RuntimeError):
    """The database predates the current schema and can't be upgraded in place."""


files_tbl_sql = """        CREATE TABLE data_files (
        id INTEGER PRIMARY KEY, study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
        file_id INTEGER NOT NULL REFERENCES files(id), format TEXT NOT NULL, category TEXT NOT NULL,
        path TEXT NOT NULL, original TEXT NOT NULL, well_name TEXT, sha256 TEXT NOT NULL, size_bytes INTEGER,
        mtime REAL, meta_json TEXT, status TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','changed','missing')),
        loaded_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE (study_id, path));
"""
curves_sql = """        CREATE TABLE data_log_curves (
        id INTEGER PRIMARY KEY, study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
        data_file_id INTEGER NOT NULL REFERENCES data_files(id) ON DELETE CASCADE, well_name TEXT NOT NULL,
        mnemonic TEXT NOT NULL, unit TEXT, description TEXT, samples INTEGER, top REAL, base REAL,
        min REAL, mean REAL, max REAL, UNIQUE (data_file_id, mnemonic));
        CREATE INDEX idx_data_log_curves ON data_log_curves(study_id, well_name, mnemonic);
"""

MIGRATIONS = [
    # (name, SQL) applied in order if the object is missing; keeps older databases current
    ("runs", """CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY, deck_id INTEGER NOT NULL REFERENCES decks(id),
        status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed','stored')),
        engine TEXT, out_dir TEXT NOT NULL, pid INTEGER, returncode INTEGER, log_path TEXT, smspec_path TEXT,
        note TEXT, started_at TEXT, finished_at TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE IF NOT EXISTS results (run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        vector TEXT NOT NULL, time_days REAL NOT NULL, value REAL NOT NULL, PRIMARY KEY (run_id, vector, time_days));
        CREATE INDEX IF NOT EXISTS idx_runs_deck ON runs(deck_id);"""),
    # Studies (v0.2): the shared board agents coordinate through. A study's tasks form a fixed chain
    # data -> fluid, geo -> res, well -> deck -> run -> view; requests carry 'data missing / needs work' back and forth.
    ("studies", """CREATE TABLE studies (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE CHECK (name GLOB '[A-Za-z0-9_-]*' AND length(name) > 0),
        requirement TEXT NOT NULL, spec_json TEXT NOT NULL, folder TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','complete','cancelled')),
        created_by TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE study_tasks (
        id INTEGER PRIMARY KEY, study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
        task TEXT NOT NULL CHECK (task IN ('data','fluid','geo','res','well','deck','run','view')),
        status TEXT NOT NULL DEFAULT 'waiting'
          CHECK (status IN ('waiting','ready','in_progress','blocked','done','failed')),
        depends_on TEXT NOT NULL DEFAULT '', result_json TEXT, note TEXT,
        updated_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE (study_id, task));
        CREATE TABLE study_requests (
        id INTEGER PRIMARY KEY, study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
        from_task TEXT NOT NULL, to_task TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('missing_data','improve_data','question')),
        message TEXT NOT NULL CHECK (length(trim(message)) > 0),
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved')),
        resolution TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')), resolved_at TEXT,
        CHECK (status = 'open' OR (resolution IS NOT NULL AND length(trim(resolution)) > 0)));
        CREATE TABLE study_events (
        id INTEGER PRIMARY KEY, study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
        task TEXT, event TEXT NOT NULL, detail TEXT, actor TEXT, at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TRIGGER study_events_no_update BEFORE UPDATE ON study_events
        BEGIN SELECT RAISE(ABORT, 'study_events is append-only'); END;
        CREATE TRIGGER study_events_no_delete BEFORE DELETE ON study_events
        WHEN (SELECT COUNT(*) FROM studies WHERE id = OLD.study_id) > 0
        BEGIN SELECT RAISE(ABORT, 'study_events is append-only'); END;"""),
    # Study data (v0.2): the data manager files every source from the inbox into <study>/data/ as it came, and
    # converts the tabular data into these tables; the modelers read them (through opmagent.data.store) for their
    # analysis and build from them. Each row points at the source (file and sheet) it came from. File-native data
    # (LAS, SEG-Y, surfaces, documents ...) stays a file: data_files is its catalog, data_log_curves describes
    # each LAS curve (with statistics). A reload replaces a study's rows. Names follow the modelers' columns.
    ("data_sources", """CREATE TABLE data_sources (
        id INTEGER PRIMARY KEY, study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
        file_id INTEGER REFERENCES files(id), path TEXT NOT NULL, label TEXT NOT NULL, dataset TEXT NOT NULL,
        rows INTEGER, loaded_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE data_wells (
        id INTEGER PRIMARY KEY, study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
        source_id INTEGER REFERENCES data_sources(id) ON DELETE CASCADE, well_name TEXT NOT NULL,
        well_type TEXT, phase TEXT, group_name TEXT, x REAL, y REAL, ref_depth REAL, i INTEGER, j INTEGER,
        UNIQUE (study_id, well_name));
        CREATE TABLE data_tops (
        id INTEGER PRIMARY KEY, study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
        source_id INTEGER REFERENCES data_sources(id) ON DELETE CASCADE, well_name TEXT NOT NULL,
        surface TEXT NOT NULL, x REAL NOT NULL, y REAL NOT NULL, tvd REAL NOT NULL,
        UNIQUE (study_id, well_name, surface));
        CREATE TABLE data_surveys (
        id INTEGER PRIMARY KEY, study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
        source_id INTEGER REFERENCES data_sources(id) ON DELETE CASCADE, well_name TEXT NOT NULL,
        md REAL NOT NULL, x REAL, y REAL, tvd REAL, inc REAL, azi REAL, x0 REAL, y0 REAL);
        CREATE TABLE data_files (
        id INTEGER PRIMARY KEY, study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
        file_id INTEGER NOT NULL REFERENCES files(id), format TEXT NOT NULL, category TEXT NOT NULL,
        path TEXT NOT NULL, original TEXT NOT NULL, well_name TEXT, sha256 TEXT NOT NULL, size_bytes INTEGER,
        mtime REAL, meta_json TEXT, status TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','changed','missing')),
        loaded_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE (study_id, path));
        CREATE TABLE data_log_curves (
        id INTEGER PRIMARY KEY, study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
        data_file_id INTEGER NOT NULL REFERENCES data_files(id) ON DELETE CASCADE, well_name TEXT NOT NULL,
        mnemonic TEXT NOT NULL, unit TEXT, description TEXT, samples INTEGER, top REAL, base REAL,
        min REAL, mean REAL, max REAL, UNIQUE (data_file_id, mnemonic));
        CREATE INDEX idx_data_log_curves ON data_log_curves(study_id, well_name, mnemonic);
        CREATE TABLE data_perforations (
        id INTEGER PRIMARY KEY, study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
        source_id INTEGER REFERENCES data_sources(id) ON DELETE CASCADE, well_name TEXT NOT NULL,
        date TEXT NOT NULL, top_md REAL NOT NULL, bottom_md REAL NOT NULL, state TEXT, skin REAL, diameter REAL);
        CREATE TABLE data_controls (
        id INTEGER PRIMARY KEY, study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
        source_id INTEGER REFERENCES data_sources(id) ON DELETE CASCADE, well_name TEXT NOT NULL,
        date TEXT NOT NULL, control TEXT NOT NULL, rate REAL, bhp REAL);
        CREATE TABLE data_production (
        id INTEGER PRIMARY KEY, study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
        source_id INTEGER REFERENCES data_sources(id) ON DELETE CASCADE, well_name TEXT NOT NULL,
        date TEXT NOT NULL, oil REAL, water REAL, gas REAL, water_inj REAL, gas_inj REAL, bhp REAL, thp REAL,
        hours REAL);
        CREATE INDEX idx_data_production ON data_production(study_id, well_name, date);
        CREATE TABLE data_pvt_samples (
        id INTEGER PRIMARY KEY, study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
        source_id INTEGER REFERENCES data_sources(id) ON DELETE CASCADE, sample_name TEXT,
        temperature_c REAL, psat_bar REAL, unit_system TEXT, salinity_ppm REAL, p_min_bar REAL, p_max_bar REAL,
        gor_sm3_sm3 REAL, bo_fb REAL, api REAL, selected INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE data_pvt_composition (
        sample_id INTEGER NOT NULL REFERENCES data_pvt_samples(id) ON DELETE CASCADE,
        component TEXT NOT NULL, mol_percent REAL NOT NULL, mw_g_mol REAL, sg REAL,
        PRIMARY KEY (sample_id, component));
        CREATE TABLE data_pvt_tests (
        id INTEGER PRIMARY KEY, sample_id INTEGER NOT NULL REFERENCES data_pvt_samples(id) ON DELETE CASCADE,
        test TEXT NOT NULL CHECK (test IN ('separator','cce','dl','viscosity')), stage INTEGER,
        pressure REAL NOT NULL, temperature REAL, relative_volume REAL, rs REAL, bo REAL, oil_density REAL,
        mu_o REAL);
        CREATE TABLE data_contacts (
        study_id INTEGER PRIMARY KEY REFERENCES studies(id) ON DELETE CASCADE,
        source_id INTEGER REFERENCES data_sources(id) ON DELETE CASCADE,
        datum_depth REAL, datum_pressure REAL, owc_depth REAL, goc_depth REAL);
        CREATE TABLE data_geo_spec (
        study_id INTEGER PRIMARY KEY REFERENCES studies(id) ON DELETE CASCADE,
        source_id INTEGER REFERENCES data_sources(id) ON DELETE CASCADE, spec_json TEXT NOT NULL);
        CREATE TABLE data_issues (
        id INTEGER PRIMARY KEY, study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
        modeler TEXT NOT NULL CHECK (modeler IN ('fluid','geo','res','well','data')),
        severity TEXT NOT NULL CHECK (severity IN ('required','decision','warning')),
        item TEXT NOT NULL, message TEXT NOT NULL);"""),
    # Upgrade for databases that got the first study-data shape (log samples in the database): logs stay files.
    ("data_files", files_tbl_sql.strip() + """
        DROP TABLE IF EXISTS data_log_samples; DROP TABLE IF EXISTS data_log_curves;
""" + curves_sql),
    # Study data from a deck (the reverse process): the data manager reads an existing deck and stores what each
    # modeler needs to rebuild it exactly. Tables (PVT, SCAL, contacts, wells, completions, schedule) are rows
    # here, one JSON record per model-table row; the grid and cell arrays stay files (data_files, category 'deck');
    # data_deck keeps the source, the model headers, the deck spec (dates, passthrough, schedule mode) and coverage.
    ("data_deck", """CREATE TABLE data_deck (
        study_id INTEGER PRIMARY KEY REFERENCES studies(id) ON DELETE CASCADE,
        source_path TEXT NOT NULL, source_file_id INTEGER REFERENCES files(id), folder TEXT NOT NULL,
        headers_json TEXT NOT NULL, spec_json TEXT NOT NULL, props_json TEXT NOT NULL, coverage_json TEXT,
        grid_json TEXT, loaded_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE data_deck_rows (
        id INTEGER PRIMARY KEY, study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('fluid','geo','res','well')), tbl TEXT NOT NULL, seq INTEGER NOT NULL,
        row_json TEXT NOT NULL);
        CREATE INDEX idx_data_deck_rows ON data_deck_rows(study_id, kind, tbl, seq);"""),
    # Review feedback: what a reviewer says about a model version, item by item, and the modeler's answer.
    # Only the reviewer writes comments; the modeler only answers them (with the version that addresses them).
    ("review_feedback", """CREATE TABLE review_feedback (
        id INTEGER PRIMARY KEY, model_kind TEXT NOT NULL CHECK (model_kind IN ('fluid','geo','res','well')),
        version_id INTEGER NOT NULL, model_name TEXT NOT NULL, item TEXT, comment TEXT NOT NULL
          CHECK (length(trim(comment)) > 0),
        severity TEXT NOT NULL DEFAULT 'change' CHECK (severity IN ('change','question','note')),
        reviewer TEXT NOT NULL CHECK (length(trim(reviewer)) > 0),
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','addressed','wontfix')),
        response TEXT, addressed_in_version_id INTEGER, created_at TEXT NOT NULL DEFAULT (datetime('now')),
        answered_at TEXT,
        CHECK (status = 'open' OR (response IS NOT NULL AND length(trim(response)) > 0)));
        CREATE INDEX idx_review_feedback ON review_feedback(model_kind, model_name, status);
        CREATE TRIGGER review_feedback_comment_fixed BEFORE UPDATE OF model_kind, version_id, model_name, item,
          comment, severity, reviewer, created_at ON review_feedback
        BEGIN SELECT RAISE(ABORT, 'a review comment cannot be changed; add a new one'); END;"""),
]


def migrate(con: sqlite3.Connection) -> list[str]:
    applied = []
    for name, sql in MIGRATIONS:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone():
            con.executescript(sql)
            applied.append(name)
    return applied


def init_db(path: str | Path) -> None:
    path = Path(path)
    if path.exists() and path.stat().st_size > 0:
        raise FileExistsError(f"{path} already exists; refusing to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = connect(path)
    con.executescript(schema_sql())
    migrate(con)   # tables added after schema.sql (studies) come from the migrations
    con.close()


@contextmanager
def transaction(con: sqlite3.Connection):
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except Exception:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")
