"""
Swarm Fan-Out Planner (Track 1)

Decomposes one goal into a full dependency graph of missions up front and
inserts them all at once. Unlike autoloop.py (1-3 tasks per iteration, wait,
re-plan), a swarm hands the whole graph to mission_watcher.py, which already
dispatches any auto_dispatch=1 mission whose depends_on are all completed.

Flow:
  1. plan_swarm: one Claude call → list of tasks with depends_on_index
  2. launch_swarm: insert a draft "swarm root" mission (tag swarm_root, never
     dispatched) + one auto_dispatch=1 child per task, with depends_on
     resolved from list indices to real mission IDs
  3. mission_watcher (unmodified) fans the graph out as dependencies complete

Contract: a swarm is any mission whose parent_mission_id points at a mission
whose tags contain "swarm_root". Tracks 5 and 6 key off this tag.

Quality gate: sdk_engine auto-merges on _validate_completion, which only
checks that work happened, not that it's correct. The prompt below therefore
requires every task to run the project's tests/lint/build before submitting.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import db

log = logging.getLogger("devfleet.swarm_planner")

SWARM_ROOT_TAG = "swarm_root"
SWARM_MEMBER_TAG = "swarm"
MAX_SWARM_TASKS = 20  # Safety limit, mirrors autoloop's max_iterations

QUALITY_GATE = (
    "\n\n## Before you finish (required)\n"
    "Run the project's existing tests, lint and build commands (look for them in "
    "CLAUDE.md, README, package.json, pyproject.toml, Makefile, etc.). If any of them "
    "fail, fix the problem and run them again. Do NOT call submit_report until they "
    "pass. If the project has no tests for what you built, add them. If a failure is "
    "genuinely outside this task's scope, say so explicitly in errors_encountered "
    "and what_untested instead of reporting success."
)

SWARM_PROMPT = """You are a DevFleet swarm planner. Decompose a goal into a COMPLETE set of coding tasks that parallel agents will execute, with explicit dependencies between them.

## Goal
{goal}

## Project Path
{project_path}

## Instructions
Inspect the project, then plan every task needed to achieve the goal. Tasks run in isolated git worktrees and merge into the main branch when done, so a task that depends on another's code must list it in depends_on_index.

Respond with ONLY a JSON array (no markdown, no code fences, no commentary):
[
  {{
    "title": "Short task title",
    "detailed_prompt": "Full implementation prompt for the coding agent. Reference specific files.",
    "acceptance_criteria": "Bullet list of what defines done",
    "priority": 3,
    "depends_on_index": []
  }},
  {{
    "title": "Integration tests for X",
    "detailed_prompt": "...",
    "acceptance_criteria": "...",
    "priority": 2,
    "depends_on_index": [0]
  }}
]

