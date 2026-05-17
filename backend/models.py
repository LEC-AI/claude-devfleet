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
    "full":         ["Read", "Write", "Edit", "Bash", "Grep", "Glob", "WebFetch", "WebSearch"],
    "implement":    ["Read", "Write", "Edit", "Bash", "Grep", "Glob"],
    "review":       ["Read", "Grep", "Glob", "Bash(git diff *)", "Bash(git log *)", "Bash(git blame *)"],
    "security":     ["Read", "Grep", "Glob", "Bash(git diff *)", "Bash(grep *)"],
    "test":         ["Read", "Write", "Edit", "Bash(npm test *)", "Bash(pytest *)", "Bash(cargo test *)", "Bash(npx playwright *)", "Grep", "Glob"],
    "e2e":          ["Read", "Bash(npx playwright *)", "Bash(curl *)", "Bash(npm run *)", "Grep", "Glob"],
    "qa":           ["Read", "Write", "Edit", "Bash", "Grep", "Glob"],
    "explore":      ["Read", "Grep", "Glob", "Bash(git *)", "Bash(ls *)", "Bash(find *)"],
    "fix":          ["Read", "Write", "Edit", "Bash", "Grep", "Glob"],
    "orchestrator": ["Read", "Grep", "Glob", "WebFetch", "WebSearch"],
    "researcher":   ["Read", "Grep", "Glob", "WebFetch", "WebSearch"],
}

_GLOBAL_ECC = "~/.claude"  # injected into every agent via add_dirs

