"""
Reconcile orphaned DevFleet sessions whose code is already on main.

Background: a dispatcher bug (auto-merge running against a dirty project
working tree) caused sessions to be marked `failed / merge_blocked` even after
their devfleet/<short_id> branch had real work auto-committed on it. Someone
manually merged those branches into main, but the DevFleet DB was never
updated — so operators and external MCP callers see a wall of false "failed"
missions.

This script:
  1. Scans agent_sessions where status='failed' on git-backed projects.
  2. For each, checks whether the corresponding devfleet/<short_id> branch
     exists locally on the project, and whether it's fully contained in main.
  3. If fully contained → marks the session + mission `completed`, annotates
     the error_log with a reconciliation note, removes the stale worktree
     directory and deletes the branch.

Default is --dry-run. Pass --apply to actually mutate the DB and filesystem.

Usage:
  python reconcile_orphaned_missions.py                  # dry-run, all projects
  python reconcile_orphaned_missions.py --apply          # commit changes
  python reconcile_orphaned_missions.py --project myproj --apply
"""

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import aiosqlite

DB_PATH = os.environ.get(
    "DEVFLEET_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "devfleet.db"),
)


def _run_git(args: list[str], cwd: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _short(session_id: str) -> str:
    return session_id[:8]


def _branch_fully_on_main(project_path: str, branch: str) -> tuple[bool, str]:
    """Return (is_merged, reason). True iff `git branch --contains <sha>` shows
    main (or the project's HEAD-pointed branch) contains every commit on branch.
    """
    code, head_ref, _ = _run_git(["symbolic-ref", "--short", "HEAD"], project_path)
    if code != 0 or not head_ref:
        return False, f"detached HEAD or symbolic-ref failed in {project_path}"
    main_branch = head_ref

    code, sha, err = _run_git(["rev-parse", branch], project_path)
    if code != 0:
        return False, f"branch {branch} not found ({err[:120]})"

    code, ahead, _ = _run_git(["log", "--oneline", f"{main_branch}..{branch}"], project_path)
    if code != 0:
        return False, f"log {main_branch}..{branch} failed"
    if ahead.strip():
        return False, f"branch ahead of {main_branch} by {len(ahead.splitlines())} commit(s) — not yet merged"
    return True, f"fully contained in {main_branch}"


async def reconcile(apply: bool, project_filter: str | None) -> int:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row

    where = "p.path != '' "
    params: list = []
    if project_filter:
        where += "AND p.name = ? "
        params.append(project_filter)

    cur = await db.execute(
        f"""SELECT s.id AS sid, s.mission_id, s.status AS sstatus, s.error_type, s.last_error,
                  m.status AS mstatus, m.title,
                  p.path AS project_path, p.name AS project_name
           FROM agent_sessions s
           JOIN missions m ON m.id = s.mission_id
           JOIN projects p ON p.id = m.project_id
           WHERE s.status = 'failed' AND {where}
           ORDER BY s.started_at DESC""",
        params,
    )
    rows = await cur.fetchall()

    print(f"[reconcile] scanning {len(rows)} failed session(s){' for ' + project_filter if project_filter else ''}")

    reconciled = 0
    skipped = 0
    for r in rows:
        sid = r["sid"]
        short = _short(sid)
        branch = f"devfleet/{short}"
        project_path = r["project_path"]
        project_name = r["project_name"]
        worktree_dir = os.path.join(project_path, ".devfleet-worktrees", f"session-{short}")

        ok, reason = _branch_fully_on_main(project_path, branch)
        if not ok:
            skipped += 1
            print(f"  SKIP  {project_name}/{short}  ({r['title'][:50]})  →  {reason}")
            continue

        print(f"  HIT   {project_name}/{short}  ({r['title'][:50]})  →  {reason}")
        reconciled += 1
        if not apply:
            continue

        # Update DB
        ended_at = datetime.now(timezone.utc).isoformat()
        note = (
            f"Reconciled {ended_at}: branch {branch} was already merged into main "
            f"out-of-band. Original error_type={r['error_type'] or 'unknown'}; "
            f"original last_error: {(r['last_error'] or '')[:200]}"
        )
        await db.execute(
            "UPDATE agent_sessions SET status='completed', error_type='reconciled', "
            "last_error=?, ended_at=COALESCE(ended_at, ?) WHERE id=?",
            (note, ended_at, sid),
        )
        await db.execute(
            "UPDATE missions SET status='completed', updated_at=? WHERE id=?",
            (ended_at, r["mission_id"]),
        )
        await db.commit()

        # Remove stale worktree dir
        if os.path.isdir(worktree_dir):
            code, _, err = _run_git(
                ["worktree", "remove", "--force", worktree_dir], project_path,
            )
            if code != 0:
                # Fall back to shutil for the dir; then prune
                shutil.rmtree(worktree_dir, ignore_errors=True)
                _run_git(["worktree", "prune"], project_path)
                print(f"        cleaned worktree dir via shutil ({err[:120]})")
            else:
                print(f"        removed worktree {worktree_dir}")

        # Delete the now-redundant branch (it's already on main)
        code, _, err = _run_git(["branch", "-D", branch], project_path)
        if code == 0:
            print(f"        deleted branch {branch}")
        else:
            print(f"        could not delete branch {branch}: {err[:120]}")

    await db.close()

    print()
    print(f"[reconcile] {'APPLIED' if apply else 'DRY-RUN'}: {reconciled} reconciled, {skipped} skipped")
    if not apply and reconciled:
        print("[reconcile] re-run with --apply to commit changes")
    return 0 if reconciled or not rows else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Actually update the DB and remove worktree dirs. Default is dry-run.")
    parser.add_argument("--project", default=None,
                        help="Only reconcile sessions for this project (by name).")
    args = parser.parse_args()
    sys.exit(asyncio.run(reconcile(apply=args.apply, project_filter=args.project)))


if __name__ == "__main__":
    main()
