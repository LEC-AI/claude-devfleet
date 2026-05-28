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


async def _run(cmd: list[str], cwd: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


async def is_git_repo(path: str) -> bool:
    code, _, _ = await _run(["git", "rev-parse", "--is-inside-work-tree"], path)
    return code == 0


async def create_worktree(project_path: str, session_id: str) -> str | None:
    """Create an isolated git worktree for this session. Returns worktree path or None if not a git repo."""
    if not await is_git_repo(project_path):
        log.info("Project %s is not a git repo — skipping worktree isolation", project_path)
        return None

    short_id = session_id[:8]
    branch_name = f"devfleet/{short_id}"
    worktree_dir = os.path.join(project_path, ".devfleet-worktrees")
    worktree_path = os.path.join(worktree_dir, f"session-{short_id}")

    os.makedirs(worktree_dir, exist_ok=True)

    # Create worktree on a new branch from HEAD
    code, out, err = await _run(
        ["git", "worktree", "add", "-b", branch_name, worktree_path, "HEAD"],
        project_path,
    )
    if code != 0:
        log.error("Failed to create worktree: %s", err)
        return None

    log.info("Created worktree at %s on branch %s", worktree_path, branch_name)

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

    return worktree_path


async def cleanup_worktree(project_path: str, session_id: str, merge: bool = False):
    """Remove a worktree. Optionally merge its branch first.

    Merge is done via git plumbing (`merge-tree` + `commit-tree` + `update-ref`)
    so the project's live working tree is never touched. This avoids the failure
    mode where a dirty working tree in `project_path` (from parallel sessions or
    any background process that writes to the same checkout) caused `git merge`
    to refuse, leaving real work stranded on a branch that someone then had to
    merge by hand.
    """
    short_id = session_id[:8]
    branch_name = f"devfleet/{short_id}"
    worktree_path = os.path.join(project_path, ".devfleet-worktrees", f"session-{short_id}")

    if merge:
        # auto-commit-pre-merge: agents rarely git-commit themselves, so any
        # uncommitted edits on the worktree would be lost when we remove it.
        code, status, _ = await _run(["git", "status", "--porcelain"], worktree_path)
        if code == 0 and status.strip():
            await _run(["git", "add", "-A"], worktree_path)
            await _run(
                ["git", "commit", "-m", f"DevFleet: auto-commit session {short_id}"],
                worktree_path,
            )
            log.info("auto-committed uncommitted changes for session %s", short_id)

        merged_ok = await _merge_branch_via_plumbing(project_path, branch_name, short_id)
        if not merged_ok:
            # Worktree + branch preserved so a human / recover_mission can salvage.
            return False

    # Remove worktree
    code, _, err = await _run(["git", "worktree", "remove", "--force", worktree_path], project_path)
    if code != 0:
        log.warning("Failed to remove worktree %s: %s", worktree_path, err)
        # Fallback: just delete the dir
        if os.path.exists(worktree_path):
            shutil.rmtree(worktree_path, ignore_errors=True)

    # Delete the branch
    await _run(["git", "branch", "-D", branch_name], project_path)

    log.info("Cleaned up worktree for session %s (merge=%s)", short_id, merge)
    return True


async def _merge_branch_via_plumbing(project_path: str, branch_name: str, short_id: str) -> bool:
    """Advance the project's HEAD ref to include `branch_name` without touching
    the working tree. Returns True on success (including no-op), False if the
    branch couldn't be merged cleanly — caller should preserve the worktree.
    """
    # Fetch latest from origin so we merge against the real remote HEAD,
    # not a stale local cache.
    code, _, fetch_err = await _run(["git", "fetch", "origin"], project_path)
    if code != 0:
        log.warning("session %s: fetch origin failed, proceeding with local refs: %s",
                    short_id, fetch_err[:200])

    # Resolve target ref (usually refs/heads/main, sometimes master)
    code, head_ref, _ = await _run(["git", "symbolic-ref", "HEAD"], project_path)
    if code != 0 or not head_ref.strip():
        log.warning(
            "session %s: HEAD is detached or symbolic-ref failed; skipping ref advance",
            short_id,
        )
        return False
    target_ref = head_ref.strip()

    # Fast-forward local ref to origin's latest before merging the branch,
    # so we don't create divergent histories.
    short_ref = target_ref.removeprefix("refs/heads/")
    code, origin_sha, _ = await _run(
        ["git", "rev-parse", f"origin/{short_ref}"], project_path,
    )
    if code == 0 and origin_sha.strip():
        code_local, local_sha, _ = await _run(
            ["git", "rev-parse", target_ref], project_path,
        )
        if code_local == 0 and local_sha.strip() != origin_sha.strip():
            code_base, ff_base, _ = await _run(
                ["git", "merge-base", local_sha.strip(), origin_sha.strip()],
                project_path,
            )
            if code_base == 0 and ff_base.strip() == local_sha.strip():
                await _run(
                    ["git", "update-ref", target_ref, origin_sha.strip(), local_sha.strip()],
                    project_path,
                )
                log.info("session %s: fast-forwarded %s to origin/%s",
                         short_id, target_ref, short_ref)

    code, branch_sha, _ = await _run(["git", "rev-parse", branch_name], project_path)
    if code != 0:
        log.warning("session %s: cannot resolve %s", short_id, branch_name)
        return False
    branch_sha = branch_sha.strip()

    code, main_sha, _ = await _run(["git", "rev-parse", target_ref], project_path)
    if code != 0:
        log.warning("session %s: cannot resolve %s", short_id, target_ref)
        return False
    main_sha = main_sha.strip()

    # Already up to date?
    if branch_sha == main_sha:
        return True

    # ── Pre-merge rebase: bring the branch base up to current target_ref. ──
    # Without this, a session that finishes long after main has moved (or that
    # was dispatched in a burst, where many missions share the same stale base)
    # produces spurious conflicts against every commit main has gained since
    # the agent began. The symptom: 1 commit "ahead" of main on the branch, but
    # tens of commits behind in base — every overlapping file looks like a
    # conflict when it's really just stale-base drift. Rebasing first means we
    # only merge the agent's actual delta on top of current main.
    branch_short = branch_name.removeprefix("refs/heads/")
    short_in_path = branch_short.removeprefix("devfleet/")
    worktree_path = os.path.join(
        project_path, ".devfleet-worktrees", f"session-{short_in_path}",
    )
    if os.path.isdir(worktree_path):
        # Worktree's WD must be clean before rebasing — we don't want to lose
        # uncommitted edits (the caller has already auto-committed in
        # cleanup_worktree, so this is usually clean).
        code, dirty, _ = await _run(
            ["git", "status", "--porcelain"], worktree_path,
        )
        if code == 0 and not dirty.strip():
            code, current_base, _ = await _run(
                ["git", "merge-base", target_ref, "HEAD"], worktree_path,
            )
            if code == 0 and current_base.strip() != main_sha:
                behind = "?"
                code_b, behind_out, _ = await _run(
                    ["git", "rev-list", "--count", f"{current_base.strip()}..{main_sha}"],
                    project_path,
                )
                if code_b == 0:
                    behind = behind_out.strip()
                code, _, rb_err = await _run(
                    ["git", "rebase", target_ref], worktree_path,
                )
                if code != 0:
                    # Stale-base conflict — abort and preserve worktree for
                    # human review or re-dispatch. This is the right failure
                    # mode: the agent's work cannot mechanically reconcile
                    # with current main and needs explicit attention.
                    await _run(["git", "rebase", "--abort"], worktree_path)
                    log.warning(
                        "session %s: STALE_BASE_CONFLICT — rebase onto %s failed "
                        "(branch was %s commits behind). Worktree preserved at %s. "
                        "Recommend re-dispatch on fresh main. rebase_err=%s",
                        short_id, target_ref, behind, worktree_path, rb_err[:300],
                    )
                    return False
                log.info(
                    "session %s: rebased %s onto current %s (was %s commits behind)",
                    short_id, branch_name, target_ref, behind,
                )
                # Re-resolve branch_sha — rebase rewrote commits, new SHAs
                code, branch_sha_new, _ = await _run(
                    ["git", "rev-parse", branch_name], project_path,
                )
                if code == 0 and branch_sha_new.strip():
                    branch_sha = branch_sha_new.strip()
        else:
            log.info(
                "session %s: worktree has uncommitted changes, skipping pre-merge rebase",
                short_id,
            )

    # Find merge base to decide FF vs real-merge
    code, base_sha, _ = await _run(
        ["git", "merge-base", main_sha, branch_sha], project_path,
    )
    if code != 0:
        log.warning("session %s: merge-base failed", short_id)
        return False
    base_sha = base_sha.strip()

    if base_sha == branch_sha:
        # Branch is behind main, nothing new — no-op
        return True

    if base_sha == main_sha:
        # Fast-forward: branch is a pure descendant of main
        new_sha = branch_sha
    else:
        # Real merge needed — do it via plumbing so the live WD isn't touched.
        # `merge-tree --write-tree` writes the merged tree to the object DB and
        # prints its SHA; exits non-zero on conflict.
        code, tree_out, mt_err = await _run(
            ["git", "merge-tree", "--write-tree", main_sha, branch_sha],
            project_path,
        )
        if code != 0 or not tree_out.strip():
            log.warning(
                "session %s: merge-tree conflict vs %s. Worktree + branch preserved. %s",
                short_id, target_ref, mt_err[:200],
            )
            return False
        tree_sha = tree_out.strip().split("\n")[0]

        commit_msg = f"DevFleet: merge session {short_id}"
        code, new_sha_out, cc_err = await _run(
            ["git", "commit-tree", tree_sha, "-p", main_sha, "-p", branch_sha,
             "-m", commit_msg],
            project_path,
        )
        if code != 0 or not new_sha_out.strip():
            log.warning("session %s: commit-tree failed: %s", short_id, cc_err[:200])
            return False
        new_sha = new_sha_out.strip()

    # CAS the ref forward. If someone else advanced main concurrently, this fails
    # and we preserve the worktree for retry.
    code, _, upd_err = await _run(
        ["git", "update-ref", target_ref, new_sha, main_sha],
        project_path,
    )
    if code != 0:
        log.warning(
            "session %s: update-ref %s failed (lost race or stale ref): %s",
            short_id, target_ref, upd_err[:200],
        )
        return False

    log.info("session %s: advanced %s %s -> %s via plumbing",
             short_id, target_ref, main_sha[:7], new_sha[:7])

    # Push merged commits to origin so the work actually lands on the remote.
    # Without this, local main advances but origin/main stays stale — the root
    # cause of the 85% orphan rate.
    short_ref = target_ref.removeprefix("refs/heads/")
    code, _, push_err = await _run(
        ["git", "push", "origin", short_ref], project_path,
    )
    if code != 0:
        log.warning("session %s: push to origin/%s failed: %s",
                    short_id, short_ref, push_err[:200])

    # Best-effort: if the project's working tree is clean, sync index+WD to the
    # new ref. If it's dirty, we leave it — the ref still advanced, and whoever
    # owns those uncommitted edits can deal with them.
    code, status_lines, _ = await _run(["git", "status", "--porcelain"], project_path)
    if code == 0 and not status_lines.strip():
        await _run(["git", "reset", "--hard", target_ref], project_path)
        log.info("session %s: synced clean project WD to %s", short_id, target_ref)
    else:
        log.info(
            "session %s: project WD has uncommitted changes; %s advanced, WD untouched",
            short_id, target_ref,
        )
    return True
