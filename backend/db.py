import aiosqlite
import os

DB_PATH = os.environ.get("DEVFLEET_DB", os.path.join(os.path.dirname(__file__), "..", "data", "devfleet.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    description TEXT DEFAULT '',
    system_prompt TEXT DEFAULT '',
    state TEXT DEFAULT 'active',
    owner TEXT DEFAULT '',
    start_date TEXT DEFAULT '',
    target_end_date TEXT DEFAULT '',
    parent_team TEXT DEFAULT '',
    teams_channel_id TEXT DEFAULT '',
    teams_channel_name TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS missions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    detailed_prompt TEXT NOT NULL,
    acceptance_criteria TEXT DEFAULT '',
    status TEXT DEFAULT 'draft',
    priority INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    tags TEXT DEFAULT '[]',
    model TEXT DEFAULT 'claude-opus-4-6',
    max_turns INTEGER,
    max_budget_usd REAL,
    allowed_tools TEXT DEFAULT '',
    mission_type TEXT DEFAULT 'implement',
    parent_mission_id TEXT,
    depends_on TEXT DEFAULT '[]',
    auto_dispatch INTEGER DEFAULT 0,
    schedule_cron TEXT,
    schedule_enabled INTEGER DEFAULT 0,
    last_scheduled_at TEXT,
    mission_number INTEGER,
    callback_url TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS agent_sessions (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    status TEXT DEFAULT 'running',
    started_at TEXT DEFAULT (datetime('now')),
    ended_at TEXT,
    exit_code INTEGER,
    output_log TEXT DEFAULT '',
    error_log TEXT DEFAULT '',
    model TEXT DEFAULT 'claude-opus-4-6',
    token_usage TEXT DEFAULT '{}',
    claude_session_id TEXT DEFAULT '',
    remote_url TEXT DEFAULT '',
    total_cost_usd REAL DEFAULT 0,
    total_tokens INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
    mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    files_changed TEXT DEFAULT '',
    what_done TEXT DEFAULT '',
    what_open TEXT DEFAULT '',
    what_tested TEXT DEFAULT '',
    what_untested TEXT DEFAULT '',
    next_steps TEXT DEFAULT '',
    errors_encountered TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS monitored_services (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    group_name TEXT DEFAULT 'Default',
    description TEXT DEFAULT '',
    check_interval INTEGER DEFAULT 30,
    timeout_ms INTEGER DEFAULT 5000,
    expected_status INTEGER DEFAULT 200,
    enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS health_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id TEXT NOT NULL REFERENCES monitored_services(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    response_time_ms INTEGER,
    status_code INTEGER,
    error_message TEXT DEFAULT '',
    checked_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_health_checks_service_time
    ON health_checks(service_id, checked_at DESC);

CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    service_id TEXT REFERENCES monitored_services(id) ON DELETE SET NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    status TEXT DEFAULT 'investigating',
    severity TEXT DEFAULT 'minor',
    started_at TEXT DEFAULT (datetime('now')),
    resolved_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS conversations (
    session_id TEXT PRIMARY KEY REFERENCES agent_sessions(id) ON DELETE CASCADE,
    messages_json TEXT DEFAULT '[]',
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS mission_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    source_mission_id TEXT,
    data TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_mission_events_mission
    ON mission_events(mission_id, created_at DESC);

CREATE TABLE IF NOT EXISTS mcp_configs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    server_name TEXT NOT NULL,
    server_type TEXT DEFAULT 'stdio',
    config_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_mcp_configs_project
    ON mcp_configs(project_id);
"""


async def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(SCHEMA)
        # Migrations for existing DBs
        migrations = [
            "ALTER TABLE agent_sessions ADD COLUMN claude_session_id TEXT DEFAULT ''",
            "ALTER TABLE reports ADD COLUMN preview_url TEXT DEFAULT ''",
            # v2: Claude Code power features
            "ALTER TABLE missions ADD COLUMN model TEXT DEFAULT 'claude-opus-4-6'",
            "ALTER TABLE missions ADD COLUMN max_turns INTEGER",
            "ALTER TABLE missions ADD COLUMN max_budget_usd REAL",
            "ALTER TABLE missions ADD COLUMN allowed_tools TEXT DEFAULT ''",
            "ALTER TABLE missions ADD COLUMN mission_type TEXT DEFAULT 'implement'",
            "ALTER TABLE agent_sessions ADD COLUMN remote_url TEXT DEFAULT ''",
            "ALTER TABLE agent_sessions ADD COLUMN total_cost_usd REAL DEFAULT 0",
            "ALTER TABLE agent_sessions ADD COLUMN total_tokens INTEGER DEFAULT 0",
            # v3: Phase 3 — multi-agent, dependencies, scheduling
            "ALTER TABLE missions ADD COLUMN parent_mission_id TEXT",
            "ALTER TABLE missions ADD COLUMN depends_on TEXT DEFAULT '[]'",
            "ALTER TABLE missions ADD COLUMN auto_dispatch INTEGER DEFAULT 0",
            "ALTER TABLE missions ADD COLUMN schedule_cron TEXT",
            "ALTER TABLE missions ADD COLUMN schedule_enabled INTEGER DEFAULT 0",
            "ALTER TABLE missions ADD COLUMN last_scheduled_at TEXT",
            "ALTER TABLE missions ADD COLUMN mission_number INTEGER",
            # v4: Webhook callbacks for external integrations
            "ALTER TABLE missions ADD COLUMN callback_url TEXT DEFAULT ''",
            # v5: Project-level system prompt — injected into every mission dispatch
            "ALTER TABLE projects ADD COLUMN system_prompt TEXT DEFAULT ''",
            # v6: Retry robustness — auto-retry on transient errors
            "ALTER TABLE missions ADD COLUMN max_retries INTEGER DEFAULT 3",
            "ALTER TABLE missions ADD COLUMN auto_retry INTEGER DEFAULT 1",
            "ALTER TABLE agent_sessions ADD COLUMN retry_count INTEGER DEFAULT 0",
            "ALTER TABLE agent_sessions ADD COLUMN last_error TEXT DEFAULT ''",
            "ALTER TABLE agent_sessions ADD COLUMN error_type TEXT DEFAULT ''",
            # v7: Project lifecycle + Microsoft Teams channel wiring
            "ALTER TABLE projects ADD COLUMN state TEXT DEFAULT 'active'",
            "ALTER TABLE projects ADD COLUMN owner TEXT DEFAULT ''",
            "ALTER TABLE projects ADD COLUMN start_date TEXT DEFAULT ''",
            "ALTER TABLE projects ADD COLUMN target_end_date TEXT DEFAULT ''",
            "ALTER TABLE projects ADD COLUMN parent_team TEXT DEFAULT ''",
            "ALTER TABLE projects ADD COLUMN teams_channel_id TEXT DEFAULT ''",
            "ALTER TABLE projects ADD COLUMN teams_channel_name TEXT DEFAULT ''",
        ]
        for migration in migrations:
            try:
                await db.execute(migration)
            except Exception:
                pass  # Column already exists

        # Backfill mission_number for existing missions that don't have one
        # Use a CTE with ROW_NUMBER to assign sequential numbers per project
        await db.execute("""
            UPDATE missions SET mission_number = (
                SELECT rn FROM (
                    SELECT id, ROW_NUMBER() OVER (PARTITION BY project_id ORDER BY created_at, id) AS rn
                    FROM missions
                ) numbered WHERE numbered.id = missions.id
            ) WHERE mission_number IS NULL
        """)
        await db.commit()


async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    return db
