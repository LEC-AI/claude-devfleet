"""
One-time Opus 4.7 critique of all lane prompts vs ECC best practices.
Called via POST /api/lanes/run-critique — runs as a background asyncio task.
"""
import asyncio
import json
import logging
import os

import db
from models import LANE_DEFAULTS

log = logging.getLogger("devfleet.lane_critique")

ECC_SKILL_MAP = {
    "orchestrator": "enterprise-agent-ops, council, blueprint",
    "coder": "tdd-workflow, coding-standards, prp-implement",
    "reviewer": "code-reviewer agent, santa-loop, code-review",
    "security": "security-review, security-reviewer agent",
    "tester": "tdd-workflow, python-testing, ai-regression-testing",
    "e2e": "e2e-testing, playwright, e2e-runner agent",
    "qa": "verification-loop, quality-gate",
    "dynamic_tester": "ai-regression-testing, chaos engineering",
    "researcher": "deep-research, market-research, search-first",
    "explorer": "code-explorer agent, iterative-retrieval",
}

_CRITIQUE_PROMPT = """You are reviewing a DevFleet agent lane prompt for quality and completeness.

Lane: {lane_name}
Relevant ECC skills: {skills}

Current lane prompt:
{prompt}

Analyze this prompt and return ONLY valid JSON with this exact shape:
{{
  "ecc_skill_mapping": "one-line description of which ECC skill this maps to",
  "gaps": ["missing behavior or instruction 1", "missing behavior or instruction 2"],
  "conflicts": ["conflicting or problematic instruction 1"],
  "suggestions": [
    {{"text": "concrete addition text", "category": "rules"}},
    {{"text": "another addition", "category": "quality_gates"}},
    {{"text": "context reference", "category": "context_hints"}}
  ]
}}

category must be one of: rules, quality_gates, context_hints.
Be specific and actionable. Return only valid JSON, no commentary."""


async def run_critique_batch() -> None:
    """Run Opus 4.7 critique for all 10 lanes and store results in DB."""
    try:
        import anthropic
    except ImportError:
        log.error("anthropic package not installed — cannot run critique batch")
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set — cannot run critique batch")
        return

    client = anthropic.Anthropic(api_key=api_key)
    conn = await db.get_db()

    try:
        for lane_name, policy in LANE_DEFAULTS.items():
            prompt_text = policy.get("append_prompt", "")
            skills = ECC_SKILL_MAP.get(lane_name, "general agent patterns")

            log.info("Running Opus critique for lane: %s", lane_name)
            try:
                msg = await asyncio.to_thread(
                    client.messages.create,
                    model="claude-opus-4-7",
                    max_tokens=1500,
                    messages=[{
                        "role": "user",
                        "content": _CRITIQUE_PROMPT.format(
                            lane_name=lane_name,
                            skills=skills,
                            prompt=prompt_text,
                        ),
                    }],
                )
                raw_text = msg.content[0].text.strip()
                # Strip markdown code fences if present
                if raw_text.startswith("```"):
                    lines = raw_text.split("\n")
                    raw_text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
                critique = json.loads(raw_text)
            except Exception as exc:
                log.warning("Critique failed for lane %s: %s", lane_name, exc)
                critique = {"error": str(exc), "ecc_skill_mapping": "", "gaps": [], "conflicts": [], "suggestions": []}

            await conn.execute(
                """INSERT INTO lane_prompt_critiques (lane_name, critique_json)
                   VALUES (?, ?)
                   ON CONFLICT(lane_name) DO UPDATE SET
                     critique_json = excluded.critique_json,
                     created_at = datetime('now')""",
                (lane_name, json.dumps(critique)),
            )
            await conn.commit()
            log.info("Stored critique for lane: %s", lane_name)
    finally:
        await conn.close()

    log.info("Lane critique batch complete for %d lanes", len(LANE_DEFAULTS))
