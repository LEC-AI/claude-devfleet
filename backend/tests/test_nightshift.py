"""Night shift: night missions stack on the review branch, main never moves,
resume reuses the session's worktree, and the window gate holds."""
import asyncio
import subprocess
from datetime import datetime, timezone

import worktree
from mission_watcher import NIGHT_WINDOW
from night_window import is_within_window


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_night_missions_stack_on_review_branch(tmp_path):
    repo = str(tmp_path)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
    main_sha = git(repo, "rev-parse", "main")

    async def run():
        for sid in ("aaaaaaaa-1", "bbbbbbbb-2"):
            wt = await worktree.create_worktree(repo, sid, base=worktree.NIGHT_BRANCH)
            assert await worktree.create_worktree(repo, sid, base=worktree.NIGHT_BRANCH) == wt
            open(f"{wt}/{sid}.txt", "w").write(sid)
            git(wt, "config", "user.email", "t@t")
            git(wt, "config", "user.name", "t")
            assert await worktree.cleanup_worktree(repo, sid, merge=True, into=worktree.NIGHT_BRANCH)

    asyncio.run(run())
    assert git(repo, "ls-tree", "--name-only", worktree.NIGHT_BRANCH).split() == ["aaaaaaaa-1.txt", "bbbbbbbb-2.txt"]
    assert git(repo, "rev-parse", "main") == main_sha


def test_default_night_window():
    at = lambda h: datetime(2026, 1, 15, h, 0, tzinfo=timezone.utc)  # January: London == UTC
    assert is_within_window(NIGHT_WINDOW, at(20)) and is_within_window(NIGHT_WINDOW, at(3))
    assert not is_within_window(NIGHT_WINDOW, at(8)) and not is_within_window(NIGHT_WINDOW, at(14))


def test_one_night_mission_per_project_per_tick(temp_db, monkeypatch):
    import json as _json
    import db
    import mission_watcher

    monkeypatch.setattr(mission_watcher, "in_night_window", lambda: True)

    async def run():
        conn = await db.get_db()
        await conn.execute("INSERT INTO projects (id, name, path) VALUES ('p1', 'p1', '/tmp/p1')")
        for i in range(2):
            await conn.execute(
                "INSERT INTO missions (id, project_id, title, detailed_prompt, tags, auto_dispatch) "
                "VALUES (?, 'p1', ?, 'x', ?, 1)", (f"m{i}", f"m{i}", _json.dumps(["night"])),
            )
        await conn.commit()
        await conn.close()
        return await mission_watcher._find_eligible_missions(limit=3)

    assert len(asyncio.run(run())) == 1


def test_one_night_resume_per_project(temp_db):
    import db
    import mission_watcher

    async def run():
        conn = await db.get_db()
        await conn.execute("INSERT INTO projects (id, name, path) VALUES ('p1', 'p1', '/tmp/p1')")
        for i in range(2):
            await conn.execute(
                "INSERT INTO missions (id, project_id, title, detailed_prompt, tags, status) "
                "VALUES (?, 'p1', ?, 'x', '[\"night\"]', 'failed')", (f"m{i}", f"m{i}"),
            )
            await conn.execute(
                "INSERT INTO agent_sessions (id, mission_id, status, claude_session_id, error_type, ended_at) "
                "VALUES (?, ?, 'failed', 'c', 'rate_limit_exhausted', datetime('now', '-1 hour'))", (f"s{i}", f"m{i}"),
            )
        await conn.commit()
        await conn.close()
        return await mission_watcher._find_resumable_night_missions(limit=3)

    assert len(asyncio.run(run())) == 1
