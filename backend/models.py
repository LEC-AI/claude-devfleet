from pydantic import BaseModel
from typing import Optional, List


class ProjectCreate(BaseModel):
    name: str
    path: str
    description: str = ""


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    path: Optional[str] = None
    description: Optional[str] = None


# ── Dispatch Configuration ──
# Controls how the Claude CLI agent is spawned per mission

TOOL_PRESETS = {
    "full": ["Read", "Write", "Edit", "Bash", "Grep", "Glob", "WebFetch", "WebSearch"],
    "implement": ["Read", "Write", "Edit", "Bash", "Grep", "Glob"],
    "review": ["Read", "Grep", "Glob", "Bash(git diff *)", "Bash(git log *)"],
    "test": ["Read", "Edit", "Bash(npm test *)", "Bash(pytest *)", "Bash(cargo test *)", "Grep", "Glob"],
    "explore": ["Read", "Grep", "Glob", "Bash(git *)", "Bash(ls *)", "Bash(find *)"],
    "fix": ["Read", "Write", "Edit", "Bash", "Grep", "Glob"],
    "planner": ["Read", "Grep", "Glob", "WebFetch"],
}

# Lane definitions — scheduling dimension separate from mission_type tool presets.
# mission_type drives which tools the agent gets; lane drives how many agents run in parallel.
LANE_DEFAULTS: dict[str, dict] = {
    "coder": {
        "max_agents": 2,
        "default_model": "claude-sonnet-4-6",
        "tool_preset": "implement",
        "append_prompt": "You are a DevFleet **Coder** agent. Your role is implementation: write clean, production-quality code.",
        "color": "#4f8ef7",
        "icon": "🛠",
    },
    "reviewer": {
        "max_agents": 1,
        "default_model": "claude-sonnet-4-6",
        "tool_preset": "review",
        "append_prompt": "You are a DevFleet **Reviewer** agent. Your role is code review: read-only analysis, find bugs, security issues, and improvement opportunities.",
        "color": "#f7a84f",
        "icon": "🔍",
    },
    "tester": {
        "max_agents": 2,
        "default_model": "claude-haiku-4-5-20251001",
        "tool_preset": "test",
        "append_prompt": "You are a DevFleet **Tester** agent. Your role is writing and running tests: unit, integration, and E2E coverage.",
        "color": "#4fc97b",
        "icon": "🧪",
    },
    "planner": {
        "max_agents": 1,
        "default_model": "claude-opus-4-6",
        "tool_preset": "planner",
        "append_prompt": "You are a DevFleet **Planner** agent. Your role is architecture and research: read widely, think deeply, propose concrete plans.",
        "color": "#b44ff7",
        "icon": "🗺",
    },
    "explorer": {
        "max_agents": 1,
        "default_model": "claude-sonnet-4-6",
        "tool_preset": "explore",
        "append_prompt": "You are a DevFleet **Explorer** agent. Your role is discovery: map the codebase, find dependencies, surface unknowns.",
        "color": "#f74f6b",
        "icon": "🔭",
    },
}

# Mapping from mission_type to default lane name
MISSION_TYPE_TO_LANE: dict[str, str] = {
    "implement": "coder",
    "fix": "coder",
    "full": "coder",
    "review": "reviewer",
    "test": "tester",
    "explore": "explorer",
    "planner": "planner",
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
    lane: Optional[str] = None               # scheduling lane override (coder/reviewer/tester/planner/explorer)


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
    lane: Optional[str] = None               # scheduling lane; derived from mission_type if absent
    # Phase 3: multi-agent, dependencies, scheduling
    parent_mission_id: Optional[str] = None  # parent mission for sub-missions
    depends_on: List[str] = []               # mission IDs that must complete first
    auto_dispatch: bool = False              # auto-dispatch when dependencies met
    schedule_cron: Optional[str] = None      # cron expression for recurring missions


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
    lane: Optional[str] = None
    parent_mission_id: Optional[str] = None
    depends_on: Optional[List[str]] = None
    auto_dispatch: Optional[bool] = None
    schedule_cron: Optional[str] = None
    schedule_enabled: Optional[bool] = None


# ── Lane Configuration ──

class LaneCreate(BaseModel):
    name: str
    max_agents: int = 1
    default_model: str = "claude-sonnet-4-6"
    tool_preset: str = "implement"
    append_prompt: str = ""
    color: str = "#888888"
    icon: str = ""


class LaneUpdate(BaseModel):
    max_agents: Optional[int] = None
    default_model: Optional[str] = None
    tool_preset: Optional[str] = None
    append_prompt: Optional[str] = None
    color: Optional[str] = None
    icon: Optional[str] = None
    enabled: Optional[bool] = None


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
