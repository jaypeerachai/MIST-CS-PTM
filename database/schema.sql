PRAGMA foreign_keys = ON;

CREATE TABLE repositories (
    repository_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    github_url TEXT NOT NULL,
    description TEXT,
    language TEXT,
    stars INTEGER,
    forks INTEGER,
    topics TEXT,
    metadata_collected_at TEXT
);

CREATE TABLE snapshots (
    snapshot_id TEXT PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories,
    commit_sha TEXT NOT NULL CHECK(length(commit_sha) = 40),
    UNIQUE(repository_id, commit_sha)
);

CREATE TABLE releases (
    release_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshots,
    tag TEXT NOT NULL,
    published_at TEXT,
    release_line INTEGER NOT NULL,
    release_order INTEGER NOT NULL,
    release_url TEXT NOT NULL
);

CREATE TABLE files (
    snapshot_id TEXT NOT NULL REFERENCES snapshots,
    path TEXT NOT NULL,
    git_blob_sha TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    git_mode TEXT NOT NULL DEFAULT '100644',
    PRIMARY KEY(snapshot_id, path)
);

CREATE TABLE analyses (
    analysis_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshots,
    analysis_type TEXT NOT NULL CHECK(analysis_type IN ('snapshot', 'release')),
    release_id TEXT REFERENCES releases,
    CHECK((analysis_type = 'snapshot' AND release_id IS NULL)
       OR (analysis_type = 'release' AND release_id IS NOT NULL))
);

CREATE TABLE occurrences (
    occurrence_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES analyses,
    snapshot_id TEXT NOT NULL,
    path TEXT NOT NULL,
    line INTEGER NOT NULL CHECK(line > 0),
    column_start INTEGER,
    column_end INTEGER,
    ptm_id TEXT NOT NULL,
    literal TEXT NOT NULL,
    confirmed_reuse INTEGER NOT NULL CHECK(confirmed_reuse IN (0, 1)),
    FOREIGN KEY(snapshot_id, path) REFERENCES files
);

CREATE TABLE bindings (
    binding_id TEXT PRIMARY KEY,
    occurrence_id TEXT NOT NULL REFERENCES occurrences,
    analysis_id TEXT NOT NULL REFERENCES analyses,
    snapshot_id TEXT NOT NULL,
    sink_path TEXT NOT NULL,
    sink_line INTEGER NOT NULL CHECK(sink_line > 0),
    sink_column INTEGER,
    sink_class TEXT,
    sink_procedure TEXT,
    interface_origin TEXT,
    call_name TEXT,
    FOREIGN KEY(snapshot_id, sink_path) REFERENCES files
);

CREATE TABLE binding_locality (
    binding_id TEXT PRIMARY KEY REFERENCES bindings,
    cross_file INTEGER NOT NULL CHECK(cross_file IN (0, 1)),
    cross_procedure INTEGER NOT NULL CHECK(cross_procedure IN (0, 1))
);

CREATE TABLE binding_steps (
    binding_id TEXT NOT NULL REFERENCES bindings,
    step INTEGER NOT NULL CHECK(step > 0),
    from_node TEXT NOT NULL,
    to_node TEXT NOT NULL,
    relation TEXT NOT NULL,
    PRIMARY KEY(binding_id, step)
);

CREATE TABLE binding_locations (
    binding_id TEXT NOT NULL REFERENCES bindings,
    position INTEGER NOT NULL,
    node_id TEXT,
    record_kind TEXT NOT NULL,
    file_path TEXT,
    start_line INTEGER,
    end_line INTEGER,
    procedure TEXT,
    scope TEXT,
    location_precision TEXT,
    location_url TEXT,
    PRIMARY KEY(binding_id, position)
);

CREATE TABLE release_pairs (
    pair_id TEXT PRIMARY KEY,
    old_release_id TEXT NOT NULL REFERENCES releases,
    new_release_id TEXT NOT NULL REFERENCES releases,
    compare_url TEXT NOT NULL,
    UNIQUE(old_release_id, new_release_id)
);

CREATE TABLE binding_matches (
    match_id TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL REFERENCES release_pairs,
    old_binding_id TEXT NOT NULL REFERENCES bindings,
    new_binding_id TEXT NOT NULL REFERENCES bindings,
    UNIQUE(pair_id, old_binding_id),
    UNIQUE(pair_id, new_binding_id)
);

CREATE TABLE ptm_changes (
    change_id TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL REFERENCES release_pairs,
    category TEXT NOT NULL CHECK(category IN ('add', 'remove', 'update', 'migration')),
    old_ptm_id TEXT,
    new_ptm_id TEXT,
    existing_method_visibility TEXT CHECK(existing_method_visibility IN
        ('fully_visible', 'partly_visible', 'not_visible'))
);

CREATE TABLE change_bindings (
    change_id TEXT NOT NULL REFERENCES ptm_changes,
    side TEXT NOT NULL CHECK(side IN ('old', 'new')),
    binding_id TEXT NOT NULL REFERENCES bindings,
    PRIMARY KEY(change_id, side, binding_id)
);

CREATE TABLE unmatched_bindings (
    pair_id TEXT NOT NULL REFERENCES release_pairs,
    side TEXT NOT NULL CHECK(side IN ('old', 'new')),
    binding_id TEXT NOT NULL REFERENCES bindings,
    PRIMARY KEY(pair_id, side, binding_id)
);

CREATE TABLE artifacts (
    path TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES analyses,
    kind TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0)
);

CREATE INDEX bindings_analysis ON bindings(analysis_id);
CREATE INDEX bindings_occurrence ON bindings(occurrence_id);
CREATE INDEX occurrences_ptm ON occurrences(ptm_id);
CREATE INDEX files_blob ON files(git_blob_sha);
CREATE INDEX ptm_changes_pair ON ptm_changes(pair_id);

CREATE VIEW binding_records AS
SELECT b.binding_id, r.name AS repository, s.commit_sha,
       o.ptm_id, o.path AS source_path, o.line AS source_line,
       b.sink_path, b.sink_line, b.interface_origin, b.call_name
FROM bindings b
JOIN occurrences o ON o.occurrence_id = b.occurrence_id
JOIN snapshots s ON s.snapshot_id = b.snapshot_id
JOIN repositories r ON r.repository_id = s.repository_id;

CREATE VIEW confirmed_repositories AS
SELECT DISTINCT r.* FROM repositories r
JOIN snapshots s ON s.repository_id = r.repository_id
JOIN bindings b ON b.snapshot_id = s.snapshot_id;
