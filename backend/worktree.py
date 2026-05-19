"""
Git Worktree Isolation

Each agent session gets its own git worktree so it can't break the main codebase.
After the session, the worktree can be merged (if successful) or discarded.

Structure:
  project_root/
    .devfleet-worktrees/
      session-{id}/   (worktree checkout on branch devfleet/{id})
"""

import asyncio
import logging
import os
import shutil

log = logging.getLogger("devfleet.worktree")


async def _run(
    cmd: list[str], cwd: str, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    kwargs = dict(
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if env is not None:
        kwargs["env"] = env
    proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


async def is_git_repo(path: str) -> bool:
    code, _, _ = await _run(["git", "rev-parse", "--is-inside-work-tree"], path)
    return code == 0


async def create_worktree(
    project_path: str,
    session_id: str,
    mission: dict | None = None,
) -> tuple[str | None, str | None]:
    """Create an isolated git worktree. Returns (worktree_path, branch_name) or (None, None)."""
    if not await is_git_repo(project_path):
        log.info("Project %s is not a git repo — skipping worktree isolation", project_path)
        return None, None

    short_id = session_id[:8]
    if mission:
        lane = (mission.get("lane") or "").strip() or "coder"
        mission_num = mission.get("mission_number") or 0
        branch_name = f"devfleet/{lane}/{mission_num}-{short_id}"
    else:
        branch_name = f"devfleet/{short_id}"
    worktree_dir = os.path.join(project_path, ".devfleet-worktrees")
    worktree_path = os.path.join(worktree_dir, f"session-{short_id}")

    os.makedirs(worktree_dir, exist_ok=True)

    # Branch from origin/dev (not HEAD — avoids inheriting operator's feature branch)
    code, out, err = await _run(
        ["git", "worktree", "add", "-b", branch_name, worktree_path, "origin/dev"],
        project_path,
    )
    if code != 0:
        log.error("Failed to create worktree: %s", err)
        return None, None

    log.info("Created worktree at %s on branch %s", worktree_path, branch_name)

    # Pre-warm node_modules so the agent doesn't OOM during install inside the session.
    # pnpm install runs here (pre-agent) where a failure is cheap and clearly scoped.
    lock_file = os.path.join(worktree_path, "pnpm-lock.yaml")
    if os.path.exists(lock_file):
        pnpm_cmd = shutil.which("pnpm") or "pnpm"
        pnpm_env = {**os.environ, "NODE_OPTIONS": "--max-old-space-size=3072"}
        log.info("Pre-warming pnpm install in worktree %s…", worktree_path)
        pw_code, _, pw_err = await _run(
            [pnpm_cmd, "install", "--frozen-lockfile", "--prefer-offline"],
            worktree_path,
            env=pnpm_env,
        )
        if pw_code != 0:
            log.warning("pnpm pre-warm failed (exit %d): %s — agent will proceed anyway", pw_code, pw_err[:300])

    # Add .devfleet-worktrees to .gitignore if not already there
    gitignore_path = os.path.join(project_path, ".gitignore")
    marker = ".devfleet-worktrees"
    if os.path.exists(gitignore_path):
        with open(gitignore_path, "r") as f:
            if marker not in f.read():
                with open(gitignore_path, "a") as f2:
                    f2.write(f"\n{marker}/\n")
    else:
        with open(gitignore_path, "w") as f:
            f.write(f"{marker}/\n")

    return worktree_path, branch_name


async def cleanup_worktree(
    project_path: str,
    session_id: str,
    merge: bool = False,
    branch_name: str | None = None,
) -> bool:
    """Remove a worktree. Optionally safe-merge its branch first.

    Safe-merge protocol:
    1. Check for new commits on the branch.
    2. Attempt merge with --no-commit --no-ff (dry run).
    3. If conflicts detected → abort, preserve worktree, return False.
    4. If clean → complete the merge with a descriptive commit message.
    5. Verify no conflict markers leaked into tracked files.
    """
    short_id = session_id[:8]
    # Use provided branch_name (lane-aware naming) or fall back to legacy format
    if not branch_name:
        branch_name = f"devfleet/{short_id}"
    worktree_path = os.path.join(project_path, ".devfleet-worktrees", f"session-{short_id}")

    if merge:
        # Step 1: any commits to merge?
        code, out, _ = await _run(
            ["git", "log", f"HEAD..{branch_name}", "--oneline"],
            project_path,
        )
        if code == 0 and out.strip():
            # Step 2: dry-run merge — detect conflicts without committing
            code, _, err = await _run(
                ["git", "merge", "--no-commit", "--no-ff", branch_name],
                project_path,
            )

            # Step 3: check for unmerged paths (conflict markers)
            _, unmerged, _ = await _run(
                ["git", "diff", "--name-only", "--diff-filter=U"],
                project_path,
            )

            if unmerged.strip():
                # Conflicts — abort and preserve worktree for human/orchestrator resolution
                await _run(["git", "merge", "--abort"], project_path)
                log.warning(
                    "Safe-merge CONFLICT for session %s — conflicts in: %s. "
                    "Worktree preserved at %s for resolution.",
                    short_id, unmerged.strip().replace("\n", ", "), worktree_path,
                )
                return False

            # Step 4: also verify no stray conflict markers in any tracked file
            _, marker_files, _ = await _run(
                ["git", "grep", "-l", "<<<<<<", "--cached"],
                project_path,
            )
            if marker_files.strip():
                await _run(["git", "merge", "--abort"], project_path)
                log.warning(
                    "Safe-merge MARKER check failed for session %s — conflict markers in: %s.",
                    short_id, marker_files.strip().replace("\n", ", "),
                )
                return False

            # Step 5: clean — finalise the merge
            code, _, err = await _run(
                ["git", "commit", "--no-edit", "-m",
                 f"Farhanmerge(devfleet): integrate session {short_id}"],
                project_path,
            )
            if code != 0:
                log.warning("Merge commit failed for session %s: %s", short_id, err)
                await _run(["git", "merge", "--abort"], project_path)
                return False

            log.info("Safe-merge completed for session %s", short_id)

    # Remove worktree
    code, _, err = await _run(["git", "worktree", "remove", "--force", worktree_path], project_path)
    if code != 0:
        log.warning("Failed to remove worktree %s: %s", worktree_path, err)
        if os.path.exists(worktree_path):
            shutil.rmtree(worktree_path, ignore_errors=True)

    # Delete the branch
    await _run(["git", "branch", "-D", branch_name], project_path)

    log.info("Cleaned up worktree for session %s (merge=%s)", short_id, merge)
    return True
