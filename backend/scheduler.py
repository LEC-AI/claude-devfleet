"""
Mission Scheduler — Cron-based recurring mission dispatch.

Background task that evaluates cron schedules on template missions and
creates fresh cloned missions when they're due. The cloned missions are
created with auto_dispatch=1, so the mission_watcher picks them up.

Template missions (those with schedule_cron set) are never dispatched
directly — they serve as templates that get cloned each time the
schedule fires.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import db

# How often to fetch origin/dev for all registered git projects (seconds)
_GIT_SYNC_INTERVAL = int(os.environ.get("DEVFLEET_GIT_SYNC_INTERVAL", "300"))  # 5 min default
_git_sync_task: asyncio.Task | None = None

log = logging.getLogger("devfleet.scheduler")

_scheduler_task: asyncio.Task | None = None
CHECK_INTERVAL = int(os.environ.get("DEVFLEET_SCHEDULER_INTERVAL", "60"))

# Lightweight cron matching — no external dependency needed
# Supports: minute hour day_of_month month day_of_week
# Supports: *, */N, N, N-M, N,M,O


def _match_cron_field(field: str, value: int, max_val: int) -> bool:
    """Check if a value matches a cron field expression."""
    if field == "*":
        return True
    for part in field.split(","):
        part = part.strip()
        if "/" in part:
            base, step = part.split("/", 1)
            step = int(step)
            if base == "*":
                if value % step == 0:
                    return True
            elif "-" in base:
                lo, hi = base.split("-", 1)
                if int(lo) <= value <= int(hi) and (value - int(lo)) % step == 0:
                    return True
        elif "-" in part:
            lo, hi = part.split("-", 1)
            if int(lo) <= value <= int(hi):
                return True
        else:
            if int(part) == value:
                return True
    return False


def cron_matches_now(cron_expr: str) -> bool:
    """Check if a cron expression matches the current UTC time (minute precision)."""
    try:
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            return False
        now = datetime.now(timezone.utc)
        minute, hour, dom, month, dow = parts
        return (
            _match_cron_field(minute, now.minute, 59)
            and _match_cron_field(hour, now.hour, 23)
            and _match_cron_field(dom, now.day, 31)
            and _match_cron_field(month, now.month, 12)
            and _match_cron_field(dow, now.weekday(), 6)  # 0=Monday in Python
        )
    except (ValueError, IndexError):
        log.warning("Invalid cron expression: %s", cron_expr)
        return False


async def _check_schedules():
    """Check all scheduled missions and create clones for any that are due."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            """SELECT m.*, p.name AS project_name
               FROM missions m
               JOIN projects p ON p.id = m.project_id
               WHERE m.schedule_enabled = 1
                 AND m.schedule_cron IS NOT NULL
                 AND m.schedule_cron != ''"""
        )

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        # Truncate to minute for dedup (don't fire same cron twice in same minute)
        now_minute = now.strftime("%Y-%m-%dT%H:%M")

        for row in rows:
            template = dict(row)
            cron = template["schedule_cron"]

            if not cron_matches_now(cron):
                continue

            # Check if already fired this minute
            last = template.get("last_scheduled_at") or ""
            if last and last[:16] >= now_minute:
                continue

            # Clone the template into a new mission with auto_dispatch=1
            new_id = str(uuid.uuid4())
            tags = json.loads(template.get("tags", "[]"))
            tags.append("scheduled")
            tags.append(f"template:{template['id']}")

            # Get next mission number for this project
            num_rows = await conn.execute_fetchall(
                "SELECT COALESCE(MAX(mission_number), 0) + 1 AS next_num FROM missions WHERE project_id=?",
                (template["project_id"],),
            )
            next_num = num_rows[0][0] if num_rows else 1

            await conn.execute(
                """INSERT INTO missions
                   (id, project_id, title, detailed_prompt, acceptance_criteria,
                    priority, tags, model, max_turns, max_budget_usd,
                    allowed_tools, mission_type, parent_mission_id, auto_dispatch, mission_number)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (new_id, template["project_id"],
                 f"{template['title']} ({now.strftime('%Y-%m-%d %H:%M')})",
                 template["detailed_prompt"],
                 template.get("acceptance_criteria", ""),
                 template.get("priority", 0),
                 json.dumps(tags),
                 template.get("model", "claude-opus-4-7"),
                 template.get("max_turns"),
                 template.get("max_budget_usd"),
                 template.get("allowed_tools", ""),
                 template.get("mission_type", "implement"),
                 template["id"],
                 next_num),
            )

            # Update last_scheduled_at on template
            await conn.execute(
                "UPDATE missions SET last_scheduled_at=? WHERE id=?",
                (now_iso, template["id"]),
            )

            log.info(
                "Scheduled mission '%s' → cloned as %s (cron: %s)",
                template["title"], new_id, cron,
            )

        await conn.commit()
    except Exception as e:
        log.error("Scheduler error: %s", e)
    finally:
        await conn.close()


async def _scheduler_loop():
    """Main scheduler loop — check schedules every CHECK_INTERVAL seconds."""
    log.info("Scheduler started (check every %ds)", CHECK_INTERVAL)
    while True:
        try:
            await _check_schedules()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Scheduler loop error: %s", e)
        await asyncio.sleep(CHECK_INTERVAL)


async def _fetch_project(path: str) -> None:
    """Run git fetch origin dev for a single project path."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "fetch", "origin", "dev",
            cwd=path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            log.warning("git fetch failed for %s: %s", path, stderr.decode().strip()[:200])
        else:
            log.debug("git fetch origin dev OK: %s", path)
    except asyncio.TimeoutError:
        log.warning("git fetch timed out for %s", path)
    except Exception as e:
        log.debug("git fetch skipped for %s: %s", path, e)


async def _git_sync_loop():
    """Periodically fetch origin/dev for all registered git projects."""
    log.info("Git sync loop started (every %ds)", _GIT_SYNC_INTERVAL)
    while True:
        await asyncio.sleep(_GIT_SYNC_INTERVAL)
        try:
            conn = await db.get_db()
            try:
                rows = await conn.execute_fetchall("SELECT path FROM projects WHERE path IS NOT NULL AND path != ''")
            finally:
                await conn.close()

            paths = [r["path"] for r in rows if r["path"] and os.path.isdir(os.path.join(r["path"], ".git"))]
            if paths:
                log.info("Git sync: fetching origin/dev for %d project(s)", len(paths))
                await asyncio.gather(*[_fetch_project(p) for p in paths], return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Git sync loop error: %s", e)


async def start_scheduler():
    """Start the scheduler and git sync background tasks."""
    global _scheduler_task, _git_sync_task
    if _scheduler_task and not _scheduler_task.done():
        return
    _scheduler_task = asyncio.create_task(_scheduler_loop())
    _git_sync_task = asyncio.create_task(_git_sync_loop())
    log.info("Git sync task started (interval: %ds)", _GIT_SYNC_INTERVAL)


async def stop_scheduler():
    """Stop the scheduler and git sync tasks."""
    global _scheduler_task, _git_sync_task
    for task in (_scheduler_task, _git_sync_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    _scheduler_task = None
    _git_sync_task = None


def get_scheduler_status() -> dict:
    """Get the scheduler status."""
    return {
        "active": _scheduler_task is not None and not _scheduler_task.done(),
        "check_interval": CHECK_INTERVAL,
    }
