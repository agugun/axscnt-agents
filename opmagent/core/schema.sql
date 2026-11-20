-- =====================================================================
-- OPM simulation workflow — central SQLite database (black-oil scope)
--
-- Writers : Fluid modeler, Geo modeler, Well modeler  -> model versions
-- Reviewer: You (human)                               -> approvals
-- Reader  : Deck builder (reads approved models)       -> decks
-- Later   : Run agent                                  -> runs / results
--
-- Lifecycle of every model version
--
--     draft ──(validator passed + QC report)──► qc_ready ──(human)──► approved
--       ▲                                         │                    rejected
--       └───────────── agent revises ─────────────┘
--
--   * Data can only be written while a version is 'draft'. Once it is
--     'qc_ready', the data is frozen so what you review is what gets used.
--   * 'approved' / 'rejected' are set ONLY by inserting a row in
--     `approvals` (reviewer + decision + comment). A trigger then updates
--     the version. Any direct attempt to set them without an approval
--     record is aborted. Expose the approve/reject tool to humans only.
--   * approved and rejected versions are immutable (header and data).
--   * Decks can only reference approved versions of all three models.
--   * Small tables live in rows; large cell arrays live in files
--     (path + sha256 in `files`).
--   * Units: each version declares METRIC or FIELD; values are stored in
--     that system, as OPM expects them in the deck.
-- =====================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ---------------------------------------------------------------------
-- Files: inputs you provide, and files produced by agents
-- ---------------------------------------------------------------------
CREATE TABLE files (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN
                  ('lab_report','well_log','tops','surface','core','scal',
                   'deviation_survey','production_data','well_test','other_input',
                   'grid','property','vfp','qc_report','include','deck')),
    size_bytes  INTEGER,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (path, sha256)
);

