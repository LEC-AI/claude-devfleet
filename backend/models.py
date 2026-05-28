from pydantic import BaseModel
from typing import Optional, List


class ProjectCreate(BaseModel):
    name: str
    path: str
    description: str = ""
    system_prompt: str = ""
    # Lifecycle + ownership fields
    state: str = "active"           # active | on_hold | completed | archived
    owner: str = ""                 # email or display name
    start_date: str = ""            # ISO YYYY-MM-DD
    target_end_date: str = ""       # ISO YYYY-MM-DD
    parent_team: str = ""           # owning team name, e.g. "Platform", "Robotics"
    teams_channel_id: str = ""      # set by project_create when channel is auto-created
    teams_channel_name: str = ""


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    path: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    state: Optional[str] = None
    owner: Optional[str] = None
    start_date: Optional[str] = None
    target_end_date: Optional[str] = None
    parent_team: Optional[str] = None
    teams_channel_id: Optional[str] = None
    teams_channel_name: Optional[str] = None


# ── Dispatch Configuration ──
# Controls how the Claude CLI agent is spawned per mission

TOOL_PRESETS = {
    "full": ["Read", "Write", "Edit", "Bash", "Grep", "Glob", "WebFetch", "WebSearch"],
    "implement": ["Read", "Write", "Edit", "Bash", "Grep", "Glob"],
    "review": ["Read", "Grep", "Glob", "Bash(git diff *)", "Bash(git log *)"],
    "test": ["Read", "Edit", "Bash(npm test *)", "Bash(pytest *)", "Bash(cargo test *)", "Grep", "Glob"],
    "explore": ["Read", "Grep", "Glob", "Bash(git *)", "Bash(ls *)", "Bash(find *)"],
    "fix": ["Read", "Write", "Edit", "Bash", "Grep", "Glob"],
}

MODEL_CHOICES = ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]


class DispatchOptions(BaseModel):
    """Per-dispatch overrides for Claude CLI invocation."""
    model: Optional[str] = None              # claude-opus-4-6, claude-sonnet-4-6, claude-haiku-4-5-20251001
    max_turns: Optional[int] = None          # --max-turns N
    max_budget_usd: Optional[float] = None   # --max-budget-usd N
    allowed_tools: Optional[List[str]] = None # --allowedTools list (or preset name)
    tool_preset: Optional[str] = None        # key into TOOL_PRESETS
    append_system_prompt: Optional[str] = None  # --append-system-prompt
    fork_session: bool = False               # --fork-session (for branching from resume)
    context_mode: bool = False               # attach context-mode MCP server for context savings + session continuity
    max_retries: int = 3                     # retry on transient errors (500, 502, 503, 429, timeout)
    retry_initial_delay: float = 2.0         # seconds before first retry (doubles each attempt)
    retry_max_delay: float = 60.0            # cap on backoff delay


class MissionCreate(BaseModel):
    project_id: str
    title: str
    detailed_prompt: str
    acceptance_criteria: str = ""
    priority: int = 0
    tags: List[str] = []
    # Default dispatch config stored on mission
    model: str = "claude-opus-4-6"
    max_turns: Optional[int] = None
    max_budget_usd: Optional[float] = None
    allowed_tools: Optional[str] = None      # JSON string or preset name
    mission_type: str = "implement"          # implement, review, test, explore, fix
    # Phase 3: multi-agent, dependencies, scheduling
    parent_mission_id: Optional[str] = None  # parent mission for sub-missions
    depends_on: List[str] = []               # mission IDs that must complete first
    auto_dispatch: bool = False              # auto-dispatch when dependencies met
    schedule_cron: Optional[str] = None      # cron expression for recurring missions
    callback_url: Optional[str] = None       # POST here on mission complete/failed
    max_retries: int = 3                     # auto-retry on transient API errors
    auto_retry: bool = True                  # enable/disable auto-retry


class MissionUpdate(BaseModel):
    title: Optional[str] = None
    detailed_prompt: Optional[str] = None
    acceptance_criteria: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[int] = None
    tags: Optional[List[str]] = None
    model: Optional[str] = None
    max_turns: Optional[int] = None
    max_budget_usd: Optional[float] = None
    allowed_tools: Optional[str] = None
    mission_type: Optional[str] = None
    parent_mission_id: Optional[str] = None
    depends_on: Optional[List[str]] = None
    auto_dispatch: Optional[bool] = None
    schedule_cron: Optional[str] = None
    schedule_enabled: Optional[bool] = None
    callback_url: Optional[str] = None


class ServiceCreate(BaseModel):
    project_id: str
    name: str
    url: str
    group_name: str = "Default"
    description: str = ""
    check_interval: int = 30
    timeout_ms: int = 5000
    expected_status: int = 200


class ServiceUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    group_name: Optional[str] = None
    description: Optional[str] = None
    check_interval: Optional[int] = None
    timeout_ms: Optional[int] = None
    expected_status: Optional[int] = None
    enabled: Optional[bool] = None


class IncidentCreate(BaseModel):
    service_id: Optional[str] = None
    project_id: str
    title: str
    description: str = ""
    severity: str = "minor"


class IncidentUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    severity: Optional[str] = None
    resolved_at: Optional[str] = None


# ── MCP Server Configuration ──

class McpServerCreate(BaseModel):
    """Configure an MCP server for a project — agents get access to its tools."""
    server_name: str                          # e.g. "github", "brave-search", "memory"
    server_type: str = "stdio"                # stdio, sse, http
    config: dict = {}                         # command, args, env, url, headers etc.
