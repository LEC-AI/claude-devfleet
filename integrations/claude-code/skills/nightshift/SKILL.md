---
name: nightshift
description: Turn new requirements into a planned build list that DevFleet agents execute unattended overnight (default 20:00–08:00) on spare Claude quota, auto-resuming after rate limits, landing work on a `nightshift` review branch. Use when the user wants to queue work for tonight, "night shift", "build this overnight", "add to the night build list", or /nightshift. `/nightshift report` shows what the night produced and how to merge it.
---

# Night shift

DevFleet does the execution. API base: `$DEVFLEET_API_URL`, default `http://localhost:18801` (called `$API` below). This skill does the part agents can't do alone at 2am: understand the project, close every ambiguity with the user NOW, and write missions precise enough to finish without a human.

Mechanics already in DevFleet (don't reimplement):
- Missions tagged `night` with `auto_dispatch: true` only dispatch inside the night window (`DEVFLEET_NIGHT_START`/`DEVFLEET_NIGHT_END` in `DEVFLEET_NIGHT_TZ`, default 20:00–08:00 Europe/London; set in the DevFleet checkout's `.env`).
- A mission killed by a quota or transient error auto-resumes its own Claude session every 20 min (max 30 tries; `DEVFLEET_NIGHT_RESUME_GAP_MIN`, `DEVFLEET_NIGHT_RESUME_MAX`) while the window is open. A 5-hour quota block running out costs a pause, not the mission.
- Successful night missions fast-forward a per-repo `nightshift` branch. `main` is never touched. The user reviews and merges in the morning.

## Mode 1: queue work (default)

1. **Project.** Use the repo of the current directory, else ask. Map it to a DevFleet project: `curl -s $API/api/projects` (match on `path`). Not registered → offer to register it, then `curl -s -X POST $API/api/projects -H 'content-type: application/json' -d '{"name": "<repo dir name>", "path": "<absolute repo path>"}'`. Must be a git repo with at least one commit (night work lands on a branch). If the API doesn't answer, tell the user to run `./start.sh` in their claude-devfleet checkout.
2. **Load context before asking anything.** Read the repo CLAUDE.md, HANDOFF.md / docs/status, `git log --oneline -20`, and the code the requirement touches. Check `git log main..nightshift` too: unmerged night work is the base tonight's missions build on.
3. **Interview.** List every decision the agents would otherwise guess: scope edges, behaviour on edge cases, data/API choices, what "done" looks like, what must not change. Ask them with AskUserQuestion (≤4 per round, recommended option first, at most 2 rounds). Don't ask what the code or CLAUDE.md answers. Anything still open becomes an explicit assumption in the mission text.
4. **Plan.** Split into missions an agent can finish in one session (roughly one feature or one module each). Each mission's `detailed_prompt` must stand alone:
   - Goal and why, in two lines.
   - Exact files/modules to touch and existing helpers to reuse (paths).
   - Decisions taken in the interview, and the assumptions.
   - Out of scope: what not to touch.
   - Night rules: no preview server; no questions (decide, record the assumption in the report); commit with clear messages; run the repo's tests and the repo's review subagent if one exists; never push, never deploy, never touch crons, secrets, or anything outside the repo; update HANDOFF/session log per repo convention.
   - `acceptance_criteria`: runnable checks (a test name, a command and its expected output), not prose.
5. **Show the plan** as a numbered list (title, one-line goal, acceptance). Get a yes before queueing.
6. **Queue** in order, chaining each to the previous with `depends_on` (DevFleet runs one night mission per repo at a time, so the review branch only ever fast-forwards; the chain fixes the order):

   ```bash
   curl -s -X POST $API/api/missions -H 'content-type: application/json' -d @mission.json
   # mission.json: {"project_id": "...", "title": "...", "detailed_prompt": "...",
   #   "acceptance_criteria": "...", "tags": ["night"], "auto_dispatch": true,
   #   "depends_on": ["<previous mission id>"], "mission_type": "implement", "priority": 0}
   ```
   Build the JSON with python `json.dump`, never by hand-escaping. Write them to a temp dir, not the repo.
7. **Confirm** with the mission ids/numbers and the window start time. If it's already inside the window, say it starts within seconds.

## Mode 2: `/nightshift report`

For each project with night missions updated since the last window opened (`curl -s "$API/api/missions?tag=night"`):
- Status per mission; for failures, `error_type` and `last_error` from the latest session (`GET /api/missions/{id}`).
- Each mission's report (`what_done`, `what_untested`, `errors_encountered`).
- `git -C <repo> log --oneline main..nightshift` and `git diff --stat main...nightshift`.
- Verdict per repo: merge, merge after a named fix, or redo. Merging is the user's call:
  `git -C <repo> merge --ff-only nightshift || git -C <repo> merge nightshift`, then `git branch -d nightshift`.

Stuck mission: `curl -s -X POST $API/api/missions/<id>/resume`. Kill a running one: `POST /api/sessions/<session id>/cancel` (session id from `GET /api/missions/<id>`).
