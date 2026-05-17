"""
Enhanced Project Planner — Uses extended thinking + structured outputs

Improvements over planner.py:
- Extended thinking (reasoning tokens) for better mission planning
- Structured JSON outputs for mission chains
- Suggests optimal parallelization and critical path
- Estimates complexity/time per mission
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional, List

from db import get_db

log = logging.getLogger("devfleet.planner_v2")

INTELLIGENT_PLANNER_PROMPT = """You are an expert DevFleet project architect. Analyze this project request carefully using extended reasoning.

## Request
{user_prompt}

## Project Path
{project_path}

## Your Task

Break down this project into 2-5 well-scoped, parallelizable missions. Think deeply about:
1. **Dependencies**: What must happen sequentially vs in parallel?
2. **Optimal ordering**: What's the critical path?
3. **Complexity**: Is this realistic for a single agent session?
4. **Testing**: Should testing be integrated or separate?

## Output Format (JSON only, no markdown)

{{
  "project_name": "Name",
  "project_description": "One-line description",
  "complexity_estimate": "low|medium|high",
  "estimated_total_hours": 2.5,
  "can_parallelize": true,
  "critical_path_length": 2,
  "missions": [
    {{
      "title": "Mission title",
      "detailed_prompt": "Specific, actionable prompt (3-6 sentences)",
      "acceptance_criteria": "Bullet list of concrete done criteria",
      "mission_type": "scaffold|implement|feature|test|fix|review",
      "tags": ["tag1"],
      "depends_on_index": null,
      "priority": 1,
      "estimated_hours": 1.5,
      "complexity": "low|medium|high"
    }}
  ]
}}

## Rules
- First mission must have depends_on_index: null
- Use depends_on_index sparingly (enable parallelization where possible)
- Estimate hours realistically (agents are smart but not magic)
- Each prompt must be self-contained
- Include edge cases and error handling in criteria
"""


async def _call_intelligent_planner(prompt: str, cwd: str) -> str:
    """Call Claude with extended thinking for intelligent planning."""
    try:
        from claude_code_sdk import query as sdk_query, ClaudeCodeOptions

        options = ClaudeCodeOptions(
            model="claude-opus-4-7",
            permission_mode="bypassPermissions",
            max_turns=1,
            cwd=cwd,
            thinking={
                "type": "enabled",
                "budget_tokens": 8000  # Allow deep reasoning for planning
            }
        )

        output_parts = []
        async for message in sdk_query(prompt=prompt, options=options):
            if message is None:
                continue
            if hasattr(message, "content"):
                for block in message.content:
                    if hasattr(block, "text"):
                        output_parts.append(block.text)
            elif hasattr(message, "result") and message.result:
                output_parts.append(message.result)

        return "\n".join(output_parts).strip()

    except ImportError:
        # Fallback to CLI
        import asyncio
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt, "--output-format", "text",
            "--model", "claude-opus-4-7", "--max-turns", "1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip()


async def plan_project_intelligent(user_prompt: str, project_path: str) -> dict:
    """
    Enhanced project planner with extended thinking and structured outputs.

    Returns: {"project": {...}, "missions": [...], "analysis": {...}}
    """
    os.makedirs(project_path, exist_ok=True)

    # Initialize git if needed
    git_dir = os.path.join(project_path, ".git")
    if not os.path.exists(git_dir):
        import asyncio
        proc = await asyncio.create_subprocess_exec(
            "git", "init", cwd=project_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        proc = await asyncio.create_subprocess_exec(
            "git", "commit", "--allow-empty", "-m", "Initial commit",
            cwd=project_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    # Call planner with extended thinking
    prompt = INTELLIGENT_PLANNER_PROMPT.format(
        user_prompt=user_prompt,
        project_path=project_path
    )

    plan_json = await _call_intelligent_planner(prompt, project_path)

    # Parse response
    try:
        plan = json.loads(plan_json)
    except json.JSONDecodeError:
        # Try to extract JSON from markdown code blocks
        if "```json" in plan_json:
            plan = json.loads(plan_json.split("```json")[1].split("```")[0])
        elif "```" in plan_json:
            plan = json.loads(plan_json.split("```")[1].split("```")[0])
        else:
            raise ValueError(f"Could not parse plan JSON: {plan_json[:200]}")

    # Create project in DB
    db = await get_db()
    project_id = str(uuid.uuid4())

    try:
        await db.execute(
            "INSERT INTO projects (id, name, path, description) VALUES (?, ?, ?, ?)",
            (project_id, plan["project_name"], project_path, plan.get("project_description", ""))
        )

        # Create missions
        missions = []
        mission_ids_map = {}

        for i, mission_data in enumerate(plan.get("missions", [])):
            mission_id = str(uuid.uuid4())
            mission_ids_map[i] = mission_id

            depends_on = []
            if mission_data.get("depends_on_index") is not None:
                dep_idx = mission_data["depends_on_index"]
                if dep_idx in mission_ids_map:
                    depends_on = [mission_ids_map[dep_idx]]

            await db.execute("""
                INSERT INTO missions
                (id, project_id, title, detailed_prompt, acceptance_criteria,
                 mission_type, tags, priority, depends_on, auto_dispatch, model, mission_number)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                mission_id,
                project_id,
                mission_data["title"],
                mission_data["detailed_prompt"],
                "\n".join(mission_data.get("acceptance_criteria", []))
                    if isinstance(mission_data.get("acceptance_criteria"), list)
                    else mission_data.get("acceptance_criteria", ""),
                mission_data.get("mission_type", "implement"),
                json.dumps(mission_data.get("tags", [])),
                mission_data.get("priority", 1),
                json.dumps(depends_on),
                1,  # auto_dispatch=true
                "claude-opus-4-7",
                i + 1
            ))

            missions.append({
                "id": mission_id,
                "title": mission_data["title"],
                "order": i + 1,
                "depends_on": depends_on,
                "complexity": mission_data.get("complexity", "medium"),
                "estimated_hours": mission_data.get("estimated_hours", 1)
            })

        await db.commit()
    finally:
        await db.close()

    return {
        "project": {
            "id": project_id,
            "name": plan["project_name"],
            "description": plan.get("project_description", ""),
            "path": project_path
        },
        "missions": missions,
        "analysis": {
            "complexity": plan.get("complexity_estimate", "medium"),
            "estimated_total_hours": plan.get("estimated_total_hours", 5),
            "can_parallelize": plan.get("can_parallelize", False),
            "critical_path_length": plan.get("critical_path_length", 1)
        }
    }
