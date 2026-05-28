"""
Mark unrecoverably-stale DevFleet worktree branches as dead, and clean their
on-disk state.

A branch is "stale_base_irrecoverable" when:
  - Its merge-base is N commits behind target_ref (default >= 20)
  - A pre-merge rebase onto target_ref cannot complete cleanly
i.e. main has moved far enough that the agent's work cannot mechanically
reconcile and re-dispatch is the right answer.

For each such branch on the given project:
  - Mark the session status='failed' and error_type='stale_base_irrecoverable'
  - Annotate last_error with the behind-count and current main HEAD
  - Remove the worktree directory
  - Delete the branch

Default is --dry-run. Pass --apply to commit.
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
STALE_THRESHOLD = 20  # commits behind target_ref to qualify


def _git(args: list[str], cwd: str) -> tuple[int, str, str]:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def _list_devfleet_branches(project_path: str) -> list[str]:
    code, out, _ = _git(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads/devfleet/"],
        project_path,
    )
    if code != 0:
        return []
    return [l.strip() for l in out.splitlines() if l.strip().startswith("devfleet/")]


def _behind_count(project_path: str, branch: str, main_branch: str) -> int:
    code, base, _ = _git(["merge-base", main_branch, branch], project_path)
    if code != 0:
        return -1
    code, n, _ = _git(["rev-list", "--count", f"{base.strip()}..{main_branch}"], project_path)
    if code != 0:
        return -1
    try:
        return int(n.strip())
    except ValueError:
        return -1


def _can_rebase_cleanly(project_path: str, branch: str, main_branch: str) -> bool:
    short = branch.removeprefix("devfleet/")
    worktree = os.path.join(project_path, ".devfleet-worktrees", f"session-{short}")
    if not os.path.isdir(worktree):
        return False
    code, dirty, _ = _git(["status", "--porcelain"], worktree)
    if code != 0 or dirty.strip():
        return False
    code, _, _ = _git(["rebase", main_branch], worktree)
    if code != 0:
        _git(["rebase", "--abort"], worktree)
        return False
    # Roll back the rebase so the branch state is unchanged for now
    _git(["reset", "--hard", "ORIG_HEAD"], worktree)
    return True


def _cleanup(project_path: str, branch: str) -> str:
    short = branch.removeprefix("devfleet/")
    wt = os.path.join(project_path, ".devfleet-worktrees", f"session-{short}")
    msgs = []
    code, _, err = _git(["worktree", "remove", "--force", wt], project_path)
    if code != 0:
        if os.path.isdir(wt):
            shutil.rmtree(wt, ignore_errors=True)
        _git(["worktree", "prune"], project_path)
        msgs.append(f"shutil-removed ({err[:80]})")
    else:
        msgs.append("removed worktree")
    code, _, err = _git(["branch", "-D", branch], project_path)
    msgs.append("deleted branch" if code == 0 else f"branch-delete err: {err[:80]}")
    return "; ".join(msgs)


async def run(project_name: str, apply: bool) -> int:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row

    cur = await db.execute(
        "SELECT path FROM projects WHERE name = ?", (project_name,),
    )
    proj = await cur.fetchone()
    if not proj:
        print(f"[clean] project '{project_name}' not found")
        await db.close()
        return 1
    project_path = proj["path"]

    code, head_ref, _ = _git(["symbolic-ref", "HEAD"], project_path)
    if code != 0:
        print(f"[clean] detached HEAD in {project_path}; aborting")
        await db.close()
        return 1
    main_branch = head_ref.strip().removeprefix("refs/heads/")
    code, main_sha, _ = _git(["rev-parse", main_branch], project_path)
    main_sha = main_sha.strip()

    branches = _list_devfleet_branches(project_path)
    print(f"[clean] {project_name}: {len(branches)} devfleet/* branch(es). "
          f"target={main_branch} ({main_sha[:7]}). stale_threshold={STALE_THRESHOLD}\n")

    killed = 0
    spared = 0
    for branch in branches:
        short = branch.removeprefix("devfleet/")
        behind = _behind_count(project_path, branch, main_branch)
        if behind < STALE_THRESHOLD:
            spared += 1
            print(f"  SPARE   {short}  behind={behind}  (under threshold)")
            continue
        if _can_rebase_cleanly(project_path, branch, main_branch):
            spared += 1
            print(f"  SPARE   {short}  behind={behind}  (rebase succeeds — re-run merge script)")
            continue

        print(f"  KILL    {short}  behind={behind}  rebase-fails  →  stale_base_irrecoverable")
        killed += 1
        if not apply:
            continue

        # Update DB
        cur = await db.execute(
            "SELECT s.id AS sid, s.mission_id FROM agent_sessions s "
            "WHERE s.id LIKE ? LIMIT 1",
            (short + "%",),
        )
        row = await cur.fetchone()
        if row:
            now = datetime.now(timezone.utc).isoformat()
            note = (
                f"stale_base_irrecoverable {now}: branch {behind} commits behind "
                f"{main_branch} ({main_sha[:7]}); rebase onto current main failed "
                f"with semantic conflicts. Re-dispatch the mission against fresh main."
            )
            await db.execute(
                "UPDATE agent_sessions SET error_type='stale_base_irrecoverable', "
                "last_error=?, ended_at=COALESCE(ended_at, ?) WHERE id=?",
                (note, now, row["sid"]),
            )
            await db.commit()

        msg = _cleanup(project_path, branch)
        print(f"          {msg}")

    await db.close()
    print()
    print(f"[clean] {'APPLIED' if apply else 'DRY-RUN'}: killed={killed}, spared={spared}")
    if not apply and killed:
        print("[clean] re-run with --apply to commit changes")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project", required=True)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    sys.exit(asyncio.run(run(args.project, args.apply)))


if __name__ == "__main__":
    main()
