import aiosqlite
import os

DB_PATH = os.environ.get("DEVFLEET_DB", os.path.join(os.path.dirname(__file__), "..", "data", "devfleet.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    description TEXT DEFAULT '',
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
    model TEXT DEFAULT 'claude-sonnet-4-6',
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
    mission_number INTEGER
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
    model TEXT DEFAULT 'claude-sonnet-4-6',
    token_usage TEXT DEFAULT '{}',
    claude_session_id TEXT DEFAULT '',
    remote_url TEXT DEFAULT '',
    total_cost_usd REAL DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    last_activity_at TEXT,
    branch_name TEXT DEFAULT ''
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
    failure_layer TEXT,
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

CREATE TABLE IF NOT EXISTS lanes (
    name TEXT PRIMARY KEY,
    max_agents INTEGER NOT NULL DEFAULT 1,
    default_model TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
    tool_preset TEXT NOT NULL DEFAULT 'implement',
    append_prompt TEXT DEFAULT '',
    color TEXT DEFAULT '#888888',
    icon TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lane_mcp_tools (
    id TEXT PRIMARY KEY,
    lane_name TEXT NOT NULL REFERENCES lanes(name) ON DELETE CASCADE,
    server_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    trigger_hint TEXT DEFAULT 'always',
    UNIQUE(lane_name, server_name, tool_name)
);

CREATE TABLE IF NOT EXISTS lane_prompt_critiques (
    lane_name TEXT PRIMARY KEY REFERENCES lanes(name) ON DELETE CASCADE,
    critique_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS prompt_templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT DEFAULT 'general',
    content_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT DEFAULT 'user',
    created_at TEXT DEFAULT (datetime('now')),
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS invite_tokens (
    token TEXT PRIMARY KEY,
    created_by TEXT NOT NULL REFERENCES users(id),
    used_by TEXT REFERENCES users(id),
    used_at TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
"""


async def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA cache_size=-64000")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(SCHEMA)
        # Migrations for existing DBs
        migrations = [
            "ALTER TABLE agent_sessions ADD COLUMN claude_session_id TEXT DEFAULT ''",
            "ALTER TABLE reports ADD COLUMN preview_url TEXT DEFAULT ''",
            # v2: Claude Code power features
            "ALTER TABLE missions ADD COLUMN model TEXT DEFAULT 'claude-sonnet-4-6'",
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
            # v4: agentic lanes
            "ALTER TABLE missions ADD COLUMN lane TEXT DEFAULT 'coder'",
            # v5: failure layer classification (dispatch vs agent)
            "ALTER TABLE mission_events ADD COLUMN failure_layer TEXT",
            # v6: activity heartbeat + accurate cost tracking
            "ALTER TABLE agent_sessions ADD COLUMN last_activity_at TEXT",
            "ALTER TABLE agent_sessions ADD COLUMN cache_read_tokens INTEGER DEFAULT 0",
            "ALTER TABLE agent_sessions ADD COLUMN cache_creation_tokens INTEGER DEFAULT 0",
            # v7: lane-aware branch naming
            "ALTER TABLE agent_sessions ADD COLUMN branch_name TEXT DEFAULT ''",
            # v8: online auth
            "ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'",
        ]
        for migration in migrations:
            try:
                await db.execute(migration)
            except Exception:
                pass  # Column already exists

        # Full re-sync of all lane fields from LANE_DEFAULTS
        # INSERT OR IGNORE seeds new lanes; UPDATE syncs capacity/model/preset/style — but NOT
        # append_prompt, which the user can customise via Prompt Studio and must survive restarts.
        # Also disables lanes no longer in LANE_DEFAULTS (deprecated/renamed lanes)
        from models import LANE_DEFAULTS as _LD
        await db.execute(
            f"UPDATE lanes SET enabled=0 WHERE name NOT IN ({','.join('?' for _ in _LD)})",
            list(_LD.keys()),
        )
        for _lane_name, _policy in _LD.items():
            await db.execute(
                """INSERT INTO lanes (name, max_agents, default_model, tool_preset, append_prompt, color, icon)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     max_agents    = excluded.max_agents,
                     default_model = excluded.default_model,
                     tool_preset   = excluded.tool_preset,
                     color         = excluded.color,
                     icon          = excluded.icon""",
                (
                    _lane_name,
                    _policy["max_agents"],
                    _policy["default_model"],
                    _policy["tool_preset"],
                    _policy["append_prompt"],
                    _policy.get("color", "#888888"),
                    _policy.get("icon", ""),
                ),
            )

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

        # Seed default lanes (imported here to avoid top-level circular import)
        from models import LANE_DEFAULTS
        for name, policy in LANE_DEFAULTS.items():
            await db.execute(
                """INSERT OR IGNORE INTO lanes
                   (name, max_agents, default_model, tool_preset, append_prompt, color, icon)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    name,
                    policy["max_agents"],
                    policy["default_model"],
                    policy["tool_preset"],
                    policy["append_prompt"],
                    policy["color"],
                    policy["icon"],
                ),
            )

        # Seed default MCP tools per lane (INSERT OR IGNORE — preserves user toggles)
        _DEVFLEET_TOOLS = [
            ("devfleet-context", "get_mission_context", "always"),
            ("devfleet-context", "get_project_context", "always"),
            ("devfleet-context", "get_session_history", "always"),
            ("devfleet-context", "get_team_context", "always"),
            ("devfleet-context", "read_past_reports", "always"),
            ("devfleet-tools", "submit_report", "always"),
            ("devfleet-tools", "create_sub_mission", "only when spawning parallel sub-tasks"),
            ("devfleet-tools", "request_review", "after completing implementation"),
            ("devfleet-tools", "get_sub_mission_status", "when waiting on sub-missions"),
            ("devfleet-tools", "list_project_missions", "when checking project context"),
        ]
        for _lane_name in _LD:
            for _server, _tool, _hint in _DEVFLEET_TOOLS:
                _tool_id = f"{_lane_name}__{_server}__{_tool}"
                await db.execute(
                    """INSERT OR IGNORE INTO lane_mcp_tools (id, lane_name, server_name, tool_name, trigger_hint)
                       VALUES (?, ?, ?, ?, ?)""",
                    (_tool_id, _lane_name, _server, _tool, _hint),
                )

        # Backfill missions.lane from mission_type for existing rows
        await db.execute("""
            UPDATE missions SET lane = CASE mission_type
                WHEN 'implement' THEN 'coder'
                WHEN 'fix'       THEN 'coder'
                WHEN 'full'      THEN 'coder'
                WHEN 'review'    THEN 'reviewer'
                WHEN 'test'      THEN 'tester'
                WHEN 'explore'   THEN 'explorer'
                WHEN 'planner'   THEN 'planner'
                ELSE 'coder'
            END
            WHERE lane IS NULL OR lane = ''
        """)

        # Seed first admin from env if no users exist
        _admin_email = os.environ.get("DEVFLEET_ADMIN_EMAIL")
        _admin_pw = os.environ.get("DEVFLEET_ADMIN_PASSWORD")
        if _admin_email and _admin_pw:
            _existing = await db.execute_fetchall(
                "SELECT id FROM users WHERE email=?", (_admin_email,)
            )
            if not _existing:
                from auth import hash_password as _hp
                import uuid as _uuid
                _aid = str(_uuid.uuid4())
                await db.execute(
                    "INSERT INTO users (id, email, password_hash, role) VALUES (?,?,?,'admin')",
                    (_aid, _admin_email, _hp(_admin_pw))
                )
                import logging as _logging
                _logging.getLogger("devfleet").info("Seeded initial admin: %s", _admin_email)

        await db.commit()


async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA busy_timeout=5000")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA cache_size=-64000")
    return db