Rules:
- depends_on_index holds 0-based positions of OTHER tasks in this same array. No cycles.
- Only add a dependency when a task truly needs another task's merged code.
- Tasks with no dependency on each other must touch different files, so their merges don't conflict.
- Up to {max_agents} agents run at once — aim for that much parallelism at each stage.
- Each task should be completable in one agent session (30-60 min of work).
- At most {max_tasks} tasks in total.
- priority: higher runs first when slots are scarce (0-5).
- Every task must run the project's tests/lint/build before finishing and keep fixing until they pass.
"""


# ── Goal record stub ──
# Track 3 (goals.py / goals_registry) is the durable source of truth for a
# project's goal. Until it lands, swarms keep a local record of the same
# shape. At integration, replace these two functions with
# goals.create_goal / goals.get_active_goal.
_goal_stub: dict[str, dict] = {}


async def _record_goal(project_id: str, goal_text: str) -> str:
    goal_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    _goal_stub[project_id] = {
        "id": goal_id, "project_id": project_id, "goal_text": goal_text,
        "status": "active", "max_iterations": 1, "current_iteration": 1,
        "created_at": now, "updated_at": now, "stopped_reason": None,
    }
    return goal_id


async def get_active_goal(project_id: str) -> dict | None:
    return _goal_stub.get(project_id)


async def _call_planner(prompt: str, cwd: str) -> str:
    """Call Claude via SDK if available, fall back to CLI subprocess.

    Same pattern as autoloop._call_planner (deliberately not imported).
    """
    try:
        from claude_code_sdk import query as sdk_query, ClaudeCodeOptions
        from claude_code_sdk.types import TextBlock

        options = ClaudeCodeOptions(
            model="claude-sonnet-4-6",
            permission_mode="bypassPermissions",
            max_turns=1,
            cwd=cwd,
        )

        output_parts = []
        async for message in sdk_query(prompt=prompt, options=options):
            if message is None:
                continue
            if hasattr(message, "content"):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        output_parts.append(block.text)
            elif hasattr(message, "result") and message.result:
                output_parts.append(message.result)

        return "\n".join(output_parts).strip()

    except ImportError:
        process = await asyncio.create_subprocess_exec(
            "claude",
            "--print",
            "--dangerously-skip-permissions",
            "--model", "claude-sonnet-4-6",
            "-p", prompt,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )
        stdout, _ = await process.communicate()
        return stdout.decode("utf-8", errors="replace").strip()


def _extract_json(output: str):
    """Pull a JSON array out of planner output, tolerating code fences or prose."""
    text = output
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end <= start:
            raise ValueError(f"Planner returned no JSON array: {output[:500]}")
        return json.loads(text[start:end + 1])


def _validate_tasks(raw) -> list[dict]:
    """Normalize planner tasks and reject bad dependency graphs.

    Raises ValueError on missing fields, out-of-range/self indices, or cycles.
    """
    if isinstance(raw, dict) and isinstance(raw.get("tasks"), list):
        raw = raw["tasks"]
    if not isinstance(raw, list) or not raw:
        raise ValueError("Planner must return a non-empty list of tasks")
    if len(raw) > MAX_SWARM_TASKS:
        raise ValueError(f"Planner returned {len(raw)} tasks (max {MAX_SWARM_TASKS})")

    tasks = []
    n = len(raw)
    for i, t in enumerate(raw):
        if not isinstance(t, dict) or not t.get("title") or not t.get("detailed_prompt"):
            raise ValueError(f"Task {i} is missing title or detailed_prompt")
        deps = t.get("depends_on_index") or []
        if not isinstance(deps, list) or not all(isinstance(d, int) for d in deps):
            raise ValueError(f"Task {i} depends_on_index must be a list of ints")
        for d in deps:
            if d == i or not 0 <= d < n:
                raise ValueError(f"Task {i} has invalid dependency index {d}")
        tasks.append({
            "title": str(t["title"]),
            "detailed_prompt": str(t["detailed_prompt"]),
            "acceptance_criteria": str(t.get("acceptance_criteria", "")),
            "priority": int(t.get("priority", 2)),
            "depends_on_index": sorted(set(deps)),
        })

    # Cycle check (Kahn's algorithm) — a cycle would leave missions draft forever
    indegree = [len(t["depends_on_index"]) for t in tasks]
    dependents: dict[int, list[int]] = {i: [] for i in range(n)}
    for i, t in enumerate(tasks):
        for d in t["depends_on_index"]:
            dependents[d].append(i)
    ready = [i for i in range(n) if indegree[i] == 0]
    visited = 0
    while ready:
        i = ready.pop()
        visited += 1
        for j in dependents[i]:
            indegree[j] -= 1
            if indegree[j] == 0:
                ready.append(j)
    if visited != n:
        raise ValueError("Planner returned a dependency cycle")

    return tasks


async def plan_swarm(goal: str, project_path: str, max_agents: int) -> list[dict]:
    """One Claude call → validated list of task dicts.

    Each task: title, detailed_prompt, acceptance_criteria, priority,
    depends_on_index (indices into the same list). Raises ValueError if the
    plan is unusable.
    """
    prompt = SWARM_PROMPT.format(
        goal=goal,
        project_path=project_path,
        max_agents=max_agents,
        max_tasks=MAX_SWARM_TASKS,
    )
    output = await _call_planner(prompt, project_path)
    return _validate_tasks(_extract_json(output))


async def launch_swarm(project_id: str, goal: str, max_agents: int) -> str:
    """Plan a swarm and insert it as missions. Returns the swarm root mission id.

    Dispatch is left to mission_watcher: children are auto_dispatch=1 and the
    root is a draft with auto_dispatch=0, so it is never dispatched.
    """
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM projects WHERE id=?", (project_id,))
        if not rows:
            raise LookupError(f"Project {project_id} not found")
        project = dict(rows[0])
    finally:
        await conn.close()

    tasks = await plan_swarm(goal, project["path"], max_agents)
    goal_id = await _record_goal(project_id, goal)

    root_id = str(uuid.uuid4())
    child_ids = [str(uuid.uuid4()) for _ in tasks]

    conn = await db.get_db()
    try:
        num_rows = await conn.execute_fetchall(
            "SELECT COALESCE(MAX(mission_number), 0) + 1 AS next_num FROM missions WHERE project_id=?",
            (project_id,),
        )
        next_num = num_rows[0][0] if num_rows else 1

        await conn.execute(
            """INSERT INTO missions (id, project_id, title, detailed_prompt, acceptance_criteria,
                                     status, tags, auto_dispatch, mission_number)
               VALUES (?, ?, ?, ?, ?, 'draft', ?, 0, ?)""",
            (root_id, project_id, f"Swarm: {goal[:80]}", goal,
             f"All {len(tasks)} swarm missions completed",
             json.dumps([SWARM_ROOT_TAG]), next_num),
        )

        for i, (task, mid) in enumerate(zip(tasks, child_ids)):
            depends_on = [child_ids[d] for d in task["depends_on_index"]]
            await conn.execute(
                """INSERT INTO missions (id, project_id, title, detailed_prompt, acceptance_criteria,
                                         status, priority, tags, parent_mission_id, depends_on,
                                         auto_dispatch, mission_number)
                   VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, 1, ?)""",
                (mid, project_id, task["title"], task["detailed_prompt"] + QUALITY_GATE,
                 task["acceptance_criteria"], task["priority"], json.dumps([SWARM_MEMBER_TAG]),
                 root_id, json.dumps(depends_on), next_num + 1 + i),
            )

        await conn.execute(
            "INSERT INTO mission_events (mission_id, event_type, data) VALUES (?, ?, ?)",
            (root_id, "swarm_launched",
             json.dumps({"goal_id": goal_id, "tasks": len(tasks), "max_agents": max_agents})),
        )
        await conn.commit()
    finally:
        await conn.close()

    log.info("Swarm %s launched for project %s: %d missions", root_id, project_id, len(tasks))
    return root_id


async def get_swarm_status(swarm_id: str) -> dict | None:
    """Status rollup of a swarm's children. None if swarm_id isn't a swarm root."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            """SELECT m.* FROM missions m
               WHERE m.id=? AND EXISTS (
                 SELECT 1 FROM json_each(m.tags) t WHERE t.value=?
               )""",
            (swarm_id, SWARM_ROOT_TAG),
        )
        if not rows:
            return None
        root = dict(rows[0])

        counts = await conn.execute_fetchall(
            "SELECT status, COUNT(*) AS n FROM missions WHERE parent_mission_id=? GROUP BY status",
            (swarm_id,),
        )
        by_status = {r["status"]: r["n"] for r in counts}

        # Drafts that can never run because a dependency failed
        blocked = await conn.execute_fetchall(
            """SELECT COUNT(*) FROM missions m
               WHERE m.parent_mission_id=? AND m.status='draft'
                 AND EXISTS (
                   SELECT 1 FROM json_each(m.depends_on) dep
                   JOIN missions d ON d.id = dep.value
                   WHERE d.status IN ('failed', 'cancelled')
                 )""",
            (swarm_id,),
        )
    finally:
        await conn.close()

    total = sum(by_status.values())
    return {
        "swarm_id": swarm_id,
        "project_id": root["project_id"],
        "goal": root["detailed_prompt"],
        "created_at": root["created_at"],
        "total": total,
        "draft": by_status.get("draft", 0),
        "running": by_status.get("running", 0),
        "completed": by_status.get("completed", 0),
        "failed": by_status.get("failed", 0),
        "other": total - sum(by_status.get(s, 0) for s in ("draft", "running", "completed", "failed")),
        "blocked_by_failure": blocked[0][0] if blocked else 0,
        "done": total > 0 and by_status.get("completed", 0) == total,
    }