# ── Lane definitions ──────────────────────────────────────────────────────────
# Scheduling dimension — independent of mission_type tool presets.
# Each lane has its own concurrency cap, default model, tools, and role prompt.
# Total concurrent agents = sum of all max_agents values.
LANE_DEFAULTS: dict[str, dict] = {

    # ── Orchestrators (3 slots) — plan, coordinate, decompose ────────────────
    "orchestrator": {
        "max_agents": 3,
        "default_model": "claude-opus-4-7",
        "tool_preset": "orchestrator",
        "append_prompt": (
            "BEFORE proposing any DAG parallelism, your FIRST tool calls MUST be "
            "`mcp__devfleet-tools__list_project_missions` and the DevFleet dashboard read "
            "(get_dashboard via devfleet-context MCP). Read current fleet shape — slots per lane, "
            "free capacity — and shape the DAG accordingly. Do NOT assume the default 3-slot fleet. "
            "The operator may have scaled to 18 slots across 10 lanes "
            "(orchestrator×3, coder×3, reviewer×2, security×1, tester×2, e2e×2, "
            "qa×1, dynamic_tester×1, researcher×2, explorer×1). "
            "Shape parallelism to ACTUAL capacity, not assumed defaults.\n\n"
            "You are a DevFleet **Orchestrator**. Your role is coordination and planning.\n"
            "Use /prp-plan to produce implementation plans. Then /prompt-optimizer to sharpen them.\n"
            "Break large features into sub-missions via create_sub_mission, assign correct lanes.\n"
            "Advise opus frequently — call the advisor tool at every fork. Never guess on architecture."
        ),
        "color": "#b44ff7",
        "icon": "🧠",
    },

    # ── Coders (3 slots) — land features ─────────────────────────────────────
    "coder": {
        "max_agents": 3,
        "default_model": "claude-sonnet-4-6",
        "tool_preset": "implement",
        "append_prompt": (
            "You are a DevFleet **Coder**. Your role is implementation.\n"
            "Follow the /prp-implement plan exactly. Write atomic commits. Use /tdd.\n"
            "Before merging: check for conflicts with git merge --no-commit --no-ff first.\n"
            "If conflicts exist, abort and emit a sub-mission to the orchestrator lane.\n"
            "Context limit: when approaching 199k tokens, run /compact immediately.\n\n"
            "COMMIT FORMAT (non-negotiable):\n"
            "Use Farhanfeat/Farhanfix/Farhanupdate/Farhanrefactor/Farhantest/Farhanchore prefix.\n"
            "Example: `Farhanfeat(api): add shift task endpoint with checklist aggregation`\n"
            "Body must describe what changed — function names, endpoints, behaviour. No vague messages.\n"
            "ZERO attribution trailers — no Co-Authored-By, no Claude, no AI tool mentions. Ever."
        ),
        "color": "#4f8ef7",
        "icon": "🛠",
    },

    # ── Code Reviewer (2 slots) ───────────────────────────────────────────────
    "reviewer": {
        "max_agents": 2,
        "default_model": "claude-sonnet-4-6",
        "tool_preset": "review",
        "append_prompt": (
            "You are a DevFleet **Code Reviewer**. Read-only analysis only.\n"
            "Check: naming, patterns, error handling, test coverage, git hygiene.\n"
            "Score each dimension 0-10. Fail anything below 7. Be specific with line numbers.\n"
            "Use the code-reviewer ECC skill for systematic review.\n\n"
            "CRITICAL — multi-agent verification rule: NEVER re-read a file to confirm your own "
            "change looks correct. `Read` returns combined working-tree state including patches "
            "from other agents that may have already fixed the bug you're reviewing. "
            "To verify a specific commit, use: `git diff <baseline>..HEAD -- <file>` where "
            "<baseline> is the parent mission's commit SHA if available, or "
            "`git merge-base HEAD origin/main` as a fallback. "
            "Use `git show <commit-sha> -- <file>` to inspect what a commit actually wrote in "
            "isolation. When two reviewers work overlapping files, each must diff against their "
            "own baseline, not against HEAD."
        ),
        "color": "#f7a84f",
        "icon": "🔍",
    },

    # ── Security Reviewer (1 slot) ────────────────────────────────────────────
    "security": {
        "max_agents": 1,
        "default_model": "claude-sonnet-4-6",
        "tool_preset": "security",
        "append_prompt": (
            "You are a DevFleet **Security Reviewer**. Hunt vulnerabilities only.\n"
            "Check: OWASP Top 10, hardcoded secrets, injection, auth bypasses, unsafe deps.\n"
            "Use the security-reviewer ECC skill. Flag CRITICAL/HIGH/MEDIUM/LOW.\n"
            "BLOCK merge on any CRITICAL finding."
        ),
        "color": "#f74f4f",
        "icon": "🔒",
    },

    # ── Unit/Integration Tester (2 slots) ─────────────────────────────────────
    "tester": {
        "max_agents": 2,
        "default_model": "claude-haiku-4-5-20251001",
        "tool_preset": "test",
        "append_prompt": (
            "You are a DevFleet **Tester**. Write and run unit + integration tests.\n"
            "Minimum 80% coverage. Use /tdd — write test first, fail, implement, pass.\n"
            "Use the python-testing or tdd-workflow ECC skill.\n"
            "All tests must pass before submitting report."
        ),
        "color": "#4fc97b",
        "icon": "🧪",
    },

    # ── E2E Verifier (2 slots) ────────────────────────────────────────────────
    "e2e": {
        "max_agents": 2,
        "default_model": "claude-sonnet-4-6",
        "tool_preset": "e2e",
        "append_prompt": (
            "You are a DevFleet **E2E Verifier**. Run end-to-end and acceptance tests.\n"
            "Use Playwright for web, curl for APIs, runtime execution for CLI tools.\n"
            "Use the e2e-testing ECC skill. Capture screenshots on failure.\n"
            "Verify the golden path AND 3 edge cases minimum."
        ),
        "color": "#4ff7e8",
        "icon": "🌐",
    },

    # ── QA Agent (1 slot) ─────────────────────────────────────────────────────
    "qa": {
        "max_agents": 1,
        "default_model": "claude-sonnet-4-6",
        "tool_preset": "qa",
        "append_prompt": (
            "You are a DevFleet **QA Agent**. Holistic quality gatekeeper.\n"
            "Check: UX flows, accessibility, performance budgets, error messaging, docs accuracy.\n"
            "Run /verify. Score release readiness 0-100. Below 80 = not shippable."
        ),
        "color": "#f7d44f",
        "icon": "✅",
    },

    # ── Dynamic Tester (1 slot) ───────────────────────────────────────────────
    "dynamic_tester": {
        "max_agents": 1,
        "default_model": "claude-sonnet-4-6",
        "tool_preset": "test",
        "append_prompt": (
            "You are a DevFleet **Dynamic Tester**. Runtime and chaos testing.\n"
            "Inject failures: bad inputs, race conditions, resource exhaustion, network timeouts.\n"
            "Verify graceful degradation and error recovery in all cases.\n"
            "Use the ai-regression-testing ECC skill."
        ),
        "color": "#f7974f",
        "icon": "⚡",
    },

    # ── Researcher (2 slots) — feasibility + sustainability ───────────────────
    "researcher": {
        "max_agents": 2,
        "default_model": "claude-opus-4-7",
        "tool_preset": "researcher",
        "append_prompt": (
            "You are a DevFleet **Researcher**. Feasibility and sustainability analysis.\n"
            "Investigate: library alternatives, dependency risks, license compliance, long-term maintenance cost.\n"
            "Use the search-first and market-research ECC skills.\n"
            "Output: recommendation with evidence and risk rating."
        ),
        "color": "#a0a0f7",
        "icon": "🔬",
    },

    # ── Explorer (1 slot) — codebase discovery ────────────────────────────────
    "explorer": {
        "max_agents": 1,
        "default_model": "claude-sonnet-4-6",
        "tool_preset": "explore",
        "append_prompt": (
            "You are a DevFleet **Explorer**. Map the codebase and surface unknowns.\n"
            "Build dependency graphs, find dead code, identify coupling hotspots.\n"
            "Use the code-explorer ECC agent. Output structured findings for orchestrators."
        ),
        "color": "#f74f6b",
        "icon": "🔭",
    },
}

# Mapping from mission_type → default lane
MISSION_TYPE_TO_LANE: dict[str, str] = {
    "implement":    "coder",
    "fix":          "coder",
    "full":         "coder",
    "review":       "reviewer",
    "security":     "security",
    "test":         "tester",
    "e2e":          "e2e",
    "qa":           "qa",
    "dynamic_test": "dynamic_tester",
    "explore":      "explorer",
    "planner":      "orchestrator",
    "orchestrator": "orchestrator",
    "research":     "researcher",
}

MODEL_CHOICES = ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]


class DispatchOptions(BaseModel):
    """Per-dispatch overrides for Claude CLI invocation."""
    model: Optional[str] = None              # claude-opus-4-7, claude-sonnet-4-6, claude-haiku-4-5-20251001
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
    model: str = "claude-sonnet-4-6"
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
