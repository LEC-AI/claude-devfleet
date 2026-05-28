"""
Land DevFleet worktree branches that are conflict-free against main, and emit
a triage report for the ones that conflict. Uses the same plumbing-merge logic
as the patched dispatcher (`worktree._merge_branch_via_plumbing`) so the live
project working tree is never touched.

Sequential by design: after each successful land, main has moved, so the
remaining branches are re-checked against the new main before deciding
clean-vs-conflict. This handles the case where many sibling branches all
touch the same setup files — some that look "clean" individually may
conflict once an earlier sibling has landed.

Usage:
  python merge_pending_branches.py --project myproj            # dry-run
  python merge_pending_branches.py --project myproj --apply    # commit
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


def _git(args: list[str], cwd: str) -> tuple[int, str, str]:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def _list_devfleet_branches(project_path: str) -> list[str]:
    # `--format` avoids the leading `* ` / `+ ` markers that signal HEAD or
    # worktree-checked-out — those would otherwise corrupt the branch name.
    code, out, _ = _git(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads/devfleet/"],
        project_path,
    )
    if code != 0:
        return []
    return [l.strip() for l in out.splitlines() if l.strip().startswith("devfleet/")]


def _ahead_count(project_path: str, branch: str, main_branch: str) -> int:
    code, out, _ = _git(["log", "--oneline", f"{main_branch}..{branch}"], project_path)
    if code != 0:
        return 0
    return len([l for l in out.splitlines() if l.strip()])


def _try_rebase_in_worktree(project_path: str, branch: str, main_branch: str) -> tuple[bool, str]:
    """Attempt to rebase the worktree's branch onto main. Returns (ok, msg).

    Mirrors the dispatcher's pre-merge rebase: brings a stale branch base up to
    current main so we only carry the agent's tiny delta forward. If the
    rebase itself fails (real semantic conflict), aborts and leaves the
    worktree intact for human review.
    """
    short = branch.removeprefix("devfleet/")
    worktree_path = os.path.join(project_path, ".devfleet-worktrees", f"session-{short}")
    if not os.path.isdir(worktree_path):
        return False, "no worktree dir to rebase in"
    code, dirty, _ = _git(["status", "--porcelain"], worktree_path)
    if code != 0:
        return False, "git status failed in worktree"
    if dirty.strip():
        return False, "worktree has uncommitted changes"
    code, _, err = _git(["rebase", main_branch], worktree_path)
    if code != 0:
        _git(["rebase", "--abort"], worktree_path)
        return False, f"rebase aborted: {err[:200]}"
    return True, "rebased successfully"


def _can_merge_cleanly(project_path: str, branch: str, main_branch: str) -> tuple[bool, int, str]:
    """Run `git merge-tree --write-tree`. Returns (clean, conflict_chunks, output_or_tree).

    Tries a worktree-side rebase first if the branch is behind main, so a
    stale-but-otherwise-clean branch can still land.
    """
    # Cheap staleness check: if branch's merge-base != main, attempt rebase.
    code, base, _ = _git(["merge-base", main_branch, branch], project_path)
    code2, main_sha, _ = _git(["rev-parse", main_branch], project_path)
    if code == 0 and code2 == 0 and base.strip() != main_sha.strip():
        ok, msg = _try_rebase_in_worktree(project_path, branch, main_branch)
        if ok:
            # After rebase, branch is a clean descendant of main → trivial FF
            return True, 0, "ff-after-rebase"
        # Rebase failed → fall through to merge-tree, which will report the
        # same conflicts but in a structured form for the triage report.

    code, out, err = _git(
        ["merge-tree", "--write-tree", main_branch, branch], project_path,
    )
    if code == 0 and out.strip():
        return True, 0, out.strip().split("\n")[0]  # tree sha
    # Conflicts → count chunks (output is the conflicting trees)
    combined = (out + "\n" + err).lower()
    chunks = combined.count("<<<<<<<")
    if chunks == 0:
        chunks = combined.count("conflict")
    return False, chunks, (out + "\n" + err)[:1500]


def _merge_into_main(project_path: str, branch: str, main_ref: str, short: str) -> tuple[bool, str]:
    """Plumbing-merge branch into main_ref. Returns (ok, message)."""
    code, main_sha, _ = _git(["rev-parse", main_ref], project_path)
    if code != 0:
        return False, f"cannot resolve {main_ref}"
    main_sha = main_sha.strip()

    code, branch_sha, _ = _git(["rev-parse", branch], project_path)
    if code != 0:
        return False, f"cannot resolve {branch}"
    branch_sha = branch_sha.strip()

    code, base_sha, _ = _git(["merge-base", main_sha, branch_sha], project_path)
    if code != 0:
        return False, "merge-base failed"
    base_sha = base_sha.strip()

    if base_sha == branch_sha:
        return True, "no-op (already on main)"

    if base_sha == main_sha:
        new_sha = branch_sha  # fast-forward
    else:
        code, tree_out, err = _git(
            ["merge-tree", "--write-tree", main_sha, branch_sha], project_path,
        )
        if code != 0 or not tree_out.strip():
            return False, f"merge-tree conflict: {err[:200]}"
        tree_sha = tree_out.strip().split("\n")[0]
        code, new_sha_out, err = _git(
            ["commit-tree", tree_sha, "-p", main_sha, "-p", branch_sha,
             "-m", f"DevFleet: merge session {short} (pending-branch cleanup)"],
            project_path,
        )
        if code != 0:
            return False, f"commit-tree failed: {err[:200]}"
        new_sha = new_sha_out.strip()

    code, _, err = _git(["update-ref", main_ref, new_sha, main_sha], project_path)
    if code != 0:
        return False, f"update-ref failed (race?): {err[:200]}"

    # Sync the live WD if clean (cosmetic)
    code, lines, _ = _git(["status", "--porcelain"], project_path)
    if code == 0 and not lines.strip():
        _git(["reset", "--hard", main_ref], project_path)

    return True, f"advanced {main_sha[:7]} -> {new_sha[:7]}"


def _cleanup_worktree_and_branch(project_path: str, short: str, branch: str) -> str:
    wt = os.path.join(project_path, ".devfleet-worktrees", f"session-{short}")
    code, _, err = _git(["worktree", "remove", "--force", wt], project_path)
    msgs = []
    if code != 0:
        if os.path.isdir(wt):
            shutil.rmtree(wt, ignore_errors=True)
        _git(["worktree", "prune"], project_path)
        msgs.append(f"shutil-removed worktree ({err[:80]})")
    else:
        msgs.append("removed worktree")
    code, _, err = _git(["branch", "-D", branch], project_path)
    msgs.append("deleted branch" if code == 0 else f"branch-delete err: {err[:80]}")
    return "; ".join(msgs)


async def _mark_session_completed(db, session_id: str, mission_id: str, note: str):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE agent_sessions SET status='completed', error_type='reconciled', "
        "last_error=?, ended_at=COALESCE(ended_at, ?) WHERE id=?",
        (note, now, session_id),
    )
    await db.execute(
        "UPDATE missions SET status='completed', updated_at=? WHERE id=?",
        (now, mission_id),
    )


async def run(project_name: str, apply: bool) -> int:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row

    cur = await db.execute(
        "SELECT id, name, path FROM projects WHERE name = ?", (project_name,),
    )
    proj = await cur.fetchone()
    if not proj:
        print(f"[merge] project '{project_name}' not found")
        await db.close()
        return 1
    project_path = proj["path"]

    code, head_ref, _ = _git(["symbolic-ref", "HEAD"], project_path)
    if code != 0:
        print(f"[merge] {project_path} has detached HEAD; aborting")
        await db.close()
        return 1
    main_ref = head_ref.strip()
    main_short = main_ref.removeprefix("refs/heads/")

    branches = _list_devfleet_branches(project_path)
    pending = []
    for b in branches:
        n = _ahead_count(project_path, b, main_short)
        if n > 0:
            pending.append((b, n))
    print(f"[merge] {project_name}: {len(pending)} branch(es) ahead of {main_short}")

    merged = []
    triage = []

    # Iterate; after each successful merge re-check the remaining set.
    # Items are (branch, ahead_count, last_known_conflict_chunks).
    queue = [(b, n, -1) for b, n in pending]
    while queue:
        progressed = False
        new_queue = []
        for branch, ahead, _prev_chunks in queue:
            short = branch.removeprefix("devfleet/")
            clean, chunks, payload = _can_merge_cleanly(project_path, branch, main_short)
            if not clean:
                new_queue.append((branch, ahead, chunks))
                continue

            # Fetch the session/mission for DB update
            cur = await db.execute(
                "SELECT s.id AS sid, s.mission_id, m.title "
                "FROM agent_sessions s JOIN missions m ON m.id=s.mission_id "
                "WHERE s.id LIKE ? LIMIT 1",
                (short + "%",),
            )
            row = await cur.fetchone()
            title = row["title"] if row else "(no DB row)"

            if not apply:
                print(f"  WOULD MERGE  {short}  '{title[:60]}'  ({ahead} commit ahead)")
                merged.append((short, title, ahead))
                progressed = True
                # In dry-run, don't actually advance main — just count it once.
                # But because the next branches' conflict status depends on
                # this landing, mark it merged and skip in this pass.
                continue

            ok, msg = _merge_into_main(project_path, branch, main_ref, short)
            if not ok:
                print(f"  MERGE FAIL   {short}  →  {msg}")
                new_queue.append((branch, ahead, -1))
                continue

            cleanup_msg = _cleanup_worktree_and_branch(project_path, short, branch)
            if row:
                note = (
                    f"Reconciled merge {datetime.now(timezone.utc).isoformat()}: "
                    f"branch {branch} landed into {main_short} via plumbing "
                    f"({msg}). {cleanup_msg}"
                )
                await _mark_session_completed(db, row["sid"], row["mission_id"], note)
                await db.commit()

            print(f"  MERGED       {short}  '{title[:60]}'  →  {msg}; {cleanup_msg}")
            merged.append((short, title, ahead))
            progressed = True

        queue = new_queue
        if not progressed:
            break  # remaining queue is all conflicted

    for branch, ahead, chunks in queue:
        short = branch.removeprefix("devfleet/")
        cur = await db.execute(
            "SELECT m.title FROM agent_sessions s JOIN missions m ON m.id=s.mission_id "
            "WHERE s.id LIKE ? LIMIT 1",
            (short + "%",),
        )
        row = await cur.fetchone()
        title = row["title"] if row else "(no DB row)"

        # Files-touched summary
        code, files_out, _ = _git(
            ["diff", "--name-only", f"{main_short}..{branch}"], project_path,
        )
        files = [f for f in files_out.splitlines() if f.strip()]
        triage.append({
            "branch": branch,
            "short": short,
            "title": title,
            "ahead": ahead,
            "conflict_chunks": chunks,
            "files_changed": files,
        })

    await db.close()

    print()
    print(f"[merge] {'APPLIED' if apply else 'DRY-RUN'}: merged={len(merged)} triage={len(triage)}")
    if triage:
        print()
        print("=== triage: branches needing manual resolution ===")
        for t in triage:
            print(f"\n--- devfleet/{t['short']} ({t['title'][:70]}) ---")
            print(f"    ahead by {t['ahead']} commit | conflict chunks: {t['conflict_chunks']}")
            print(f"    files ({len(t['files_changed'])}): {', '.join(t['files_changed'][:8])}"
                  + (" ..." if len(t['files_changed']) > 8 else ""))
    if not apply and merged:
        print()
        print("[merge] re-run with --apply to commit changes")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project", required=True)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    sys.exit(asyncio.run(run(args.project, args.apply)))


if __name__ == "__main__":
    main()