-- =====================================================================
-- FLUID MODELS (Fluid modeler)
-- =====================================================================
CREATE TABLE fluid_models (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE fluid_model_versions (
    id                INTEGER PRIMARY KEY,
    fluid_model_id    INTEGER NOT NULL REFERENCES fluid_models(id),
    version           INTEGER NOT NULL DEFAULT 0,      -- 0 = auto-number
    status            TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft','qc_ready','approved','rejected')),
    unit_system       TEXT NOT NULL CHECK (unit_system IN ('METRIC','FIELD')),
    model_type        TEXT NOT NULL DEFAULT 'black_oil'
                        CHECK (model_type IN ('black_oil','dead_oil','dry_gas','wet_gas')),
    phases            TEXT NOT NULL,                   -- 'OIL,WATER,GAS'
    has_disgas        INTEGER NOT NULL DEFAULT 1,
    has_vapoil        INTEGER NOT NULL DEFAULT 0,
    user_description  TEXT,                            -- your description, verbatim
    spec_json         TEXT,                            -- agent's interpretation
    assumptions_json  TEXT,                            -- correlations/extrapolations used
    qc_json           TEXT,                            -- validator results + metrics
    qc_report_file_id INTEGER REFERENCES files(id),    -- HTML report with plots
    origin            TEXT NOT NULL DEFAULT 'built' CHECK (origin IN ('built','imported','demo')),
    source_deck_file_id INTEGER REFERENCES files(id),   -- set when origin = 'imported'
    created_by        TEXT NOT NULL DEFAULT 'fluid_modeler',
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    qc_ready_at       TEXT,
    decided_at        TEXT,
    UNIQUE (fluid_model_id, version)
);

CREATE TABLE fluid_version_sources (
    version_id INTEGER NOT NULL REFERENCES fluid_model_versions(id) ON DELETE CASCADE,
    file_id    INTEGER NOT NULL REFERENCES files(id),
    PRIMARY KEY (version_id, file_id)
);

-- PVTO (live oil). is_saturated=1 marks the bubble-point row of each Rs block.
CREATE TABLE pvto (
    version_id   INTEGER NOT NULL REFERENCES fluid_model_versions(id) ON DELETE CASCADE,
    pvtnum       INTEGER NOT NULL DEFAULT 1 CHECK (pvtnum >= 1),
    rs           REAL NOT NULL CHECK (rs >= 0),
    pressure     REAL NOT NULL CHECK (pressure > 0),
    bo           REAL NOT NULL CHECK (bo > 0),
    mu_o         REAL NOT NULL CHECK (mu_o > 0),
    is_saturated INTEGER NOT NULL CHECK (is_saturated IN (0,1)),
    PRIMARY KEY (version_id, pvtnum, rs, pressure)
);

-- PVDO (dead oil)
CREATE TABLE pvdo (
    version_id INTEGER NOT NULL REFERENCES fluid_model_versions(id) ON DELETE CASCADE,
    pvtnum     INTEGER NOT NULL DEFAULT 1 CHECK (pvtnum >= 1),
    pressure   REAL NOT NULL CHECK (pressure > 0),
    bo         REAL NOT NULL CHECK (bo > 0),
    mu_o       REAL NOT NULL CHECK (mu_o > 0),
    PRIMARY KEY (version_id, pvtnum, pressure)
);

-- PVDG (dry gas)
CREATE TABLE pvdg (
    version_id INTEGER NOT NULL REFERENCES fluid_model_versions(id) ON DELETE CASCADE,
    pvtnum     INTEGER NOT NULL DEFAULT 1 CHECK (pvtnum >= 1),
    pressure   REAL NOT NULL CHECK (pressure > 0),
    bg         REAL NOT NULL CHECK (bg > 0),
    mu_g       REAL NOT NULL CHECK (mu_g > 0),
    PRIMARY KEY (version_id, pvtnum, pressure)
);

-- PVTG (wet gas; only if has_vapoil=1)
CREATE TABLE pvtg (
    version_id   INTEGER NOT NULL REFERENCES fluid_model_versions(id) ON DELETE CASCADE,
    pvtnum       INTEGER NOT NULL DEFAULT 1 CHECK (pvtnum >= 1),
    pressure     REAL NOT NULL CHECK (pressure > 0),
    rv           REAL NOT NULL CHECK (rv >= 0),
    bg           REAL NOT NULL CHECK (bg > 0),
    mu_g         REAL NOT NULL CHECK (mu_g > 0),
    is_saturated INTEGER NOT NULL CHECK (is_saturated IN (0,1)),
    PRIMARY KEY (version_id, pvtnum, pressure, rv)
);

-- PVTW (water)
CREATE TABLE pvtw (
    version_id    INTEGER NOT NULL REFERENCES fluid_model_versions(id) ON DELETE CASCADE,
    pvtnum        INTEGER NOT NULL DEFAULT 1 CHECK (pvtnum >= 1),
    p_ref         REAL NOT NULL CHECK (p_ref > 0),
    bw            REAL NOT NULL CHECK (bw > 0),
    cw            REAL NOT NULL CHECK (cw >= 0),
    mu_w          REAL NOT NULL CHECK (mu_w > 0),
    viscosibility REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (version_id, pvtnum)
);

-- DENSITY (surface densities)
CREATE TABLE density (
    version_id INTEGER NOT NULL REFERENCES fluid_model_versions(id) ON DELETE CASCADE,
    pvtnum     INTEGER NOT NULL DEFAULT 1 CHECK (pvtnum >= 1),
    oil        REAL NOT NULL CHECK (oil > 0),
    water      REAL NOT NULL CHECK (water > 0),
    gas        REAL NOT NULL CHECK (gas > 0),
    PRIMARY KEY (version_id, pvtnum)
);

-- RSVD / PBVD (initial Rs or bubble point vs depth, per EQLNUM).
-- Use one or the other per region; validator enforces that.
CREATE TABLE rsvd (
    version_id INTEGER NOT NULL REFERENCES fluid_model_versions(id) ON DELETE CASCADE,
    eqlnum     INTEGER NOT NULL DEFAULT 1 CHECK (eqlnum >= 1),
    depth      REAL NOT NULL,
    rs         REAL NOT NULL CHECK (rs >= 0),
    PRIMARY KEY (version_id, eqlnum, depth)
);

CREATE TABLE pbvd (
    version_id INTEGER NOT NULL REFERENCES fluid_model_versions(id) ON DELETE CASCADE,
    eqlnum     INTEGER NOT NULL DEFAULT 1 CHECK (eqlnum >= 1),
    depth      REAL NOT NULL,
    pb         REAL NOT NULL CHECK (pb > 0),
    PRIMARY KEY (version_id, eqlnum, depth)
);

-- =====================================================================
-- GEO MODELS (Geo modeler)
-- Grid, properties, regions, faults, aquifers, rock compressibility,
-- saturation functions and initial contacts.
-- =====================================================================
CREATE TABLE geo_models (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE geo_model_versions (
    id                INTEGER PRIMARY KEY,
    geo_model_id      INTEGER NOT NULL REFERENCES geo_models(id),
    version           INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft','qc_ready','approved','rejected')),
    unit_system       TEXT NOT NULL CHECK (unit_system IN ('METRIC','FIELD')),
    grid_type         TEXT NOT NULL CHECK (grid_type IN ('cartesian','corner_point')),
    nx                INTEGER NOT NULL CHECK (nx > 0),
    ny                INTEGER NOT NULL CHECK (ny > 0),
    nz                INTEGER NOT NULL CHECK (nz > 0),
    -- uniform cartesian grids inline; non-uniform via DX/DY/DZ/TOPS arrays
    dx REAL, dy REAL, dz REAL, tops REAL,
    grid_file_id      INTEGER REFERENCES files(id),    -- COORD/ZCORN if corner_point
    minpv             REAL CHECK (minpv IS NULL OR minpv >= 0),   -- MINPV
    pinch_threshold   REAL CHECK (pinch_threshold IS NULL OR pinch_threshold >= 0), -- PINCH
    user_description  TEXT,
    spec_json         TEXT,
    assumptions_json  TEXT,
    qc_json           TEXT,                            -- PV, HCPV, active cells, checks
    qc_report_file_id INTEGER REFERENCES files(id),
    origin            TEXT NOT NULL DEFAULT 'built' CHECK (origin IN ('built','imported','demo')),
    source_deck_file_id INTEGER REFERENCES files(id),   -- set when origin = 'imported'
    created_by        TEXT NOT NULL DEFAULT 'geo_modeler',
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    qc_ready_at       TEXT,
    decided_at        TEXT,
    UNIQUE (geo_model_id, version),
    CHECK (grid_type = 'cartesian' OR grid_file_id IS NOT NULL)
);

CREATE TABLE geo_version_sources (
    version_id INTEGER NOT NULL REFERENCES geo_model_versions(id) ON DELETE CASCADE,
    file_id    INTEGER NOT NULL REFERENCES files(id),
    PRIMARY KEY (version_id, file_id)
);

-- Cell properties: constant value OR array file
CREATE TABLE geo_properties (
    version_id  INTEGER NOT NULL REFERENCES geo_model_versions(id) ON DELETE CASCADE,
    keyword     TEXT NOT NULL CHECK (keyword IN
                  ('DX','DY','DZ','TOPS',
                   'PORO','PERMX','PERMY','PERMZ','NTG','ACTNUM','SWATINIT',
                   'MULTX','MULTY','MULTZ','MULTX-','MULTY-','MULTZ-','MULTPV',
                   'SATNUM','PVTNUM','EQLNUM','FIPNUM','ROCKNUM')),
    const_value REAL,
    file_id     INTEGER REFERENCES files(id),
    min_value REAL, max_value REAL, mean_value REAL,
    PRIMARY KEY (version_id, keyword),
    CHECK ((const_value IS NULL) <> (file_id IS NULL))
);

-- FAULTS: fault segments (cell ranges + face)
CREATE TABLE faults (
    id         INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES geo_model_versions(id) ON DELETE CASCADE,
    fault_name TEXT NOT NULL CHECK (length(fault_name) <= 8),
    i1 INTEGER NOT NULL, i2 INTEGER NOT NULL,
    j1 INTEGER NOT NULL, j2 INTEGER NOT NULL,
    k1 INTEGER NOT NULL, k2 INTEGER NOT NULL,
    face       TEXT NOT NULL CHECK (face IN ('X','Y','Z','X-','Y-','Z-')),
    CHECK (i1 >= 1 AND j1 >= 1 AND k1 >= 1 AND i2 >= i1 AND j2 >= j1 AND k2 >= k1)
);

-- MULTFLT: transmissibility multiplier per fault (history-match knob)
CREATE TABLE fault_multipliers (
    version_id INTEGER NOT NULL REFERENCES geo_model_versions(id) ON DELETE CASCADE,
    fault_name TEXT NOT NULL,
    mult       REAL NOT NULL CHECK (mult >= 0),
    PRIMARY KEY (version_id, fault_name)
);

-- ROCK: compressibility per region
CREATE TABLE rock (
    version_id INTEGER NOT NULL REFERENCES geo_model_versions(id) ON DELETE CASCADE,
    region     INTEGER NOT NULL DEFAULT 1 CHECK (region >= 1),
    p_ref      REAL NOT NULL CHECK (p_ref > 0),
    cr         REAL NOT NULL CHECK (cr >= 0),
    PRIMARY KEY (version_id, region)
);

-- SWOF
CREATE TABLE swof (
    version_id INTEGER NOT NULL REFERENCES geo_model_versions(id) ON DELETE CASCADE,
    satnum     INTEGER NOT NULL DEFAULT 1 CHECK (satnum >= 1),
    sw         REAL NOT NULL CHECK (sw BETWEEN 0 AND 1),
    krw        REAL NOT NULL CHECK (krw BETWEEN 0 AND 1),
    krow       REAL NOT NULL CHECK (krow BETWEEN 0 AND 1),
    pcow       REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (version_id, satnum, sw)
);

-- SGOF
CREATE TABLE sgof (
    version_id INTEGER NOT NULL REFERENCES geo_model_versions(id) ON DELETE CASCADE,
    satnum     INTEGER NOT NULL DEFAULT 1 CHECK (satnum >= 1),
    sg         REAL NOT NULL CHECK (sg BETWEEN 0 AND 1),
    krg        REAL NOT NULL CHECK (krg BETWEEN 0 AND 1),
    krog       REAL NOT NULL CHECK (krog BETWEEN 0 AND 1),
    pcog       REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (version_id, satnum, sg)
);

-- Initial contacts per equilibration region (feeds EQUIL)
CREATE TABLE contacts (
    version_id     INTEGER NOT NULL REFERENCES geo_model_versions(id) ON DELETE CASCADE,
    eqlnum         INTEGER NOT NULL DEFAULT 1 CHECK (eqlnum >= 1),
    datum_depth    REAL NOT NULL,
    datum_pressure REAL NOT NULL CHECK (datum_pressure > 0),
    owc_depth      REAL,
    goc_depth      REAL,
    pcow_at_owc    REAL NOT NULL DEFAULT 0,
    pcog_at_goc    REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (version_id, eqlnum),
    CHECK (goc_depth IS NULL OR owc_depth IS NULL OR goc_depth <= owc_depth)
);

-- Analytical aquifers: Fetkovich (AQUFETP) or Carter-Tracy (AQUCT)
CREATE TABLE aquifers (
    version_id      INTEGER NOT NULL REFERENCES geo_model_versions(id) ON DELETE CASCADE,
    aquifer_id      INTEGER NOT NULL CHECK (aquifer_id >= 1),
    aq_type         TEXT NOT NULL CHECK (aq_type IN ('FETKOVICH','CARTER_TRACY')),
    datum_depth     REAL NOT NULL,
    init_pressure   REAL CHECK (init_pressure IS NULL OR init_pressure > 0), -- NULL = equilibrate
    total_compressibility REAL NOT NULL CHECK (total_compressibility > 0),
    pvtw_table      INTEGER NOT NULL DEFAULT 1,
    -- Fetkovich
    water_volume    REAL CHECK (water_volume IS NULL OR water_volume > 0),
    productivity_index REAL CHECK (productivity_index IS NULL OR productivity_index > 0),
    -- Carter-Tracy
    permeability    REAL CHECK (permeability IS NULL OR permeability > 0),
    porosity        REAL CHECK (porosity IS NULL OR porosity BETWEEN 0 AND 1),
    inner_radius    REAL CHECK (inner_radius IS NULL OR inner_radius > 0),
    thickness       REAL CHECK (thickness IS NULL OR thickness > 0),
    angle_deg       REAL CHECK (angle_deg IS NULL OR angle_deg BETWEEN 0 AND 360),
    influence_table INTEGER,
    PRIMARY KEY (version_id, aquifer_id),
    CHECK (aq_type <> 'FETKOVICH'   OR (water_volume IS NOT NULL AND productivity_index IS NOT NULL)),
    CHECK (aq_type <> 'CARTER_TRACY' OR (permeability IS NOT NULL AND porosity IS NOT NULL
                                         AND inner_radius IS NOT NULL AND thickness IS NOT NULL))
);

-- AQUANCON: aquifer-to-grid connections
CREATE TABLE aquifer_connections (
    id          INTEGER PRIMARY KEY,
    version_id  INTEGER NOT NULL,
    aquifer_id  INTEGER NOT NULL,
    i1 INTEGER NOT NULL, i2 INTEGER NOT NULL,
    j1 INTEGER NOT NULL, j2 INTEGER NOT NULL,
    k1 INTEGER NOT NULL, k2 INTEGER NOT NULL,
    face        TEXT NOT NULL CHECK (face IN ('I-','I+','J-','J+','K-','K+')),
    influx_coeff REAL,
    influx_mult  REAL NOT NULL DEFAULT 1 CHECK (influx_mult >= 0),
    FOREIGN KEY (version_id, aquifer_id) REFERENCES aquifers(version_id, aquifer_id) ON DELETE CASCADE,
    CHECK (i1 >= 1 AND j1 >= 1 AND k1 >= 1 AND i2 >= i1 AND j2 >= j1 AND k2 >= k1)
);

-- =====================================================================
-- WELL MODELS (Well modeler / production engineer)
-- Completions in MD (grid-independent) and/or IJK. IJK requires the
-- geo version it was built against.
-- =====================================================================
CREATE TABLE well_models (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE well_model_versions (
    id                INTEGER PRIMARY KEY,
    well_model_id     INTEGER NOT NULL REFERENCES well_models(id),
    version           INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft','qc_ready','approved','rejected')),
    unit_system       TEXT NOT NULL CHECK (unit_system IN ('METRIC','FIELD')),
    geo_version_id    INTEGER REFERENCES geo_model_versions(id),
    start_date        TEXT,
    user_description  TEXT,
    spec_json         TEXT,
    assumptions_json  TEXT,
    qc_json           TEXT,
    qc_report_file_id INTEGER REFERENCES files(id),
    origin            TEXT NOT NULL DEFAULT 'built' CHECK (origin IN ('built','imported','demo')),
    source_deck_file_id INTEGER REFERENCES files(id),   -- set when origin = 'imported'
    created_by        TEXT NOT NULL DEFAULT 'well_modeler',
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    qc_ready_at       TEXT,
    decided_at        TEXT,
    UNIQUE (well_model_id, version)
);

CREATE TABLE well_version_sources (
    version_id INTEGER NOT NULL REFERENCES well_model_versions(id) ON DELETE CASCADE,
    file_id    INTEGER NOT NULL REFERENCES files(id),
    PRIMARY KEY (version_id, file_id)
);

-- GRUPTREE: group hierarchy (FIELD is the implicit root)
CREATE TABLE well_groups (
    version_id   INTEGER NOT NULL REFERENCES well_model_versions(id) ON DELETE CASCADE,
    group_name   TEXT NOT NULL CHECK (length(group_name) <= 8 AND group_name <> 'FIELD'),
    parent_group TEXT NOT NULL DEFAULT 'FIELD',
    PRIMARY KEY (version_id, group_name)
);

-- WELSPECS (+ WEFAC efficiency factor)
CREATE TABLE wells (
    version_id        INTEGER NOT NULL REFERENCES well_model_versions(id) ON DELETE CASCADE,
    well_name         TEXT NOT NULL CHECK (length(well_name) <= 8),
    group_name        TEXT NOT NULL DEFAULT 'FIELD',
    well_type         TEXT NOT NULL CHECK (well_type IN ('PRODUCER','INJECTOR')),
    preferred_phase   TEXT NOT NULL CHECK (preferred_phase IN ('OIL','WATER','GAS','LIQ')),
    head_i INTEGER, head_j INTEGER,
    head_x REAL,    head_y REAL,
    ref_depth         REAL,
    wellbore_diameter REAL CHECK (wellbore_diameter IS NULL OR wellbore_diameter > 0),
    efficiency_factor REAL NOT NULL DEFAULT 1 CHECK (efficiency_factor > 0 AND efficiency_factor <= 1),
    PRIMARY KEY (version_id, well_name),
    CHECK ((head_i IS NOT NULL AND head_j IS NOT NULL) OR
           (head_x IS NOT NULL AND head_y IS NOT NULL)),
    CHECK (well_type <> 'INJECTOR' OR preferred_phase IN ('WATER','GAS'))
);

CREATE TABLE well_trajectory (
    version_id INTEGER NOT NULL,
    well_name  TEXT NOT NULL,
    md         REAL NOT NULL CHECK (md >= 0),
    x          REAL NOT NULL,
    y          REAL NOT NULL,
    tvd        REAL NOT NULL,
    PRIMARY KEY (version_id, well_name, md),
    FOREIGN KEY (version_id, well_name) REFERENCES wells(version_id, well_name) ON DELETE CASCADE
);

-- COMPDAT over time
CREATE TABLE well_completions (
    id          INTEGER PRIMARY KEY,
    version_id  INTEGER NOT NULL,
    well_name   TEXT NOT NULL,
    event_date  TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'OPEN' CHECK (state IN ('OPEN','SHUT')),
    top_md REAL, bottom_md REAL,
    i INTEGER, j INTEGER, k1 INTEGER, k2 INTEGER,
    diameter    REAL CHECK (diameter IS NULL OR diameter > 0),
    skin        REAL NOT NULL DEFAULT 0,
    conn_factor REAL CHECK (conn_factor IS NULL OR conn_factor > 0),
    kh          REAL CHECK (kh IS NULL OR kh > 0),
    FOREIGN KEY (version_id, well_name) REFERENCES wells(version_id, well_name) ON DELETE CASCADE,
    CHECK ((top_md IS NOT NULL AND bottom_md IS NOT NULL AND bottom_md >= top_md) OR
           (i IS NOT NULL AND j IS NOT NULL AND k1 IS NOT NULL AND k2 IS NOT NULL AND k2 >= k1))
);

-- WCONHIST / WCONINJH / WCONPROD / WCONINJE over time
CREATE TABLE well_events (
    id          INTEGER PRIMARY KEY,
    version_id  INTEGER NOT NULL,
    well_name   TEXT NOT NULL,
    event_date  TEXT NOT NULL,
    mode        TEXT NOT NULL CHECK (mode IN ('HISTORY','PREDICTION')),
    status      TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','SHUT','STOP')),
    control     TEXT CHECK (control IN ('ORAT','WRAT','GRAT','LRAT','RESV','BHP','THP','RATE')),
    oil_rate    REAL CHECK (oil_rate    IS NULL OR oil_rate    >= 0),
    water_rate  REAL CHECK (water_rate  IS NULL OR water_rate  >= 0),
    gas_rate    REAL CHECK (gas_rate    IS NULL OR gas_rate    >= 0),
    liquid_rate REAL CHECK (liquid_rate IS NULL OR liquid_rate >= 0),
    inj_rate    REAL CHECK (inj_rate    IS NULL OR inj_rate    >= 0),
    bhp         REAL CHECK (bhp IS NULL OR bhp > 0),
    thp         REAL CHECK (thp IS NULL OR thp > 0),
    vfp_table   INTEGER,
    FOREIGN KEY (version_id, well_name) REFERENCES wells(version_id, well_name) ON DELETE CASCADE,
    UNIQUE (version_id, well_name, event_date, mode)
);

-- WECON: economic limits per well
CREATE TABLE well_econ_limits (
    version_id   INTEGER NOT NULL,
    well_name    TEXT NOT NULL,
    min_oil_rate REAL CHECK (min_oil_rate IS NULL OR min_oil_rate >= 0),
    min_gas_rate REAL CHECK (min_gas_rate IS NULL OR min_gas_rate >= 0),
    max_wct      REAL CHECK (max_wct IS NULL OR max_wct BETWEEN 0 AND 1),
    max_gor      REAL CHECK (max_gor IS NULL OR max_gor > 0),
    max_wgr      REAL CHECK (max_wgr IS NULL OR max_wgr > 0),
    workover     TEXT NOT NULL DEFAULT 'NONE' CHECK (workover IN ('NONE','CON','+CON','WELL')),
    end_run      INTEGER NOT NULL DEFAULT 0 CHECK (end_run IN (0,1)),
    PRIMARY KEY (version_id, well_name),
    FOREIGN KEY (version_id, well_name) REFERENCES wells(version_id, well_name) ON DELETE CASCADE
);

-- GCONPROD / GCONINJE over time (group_name may be 'FIELD')
CREATE TABLE group_controls (
    id          INTEGER PRIMARY KEY,
    version_id  INTEGER NOT NULL REFERENCES well_model_versions(id) ON DELETE CASCADE,
    group_name  TEXT NOT NULL,
    event_date  TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('PROD','INJ')),
    control     TEXT NOT NULL CHECK (control IN
                  ('NONE','ORAT','WRAT','GRAT','LRAT','RESV','RATE','VREP','REIN','FLD')),
    inj_phase   TEXT CHECK (inj_phase IN ('WATER','GAS')),
    oil_rate    REAL, water_rate REAL, gas_rate REAL, liquid_rate REAL,
    resv_rate   REAL, inj_rate REAL,
    voidage_frac REAL CHECK (voidage_frac IS NULL OR voidage_frac >= 0),   -- VREP
    reinj_frac   REAL CHECK (reinj_frac   IS NULL OR reinj_frac   >= 0),   -- REIN
    CHECK (kind <> 'INJ' OR inj_phase IS NOT NULL),
    UNIQUE (version_id, group_name, event_date, kind, inj_phase)
);

-- VFP tables as include files
CREATE TABLE vfp_tables (
    version_id INTEGER NOT NULL REFERENCES well_model_versions(id) ON DELETE CASCADE,
    table_no   INTEGER NOT NULL CHECK (table_no >= 1),
    kind       TEXT NOT NULL CHECK (kind IN ('PROD','INJ')),
    file_id    INTEGER NOT NULL REFERENCES files(id),
    PRIMARY KEY (version_id, table_no, kind)
);

-- =====================================================================
-- HUMAN APPROVALS (append-only audit log)
-- Inserting a row here is the ONLY way a version becomes approved or
-- rejected. Expose this write to humans only, never to modeler agents.
-- =====================================================================
CREATE TABLE approvals (
    id          INTEGER PRIMARY KEY,
    model_kind  TEXT NOT NULL CHECK (model_kind IN ('fluid','geo','well')),
    version_id  INTEGER NOT NULL,
    decision    TEXT NOT NULL CHECK (decision IN ('approved','rejected')),
    reviewer    TEXT NOT NULL CHECK (length(trim(reviewer)) > 0),
    comment     TEXT,
    qc_report_file_id INTEGER REFERENCES files(id),   -- the report that was reviewed
    decided_at  TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (decision = 'approved' OR (comment IS NOT NULL AND length(trim(comment)) > 0))
);

-- =====================================================================
-- DECKS (Deck builder)
-- =====================================================================
CREATE TABLE decks (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    requirement_text TEXT NOT NULL,
    spec_json        TEXT NOT NULL,            -- dates, report steps, summary vectors, tuning
    fluid_version_id INTEGER NOT NULL REFERENCES fluid_model_versions(id),
    geo_version_id   INTEGER NOT NULL REFERENCES geo_model_versions(id),
    well_version_id  INTEGER NOT NULL REFERENCES well_model_versions(id),
    deck_file_id     INTEGER REFERENCES files(id),
    status           TEXT NOT NULL DEFAULT 'draft'
                       CHECK (status IN ('draft','validated','failed','ready')),
    validation_log   TEXT,
    origin           TEXT NOT NULL DEFAULT 'built' CHECK (origin IN ('built','imported','demo')),
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Keywords carried verbatim from an imported deck (no model home yet)
CREATE TABLE deck_passthrough (
    id        INTEGER PRIMARY KEY,
    deck_id   INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    section   TEXT NOT NULL CHECK (section IN ('RUNSPEC','GRID','EDIT','PROPS','REGIONS','SOLUTION','SUMMARY','SCHEDULE')),
    position  INTEGER NOT NULL,              -- order within section
    keyword   TEXT NOT NULL,
    raw_text  TEXT NOT NULL
);

-- Audit log of every write tool call (append-only)
CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY,
    tool        TEXT NOT NULL,
    profile     TEXT NOT NULL,
    args_sha256 TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TABLE deck_files (
    deck_id INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES files(id),
    role    TEXT NOT NULL,
    PRIMARY KEY (deck_id, file_id)
);

-- =====================================================================
-- TRIGGERS
-- =====================================================================

-- ---- Approvals: only on qc_ready versions, report must match, append-only
CREATE TRIGGER approvals_require_qc_ready
BEFORE INSERT ON approvals
BEGIN
    SELECT RAISE(ABORT, 'version not found or not qc_ready')
     WHERE (CASE NEW.model_kind
              WHEN 'fluid' THEN (SELECT status FROM fluid_model_versions WHERE id = NEW.version_id)
              WHEN 'geo'   THEN (SELECT status FROM geo_model_versions   WHERE id = NEW.version_id)
              WHEN 'well'  THEN (SELECT status FROM well_model_versions  WHERE id = NEW.version_id)
            END) IS NOT 'qc_ready';
    SELECT RAISE(ABORT, 'reviewed QC report does not match the version''s current report')
     WHERE NEW.qc_report_file_id IS NOT
           (CASE NEW.model_kind
              WHEN 'fluid' THEN (SELECT qc_report_file_id FROM fluid_model_versions WHERE id = NEW.version_id)
              WHEN 'geo'   THEN (SELECT qc_report_file_id FROM geo_model_versions   WHERE id = NEW.version_id)
              WHEN 'well'  THEN (SELECT qc_report_file_id FROM well_model_versions  WHERE id = NEW.version_id)
            END);
END;

CREATE TRIGGER approvals_apply
AFTER INSERT ON approvals
BEGIN
    UPDATE fluid_model_versions SET status = NEW.decision, decided_at = NEW.decided_at
     WHERE NEW.model_kind = 'fluid' AND id = NEW.version_id;
    UPDATE geo_model_versions   SET status = NEW.decision, decided_at = NEW.decided_at
     WHERE NEW.model_kind = 'geo'   AND id = NEW.version_id;
    UPDATE well_model_versions  SET status = NEW.decision, decided_at = NEW.decided_at
     WHERE NEW.model_kind = 'well'  AND id = NEW.version_id;
END;

CREATE TRIGGER approvals_no_update BEFORE UPDATE ON approvals
BEGIN SELECT RAISE(ABORT, 'approvals are append-only'); END;
CREATE TRIGGER approvals_no_delete BEFORE DELETE ON approvals
BEGIN SELECT RAISE(ABORT, 'approvals are append-only'); END;

-- ---- Decks: only approved, consistent models
CREATE TRIGGER decks_require_approved_models
BEFORE INSERT ON decks
BEGIN
    SELECT RAISE(ABORT, 'fluid model version is not approved')
     WHERE (SELECT status FROM fluid_model_versions WHERE id = NEW.fluid_version_id) IS NOT 'approved';
    SELECT RAISE(ABORT, 'geo model version is not approved')
     WHERE (SELECT status FROM geo_model_versions   WHERE id = NEW.geo_version_id)   IS NOT 'approved';
    SELECT RAISE(ABORT, 'well model version is not approved')
     WHERE (SELECT status FROM well_model_versions  WHERE id = NEW.well_version_id)  IS NOT 'approved';
    SELECT RAISE(ABORT, 'demo and non-demo data cannot be mixed in a deck')
     WHERE ((SELECT origin FROM fluid_model_versions WHERE id = NEW.fluid_version_id) = 'demo') <> (NEW.origin = 'demo')
        OR ((SELECT origin FROM geo_model_versions   WHERE id = NEW.geo_version_id)   = 'demo') <> (NEW.origin = 'demo')
        OR ((SELECT origin FROM well_model_versions  WHERE id = NEW.well_version_id)  = 'demo') <> (NEW.origin = 'demo');
    SELECT RAISE(ABORT, 'fluid and geo unit systems differ')
     WHERE (SELECT unit_system FROM fluid_model_versions WHERE id = NEW.fluid_version_id)
        <> (SELECT unit_system FROM geo_model_versions   WHERE id = NEW.geo_version_id);
    SELECT RAISE(ABORT, 'well and geo unit systems differ')
     WHERE (SELECT unit_system FROM well_model_versions WHERE id = NEW.well_version_id)
        <> (SELECT unit_system FROM geo_model_versions  WHERE id = NEW.geo_version_id);
    SELECT RAISE(ABORT, 'well model was built against a different geo model version')
     WHERE (SELECT geo_version_id FROM well_model_versions WHERE id = NEW.well_version_id) IS NOT NULL
       AND (SELECT geo_version_id FROM well_model_versions WHERE id = NEW.well_version_id) <> NEW.geo_version_id;
END;

CREATE TRIGGER decks_models_frozen
BEFORE UPDATE OF fluid_version_id, geo_version_id, well_version_id ON decks
BEGIN SELECT RAISE(ABORT, 'a deck''s model versions cannot be changed; create a new deck'); END;

-- ---- IJK completions need a geo version
CREATE TRIGGER well_ijk_needs_geo
BEFORE INSERT ON well_completions
WHEN NEW.i IS NOT NULL
 AND (SELECT geo_version_id FROM well_model_versions WHERE id = NEW.version_id) IS NULL
BEGIN
    SELECT RAISE(ABORT, 'IJK completions require well_model_versions.geo_version_id');
END;

-- ---- Version status rules, immutability, auto-numbering (generated per kind below)

-- ======================= FLUID lifecycle =======================
CREATE TRIGGER fluid_insert_as_draft BEFORE INSERT ON fluid_model_versions
WHEN NEW.status <> 'draft'
BEGIN SELECT RAISE(ABORT, 'new fluid versions must start as draft'); END;

CREATE TRIGGER fluid_version_autonumber AFTER INSERT ON fluid_model_versions
WHEN NEW.version = 0
BEGIN
    UPDATE fluid_model_versions SET version = (SELECT COALESCE(MAX(version),0) + 1 FROM fluid_model_versions
                                WHERE fluid_model_id = NEW.fluid_model_id AND id <> NEW.id)
     WHERE id = NEW.id;
END;

CREATE TRIGGER fluid_status_rules BEFORE UPDATE ON fluid_model_versions
BEGIN
    SELECT RAISE(ABORT, 'fluid version is approved/rejected and immutable; create a new version')
     WHERE OLD.status IN ('approved','rejected');
    SELECT RAISE(ABORT, 'fluid version is qc_ready (frozen for review); move back to draft to edit')
     WHERE OLD.status = 'qc_ready' AND NEW.status = 'qc_ready'
       AND NOT (OLD.qc_ready_at IS NULL AND NEW.qc_ready_at IS NOT NULL);  -- allow the timestamp stamp only
    SELECT RAISE(ABORT, 'invalid status transition for fluid version')
     WHERE NOT ((OLD.status = 'draft'    AND NEW.status IN ('draft','qc_ready')) OR
                (OLD.status = 'qc_ready' AND NEW.status IN ('draft','qc_ready','approved','rejected')));
    SELECT RAISE(ABORT, 'qc_ready requires qc_json and a QC report file')
     WHERE NEW.status = 'qc_ready' AND (NEW.qc_json IS NULL OR NEW.qc_report_file_id IS NULL);
    SELECT RAISE(ABORT, 'approve/reject only through the approvals table (human review)')
     WHERE NEW.status IN ('approved','rejected')
       AND NOT EXISTS (SELECT 1 FROM approvals
                        WHERE model_kind = 'fluid' AND version_id = NEW.id
                          AND decision = NEW.status);
END;

CREATE TRIGGER fluid_stamp_qc_ready AFTER UPDATE OF status ON fluid_model_versions
WHEN NEW.status = 'qc_ready'
BEGIN UPDATE fluid_model_versions SET qc_ready_at = datetime('now') WHERE id = NEW.id AND qc_ready_at IS NULL; END;

CREATE TRIGGER fluid_reset_qc_on_draft AFTER UPDATE OF status ON fluid_model_versions
WHEN NEW.status = 'draft' AND OLD.status = 'qc_ready'
BEGIN UPDATE fluid_model_versions SET qc_ready_at = NULL WHERE id = NEW.id; END;

CREATE TRIGGER fluid_no_delete_after_draft BEFORE DELETE ON fluid_model_versions
WHEN OLD.status <> 'draft'
BEGIN SELECT RAISE(ABORT, 'only draft fluid versions can be deleted'); END;

CREATE VIEW latest_approved_fluid AS
SELECT m.name AS model_name, v.*
  FROM fluid_model_versions v JOIN fluid_models m ON m.id = v.fluid_model_id
 WHERE v.status = 'approved'
   AND v.version = (SELECT MAX(version) FROM fluid_model_versions
                     WHERE fluid_model_id = v.fluid_model_id AND status = 'approved');

CREATE VIEW pending_review_fluid AS
SELECT m.name AS model_name, v.id, v.version, v.qc_ready_at, v.qc_report_file_id, v.qc_json
  FROM fluid_model_versions v JOIN fluid_models m ON m.id = v.fluid_model_id
 WHERE v.status = 'qc_ready';

CREATE INDEX idx_fluid_versions ON fluid_model_versions(fluid_model_id, status);

CREATE TRIGGER lock_fluid_version_sources_insert BEFORE INSERT ON fluid_version_sources
WHEN (SELECT status FROM fluid_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'fluid_version_sources: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_fluid_version_sources_update BEFORE UPDATE ON fluid_version_sources
WHEN (SELECT status FROM fluid_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'fluid_version_sources: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_fluid_version_sources_delete BEFORE DELETE ON fluid_version_sources
WHEN (SELECT status FROM fluid_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'fluid_version_sources: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_pvto_insert BEFORE INSERT ON pvto
WHEN (SELECT status FROM fluid_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'pvto: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_pvto_update BEFORE UPDATE ON pvto
WHEN (SELECT status FROM fluid_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'pvto: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_pvto_delete BEFORE DELETE ON pvto
WHEN (SELECT status FROM fluid_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'pvto: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_pvdo_insert BEFORE INSERT ON pvdo
WHEN (SELECT status FROM fluid_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'pvdo: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_pvdo_update BEFORE UPDATE ON pvdo
WHEN (SELECT status FROM fluid_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'pvdo: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_pvdo_delete BEFORE DELETE ON pvdo
WHEN (SELECT status FROM fluid_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'pvdo: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_pvdg_insert BEFORE INSERT ON pvdg
WHEN (SELECT status FROM fluid_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'pvdg: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_pvdg_update BEFORE UPDATE ON pvdg
WHEN (SELECT status FROM fluid_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'pvdg: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_pvdg_delete BEFORE DELETE ON pvdg
WHEN (SELECT status FROM fluid_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'pvdg: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_pvtg_insert BEFORE INSERT ON pvtg
WHEN (SELECT status FROM fluid_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'pvtg: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_pvtg_update BEFORE UPDATE ON pvtg
WHEN (SELECT status FROM fluid_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'pvtg: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_pvtg_delete BEFORE DELETE ON pvtg
WHEN (SELECT status FROM fluid_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'pvtg: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_pvtw_insert BEFORE INSERT ON pvtw
WHEN (SELECT status FROM fluid_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'pvtw: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_pvtw_update BEFORE UPDATE ON pvtw
WHEN (SELECT status FROM fluid_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'pvtw: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_pvtw_delete BEFORE DELETE ON pvtw
WHEN (SELECT status FROM fluid_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'pvtw: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_density_insert BEFORE INSERT ON density
WHEN (SELECT status FROM fluid_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'density: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_density_update BEFORE UPDATE ON density
WHEN (SELECT status FROM fluid_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'density: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_density_delete BEFORE DELETE ON density
WHEN (SELECT status FROM fluid_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'density: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_rsvd_insert BEFORE INSERT ON rsvd
WHEN (SELECT status FROM fluid_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'rsvd: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_rsvd_update BEFORE UPDATE ON rsvd
WHEN (SELECT status FROM fluid_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'rsvd: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_rsvd_delete BEFORE DELETE ON rsvd
WHEN (SELECT status FROM fluid_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'rsvd: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_pbvd_insert BEFORE INSERT ON pbvd
WHEN (SELECT status FROM fluid_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'pbvd: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_pbvd_update BEFORE UPDATE ON pbvd
WHEN (SELECT status FROM fluid_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'pbvd: fluid version is not draft; data is frozen'); END;

CREATE TRIGGER lock_pbvd_delete BEFORE DELETE ON pbvd
WHEN (SELECT status FROM fluid_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'pbvd: fluid version is not draft; data is frozen'); END;


-- ======================= GEO lifecycle =======================
CREATE TRIGGER geo_insert_as_draft BEFORE INSERT ON geo_model_versions
WHEN NEW.status <> 'draft'
BEGIN SELECT RAISE(ABORT, 'new geo versions must start as draft'); END;

CREATE TRIGGER geo_version_autonumber AFTER INSERT ON geo_model_versions
WHEN NEW.version = 0
BEGIN
    UPDATE geo_model_versions SET version = (SELECT COALESCE(MAX(version),0) + 1 FROM geo_model_versions
                                WHERE geo_model_id = NEW.geo_model_id AND id <> NEW.id)
     WHERE id = NEW.id;
END;

CREATE TRIGGER geo_status_rules BEFORE UPDATE ON geo_model_versions
BEGIN
    SELECT RAISE(ABORT, 'geo version is approved/rejected and immutable; create a new version')
     WHERE OLD.status IN ('approved','rejected');
    SELECT RAISE(ABORT, 'geo version is qc_ready (frozen for review); move back to draft to edit')
     WHERE OLD.status = 'qc_ready' AND NEW.status = 'qc_ready'
       AND NOT (OLD.qc_ready_at IS NULL AND NEW.qc_ready_at IS NOT NULL);  -- allow the timestamp stamp only
    SELECT RAISE(ABORT, 'invalid status transition for geo version')
     WHERE NOT ((OLD.status = 'draft'    AND NEW.status IN ('draft','qc_ready')) OR
                (OLD.status = 'qc_ready' AND NEW.status IN ('draft','qc_ready','approved','rejected')));
    SELECT RAISE(ABORT, 'qc_ready requires qc_json and a QC report file')
     WHERE NEW.status = 'qc_ready' AND (NEW.qc_json IS NULL OR NEW.qc_report_file_id IS NULL);
    SELECT RAISE(ABORT, 'approve/reject only through the approvals table (human review)')
     WHERE NEW.status IN ('approved','rejected')
       AND NOT EXISTS (SELECT 1 FROM approvals
                        WHERE model_kind = 'geo' AND version_id = NEW.id
                          AND decision = NEW.status);
END;

CREATE TRIGGER geo_stamp_qc_ready AFTER UPDATE OF status ON geo_model_versions
WHEN NEW.status = 'qc_ready'
BEGIN UPDATE geo_model_versions SET qc_ready_at = datetime('now') WHERE id = NEW.id AND qc_ready_at IS NULL; END;

CREATE TRIGGER geo_reset_qc_on_draft AFTER UPDATE OF status ON geo_model_versions
WHEN NEW.status = 'draft' AND OLD.status = 'qc_ready'
BEGIN UPDATE geo_model_versions SET qc_ready_at = NULL WHERE id = NEW.id; END;

CREATE TRIGGER geo_no_delete_after_draft BEFORE DELETE ON geo_model_versions
WHEN OLD.status <> 'draft'
BEGIN SELECT RAISE(ABORT, 'only draft geo versions can be deleted'); END;

CREATE VIEW latest_approved_geo AS
SELECT m.name AS model_name, v.*
  FROM geo_model_versions v JOIN geo_models m ON m.id = v.geo_model_id
 WHERE v.status = 'approved'
   AND v.version = (SELECT MAX(version) FROM geo_model_versions
                     WHERE geo_model_id = v.geo_model_id AND status = 'approved');

CREATE VIEW pending_review_geo AS
SELECT m.name AS model_name, v.id, v.version, v.qc_ready_at, v.qc_report_file_id, v.qc_json
  FROM geo_model_versions v JOIN geo_models m ON m.id = v.geo_model_id
 WHERE v.status = 'qc_ready';

CREATE INDEX idx_geo_versions ON geo_model_versions(geo_model_id, status);

CREATE TRIGGER lock_geo_version_sources_insert BEFORE INSERT ON geo_version_sources
WHEN (SELECT status FROM geo_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'geo_version_sources: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_geo_version_sources_update BEFORE UPDATE ON geo_version_sources
WHEN (SELECT status FROM geo_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'geo_version_sources: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_geo_version_sources_delete BEFORE DELETE ON geo_version_sources
WHEN (SELECT status FROM geo_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'geo_version_sources: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_geo_properties_insert BEFORE INSERT ON geo_properties
WHEN (SELECT status FROM geo_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'geo_properties: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_geo_properties_update BEFORE UPDATE ON geo_properties
WHEN (SELECT status FROM geo_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'geo_properties: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_geo_properties_delete BEFORE DELETE ON geo_properties
WHEN (SELECT status FROM geo_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'geo_properties: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_faults_insert BEFORE INSERT ON faults
WHEN (SELECT status FROM geo_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'faults: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_faults_update BEFORE UPDATE ON faults
WHEN (SELECT status FROM geo_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'faults: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_faults_delete BEFORE DELETE ON faults
WHEN (SELECT status FROM geo_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'faults: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_fault_multipliers_insert BEFORE INSERT ON fault_multipliers
WHEN (SELECT status FROM geo_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'fault_multipliers: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_fault_multipliers_update BEFORE UPDATE ON fault_multipliers
WHEN (SELECT status FROM geo_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'fault_multipliers: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_fault_multipliers_delete BEFORE DELETE ON fault_multipliers
WHEN (SELECT status FROM geo_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'fault_multipliers: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_rock_insert BEFORE INSERT ON rock
WHEN (SELECT status FROM geo_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'rock: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_rock_update BEFORE UPDATE ON rock
WHEN (SELECT status FROM geo_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'rock: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_rock_delete BEFORE DELETE ON rock
WHEN (SELECT status FROM geo_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'rock: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_swof_insert BEFORE INSERT ON swof
WHEN (SELECT status FROM geo_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'swof: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_swof_update BEFORE UPDATE ON swof
WHEN (SELECT status FROM geo_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'swof: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_swof_delete BEFORE DELETE ON swof
WHEN (SELECT status FROM geo_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'swof: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_sgof_insert BEFORE INSERT ON sgof
WHEN (SELECT status FROM geo_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'sgof: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_sgof_update BEFORE UPDATE ON sgof
WHEN (SELECT status FROM geo_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'sgof: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_sgof_delete BEFORE DELETE ON sgof
WHEN (SELECT status FROM geo_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'sgof: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_contacts_insert BEFORE INSERT ON contacts
WHEN (SELECT status FROM geo_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'contacts: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_contacts_update BEFORE UPDATE ON contacts
WHEN (SELECT status FROM geo_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'contacts: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_contacts_delete BEFORE DELETE ON contacts
WHEN (SELECT status FROM geo_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'contacts: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_aquifers_insert BEFORE INSERT ON aquifers
WHEN (SELECT status FROM geo_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'aquifers: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_aquifers_update BEFORE UPDATE ON aquifers
WHEN (SELECT status FROM geo_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'aquifers: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_aquifers_delete BEFORE DELETE ON aquifers
WHEN (SELECT status FROM geo_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'aquifers: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_aquifer_connections_insert BEFORE INSERT ON aquifer_connections
WHEN (SELECT status FROM geo_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'aquifer_connections: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_aquifer_connections_update BEFORE UPDATE ON aquifer_connections
WHEN (SELECT status FROM geo_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'aquifer_connections: geo version is not draft; data is frozen'); END;

CREATE TRIGGER lock_aquifer_connections_delete BEFORE DELETE ON aquifer_connections
WHEN (SELECT status FROM geo_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'aquifer_connections: geo version is not draft; data is frozen'); END;


-- ======================= WELL lifecycle =======================
CREATE TRIGGER well_insert_as_draft BEFORE INSERT ON well_model_versions
WHEN NEW.status <> 'draft'
BEGIN SELECT RAISE(ABORT, 'new well versions must start as draft'); END;

CREATE TRIGGER well_version_autonumber AFTER INSERT ON well_model_versions
WHEN NEW.version = 0
BEGIN
    UPDATE well_model_versions SET version = (SELECT COALESCE(MAX(version),0) + 1 FROM well_model_versions
                                WHERE well_model_id = NEW.well_model_id AND id <> NEW.id)
     WHERE id = NEW.id;
END;

CREATE TRIGGER well_status_rules BEFORE UPDATE ON well_model_versions
BEGIN
    SELECT RAISE(ABORT, 'well version is approved/rejected and immutable; create a new version')
     WHERE OLD.status IN ('approved','rejected');
    SELECT RAISE(ABORT, 'well version is qc_ready (frozen for review); move back to draft to edit')
     WHERE OLD.status = 'qc_ready' AND NEW.status = 'qc_ready'
       AND NOT (OLD.qc_ready_at IS NULL AND NEW.qc_ready_at IS NOT NULL);  -- allow the timestamp stamp only
    SELECT RAISE(ABORT, 'invalid status transition for well version')
     WHERE NOT ((OLD.status = 'draft'    AND NEW.status IN ('draft','qc_ready')) OR
                (OLD.status = 'qc_ready' AND NEW.status IN ('draft','qc_ready','approved','rejected')));
    SELECT RAISE(ABORT, 'qc_ready requires qc_json and a QC report file')
     WHERE NEW.status = 'qc_ready' AND (NEW.qc_json IS NULL OR NEW.qc_report_file_id IS NULL);
    SELECT RAISE(ABORT, 'approve/reject only through the approvals table (human review)')
     WHERE NEW.status IN ('approved','rejected')
       AND NOT EXISTS (SELECT 1 FROM approvals
                        WHERE model_kind = 'well' AND version_id = NEW.id
                          AND decision = NEW.status);
END;

CREATE TRIGGER well_stamp_qc_ready AFTER UPDATE OF status ON well_model_versions
WHEN NEW.status = 'qc_ready'
BEGIN UPDATE well_model_versions SET qc_ready_at = datetime('now') WHERE id = NEW.id AND qc_ready_at IS NULL; END;

CREATE TRIGGER well_reset_qc_on_draft AFTER UPDATE OF status ON well_model_versions
WHEN NEW.status = 'draft' AND OLD.status = 'qc_ready'
BEGIN UPDATE well_model_versions SET qc_ready_at = NULL WHERE id = NEW.id; END;

CREATE TRIGGER well_no_delete_after_draft BEFORE DELETE ON well_model_versions
WHEN OLD.status <> 'draft'
BEGIN SELECT RAISE(ABORT, 'only draft well versions can be deleted'); END;

CREATE VIEW latest_approved_well AS
SELECT m.name AS model_name, v.*
  FROM well_model_versions v JOIN well_models m ON m.id = v.well_model_id
 WHERE v.status = 'approved'
   AND v.version = (SELECT MAX(version) FROM well_model_versions
                     WHERE well_model_id = v.well_model_id AND status = 'approved');

CREATE VIEW pending_review_well AS
SELECT m.name AS model_name, v.id, v.version, v.qc_ready_at, v.qc_report_file_id, v.qc_json
  FROM well_model_versions v JOIN well_models m ON m.id = v.well_model_id
 WHERE v.status = 'qc_ready';

CREATE INDEX idx_well_versions ON well_model_versions(well_model_id, status);

CREATE TRIGGER lock_well_version_sources_insert BEFORE INSERT ON well_version_sources
WHEN (SELECT status FROM well_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'well_version_sources: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_well_version_sources_update BEFORE UPDATE ON well_version_sources
WHEN (SELECT status FROM well_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'well_version_sources: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_well_version_sources_delete BEFORE DELETE ON well_version_sources
WHEN (SELECT status FROM well_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'well_version_sources: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_well_groups_insert BEFORE INSERT ON well_groups
WHEN (SELECT status FROM well_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'well_groups: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_well_groups_update BEFORE UPDATE ON well_groups
WHEN (SELECT status FROM well_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'well_groups: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_well_groups_delete BEFORE DELETE ON well_groups
WHEN (SELECT status FROM well_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'well_groups: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_wells_insert BEFORE INSERT ON wells
WHEN (SELECT status FROM well_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'wells: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_wells_update BEFORE UPDATE ON wells
WHEN (SELECT status FROM well_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'wells: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_wells_delete BEFORE DELETE ON wells
WHEN (SELECT status FROM well_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'wells: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_well_trajectory_insert BEFORE INSERT ON well_trajectory
WHEN (SELECT status FROM well_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'well_trajectory: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_well_trajectory_update BEFORE UPDATE ON well_trajectory
WHEN (SELECT status FROM well_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'well_trajectory: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_well_trajectory_delete BEFORE DELETE ON well_trajectory
WHEN (SELECT status FROM well_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'well_trajectory: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_well_completions_insert BEFORE INSERT ON well_completions
WHEN (SELECT status FROM well_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'well_completions: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_well_completions_update BEFORE UPDATE ON well_completions
WHEN (SELECT status FROM well_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'well_completions: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_well_completions_delete BEFORE DELETE ON well_completions
WHEN (SELECT status FROM well_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'well_completions: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_well_events_insert BEFORE INSERT ON well_events
WHEN (SELECT status FROM well_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'well_events: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_well_events_update BEFORE UPDATE ON well_events
WHEN (SELECT status FROM well_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'well_events: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_well_events_delete BEFORE DELETE ON well_events
WHEN (SELECT status FROM well_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'well_events: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_well_econ_limits_insert BEFORE INSERT ON well_econ_limits
WHEN (SELECT status FROM well_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'well_econ_limits: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_well_econ_limits_update BEFORE UPDATE ON well_econ_limits
WHEN (SELECT status FROM well_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'well_econ_limits: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_well_econ_limits_delete BEFORE DELETE ON well_econ_limits
WHEN (SELECT status FROM well_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'well_econ_limits: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_group_controls_insert BEFORE INSERT ON group_controls
WHEN (SELECT status FROM well_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'group_controls: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_group_controls_update BEFORE UPDATE ON group_controls
WHEN (SELECT status FROM well_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'group_controls: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_group_controls_delete BEFORE DELETE ON group_controls
WHEN (SELECT status FROM well_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'group_controls: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_vfp_tables_insert BEFORE INSERT ON vfp_tables
WHEN (SELECT status FROM well_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'vfp_tables: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_vfp_tables_update BEFORE UPDATE ON vfp_tables
WHEN (SELECT status FROM well_model_versions WHERE id = NEW.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'vfp_tables: well version is not draft; data is frozen'); END;

CREATE TRIGGER lock_vfp_tables_delete BEFORE DELETE ON vfp_tables
WHEN (SELECT status FROM well_model_versions WHERE id = OLD.version_id) IN ('qc_ready','approved','rejected')
BEGIN SELECT RAISE(ABORT, 'vfp_tables: well version is not draft; data is frozen'); END;


CREATE INDEX idx_wcomp  ON well_completions(version_id, well_name, event_date);
CREATE INDEX idx_wevent ON well_events(version_id, well_name, event_date);
CREATE INDEX idx_gctrl  ON group_controls(version_id, group_name, event_date);
CREATE INDEX idx_approvals ON approvals(model_kind, version_id);
CREATE INDEX idx_decks_models ON decks(fluid_version_id, geo_version_id, well_version_id);
