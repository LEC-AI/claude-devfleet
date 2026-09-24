# PRD — Swarm Observability UI

**Status:** Draft v1 · **Date:** 2026-09-24
**Context:** one piece of a larger "swarm + nightly goal-loop" effort: a swarm planner fans a goal out into a tree of missions; a nightly cost roll-up summarises what ran. This document covers only the observability view.

---

## 1. Problem

DevFleet can already fan a goal out into a tree of missions (`parent_mission_id` + `depends_on` +
`auto_dispatch`, dispatched by `mission_watcher.py`). A planned swarm planner will make that tree a first-class
"swarm" that runs unattended overnight. When an operator arrives in the morning there is **no way to
see what the swarm did** — which missions ran, which are blocked, which failed, what each one cost —
without querying SQLite by hand.

Today's UI shows a mission's *direct* children on `MissionDetail`, with no status roll-up, no cost,
no dependency/blocked state, no auto-refresh, and no view that starts from "the swarm" rather than
one mission.

**Evidence:** operators today have to query the DB by hand to see what a swarm did overnight. The existing
`GET /api/missions/{id}` returns `children` as `id/title/status/mission_type` only.

## 2. Users and job to be done

- **Operator / engineer (primary):** "Show me, in one screen, the tree of everything this swarm
  spawned, what state each piece is in, why the blocked ones are blocked, and what it has cost —
  and keep it current while the swarm is still running."
- **Integrator:** "Wire this in with one line and trust it doesn't change any existing behaviour."
- **Adjacent features (swarm planner, nightly cost roll-up):** consume the same `swarm_root`
  tag convention; nothing here changes it.

## 3. Success criteria

| Metric | Target |
|---|---|
| Time to answer "what did the swarm do overnight" | One click from the sidebar, no DB access |
| Freshness while a swarm is live | Status changes visible within one poll interval (≤ 10 s) with no manual refresh |
| Correctness of linkage | Every mission returned has a `parent_mission_id` chain back to the requested root |
| Regression risk | Zero changes to existing route behaviour, schema, or dispatch logic |

Failure looks like: a page that only works once the swarm planner exists, a tree that misses agent-spawned
sub-sub-missions, or a poll that never stops and hammers the API after the swarm finishes.

## 4. Scope

### In scope
- Read-only backend endpoint returning the swarm tree with per-node status, dependencies,
  blocked-on state, and latest-session cost/tokens.
- Read-only backend endpoint listing swarm roots (entry point for the UI).
- Frontend page rendering the tree as an indented, collapsible, status-colour-coded list with a
  roll-up summary, polling while any mission is non-terminal.
- Sidebar entry and page route so the page is reachable; a "View swarm" link from a swarm-root
  mission's detail page.
- A runnable, framework-free check (`python3 backend/test_swarm_tree.py`).

### Out of scope (explicitly)
- Creating, dispatching, cancelling, or editing missions from this page (the swarm planner owns creation;
  existing `MissionDetail` owns per-mission actions).
- Any new DB table or column. This track is a pure consumer of `missions` and `agent_sessions`.
- Wiring `include_router` into `backend/app.py` — kept as a one-line integration step so parallel
  feature branches don't conflict. The PR documents the exact line.
- Graph/DAG visualisation (Mermaid, canvas). A tree list covers the need; a DAG view is a
  candidate follow-up.
- Nightly cost roll-up across swarms (separate feature).
- WebSocket/SSE push. Polling is sufficient for a 5–10 s freshness target and matches existing
  pages (`Sidebar`, `Dashboard`).

## 5. Requirements

Priority: **Must** / **Should** / **Won't (this version)**.

### Backend — `backend/routes_swarm_tree.py`

| ID | Requirement | Priority |
|---|---|---|
| R1 | `GET /api/swarms/{id}/tree` returns `404` when no mission with that id exists. | Must |
| R2 | The response contains `root` (the requested mission) and `missions`: every mission whose `parent_mission_id` chain leads to the root, at any depth, in a stable order (depth, then `created_at`, then `id`). Depth is capped (default 20) to guarantee termination on corrupt data. | Must |
| R3 | Each node carries: `id, title, status, mission_type, priority, parent_mission_id, depth, depends_on (parsed list), blocked_on (subset of depends_on not yet completed), auto_dispatch, tags (parsed list), created_at, updated_at, latest_session` where `latest_session` is `null` or `{id, status, model, started_at, ended_at, total_cost_usd, total_tokens}` from the most recently started `agent_sessions` row for that mission. | Must |
| R4 | The response contains `summary`: `counts` keyed by status (`draft/running/completed/failed/cancelled` always present, plus any other status seen), `total` (child count, root excluded), `total_cost_usd` and `total_tokens` summed over each child's latest session, and `is_active` = any child in a non-terminal status. Terminal statuses are `completed`, `failed`, `cancelled`. | Must |
| R5 | The response reports `is_swarm_root` (root's `tags` contains `"swarm_root"`). A root without the tag is still served — the tree logic is generic — so the page works today on hand-built or agent-built sub-mission trees. | Must |
| R6 | `GET /api/swarms?project_id=` lists missions tagged `swarm_root` (newest first) with the same `summary` roll-up per swarm. Optional `project_id` filter. | Should |
| R7 | All queries are plain `SELECT`s against existing tables; the module writes nothing, imports nothing from `autoloop`, `mission_watcher`, `scheduler`, `sdk_engine`, and adds no schema. | Must |
| R8 | The router is defined with `prefix="/api"` so integration is exactly `app.include_router(routes_swarm_tree.router)`. | Must |
| R9 | JSON parsing of `tags` / `depends_on` tolerates `NULL`, empty string, and malformed JSON (treated as `[]`) so a single bad row can't 500 the whole tree. | Must |
| R10 | `blocked_on` is computed only for missions in `draft` status; for other statuses it is `[]`. | Should |

### Frontend — `frontend/src/pages/SwarmView.jsx`

| ID | Requirement | Priority |
|---|---|---|
| R11 | Page follows repo conventions: `.jsx`, `navigate(page, id)` props, existing CSS classes/variables, API helpers added to `frontend/src/api/client.js`. | Must |
| R12 | With no id selected, the page lists swarms from R6 with per-swarm counts; clicking one opens its tree. Empty state explains that a swarm is any mission tagged `swarm_root`. | Should |
| R13 | With an id, the page renders the root and an indented, collapsible tree of descendants. Each row shows title, status badge, mission type, blocked-on count (draft only), latest-session cost and tokens, and is clickable to `MissionDetail`. | Must |
| R14 | Rows are colour-coded by status using existing tokens (`--success` completed, `--warning` running, `--danger` failed, `--border` draft, muted for cancelled). | Must |
| R15 | A summary strip shows total / running / completed / failed / blocked counts and total cost. | Must |
| R16 | The page polls the tree endpoint every 5 s while `summary.is_active` is true and stops polling when it becomes false; a manual Refresh button always exists. Polling stops on unmount. | Must |
| R17 | A poll error is shown inline without blanking the last good tree. | Should |
| R18 | Collapse/expand state survives polls (keyed by mission id). | Should |
| R19 | Sidebar gains a "Swarms" entry; `App.jsx` gains `swarms` (list) and `swarm` (detail) cases. | Must |
| R20 | `MissionDetail` shows a "View swarm tree" button when the mission's tags include `swarm_root`. | Should |
| R21 | A "Show terminal" toggle to hide completed/cancelled rows in large swarms. | Won't (follow-up) |

### Verification

| ID | Requirement | Priority |
|---|---|---|
| R22 | `backend/test_swarm_tree.py` runs with `python3`, uses a throw-away SQLite file, seeds a root with 3 children (2 running, 1 draft blocked on the other two) plus one grandchild, and asserts: 404 on unknown id; 4+1 rows returned with correct parent linkage and depth; `blocked_on` equals the two running ids; latest-session cost picked over an older session; summary counts and cost sum; `is_active` true; list endpoint returns the root; malformed `tags` JSON doesn't raise. | Must |
| R23 | `npm run build` succeeds in `frontend/`. | Must |
| R24 | Manual end-to-end check: run the API with the router temporarily included, seed rows, open the page, flip a child to `completed` via SQL, observe the row change colour within one poll without a refresh. | Must |

## 6. Design notes (solution, separable from 1–3)

- **Recursive descent, not just direct children.** The minimal version would return only missions with matching
  `parent_mission_id`. Agents spawn sub-missions mid-flight via `create_sub_mission`, which point
  at the *agent's* mission, not the root. A one-level query would silently hide those. A recursive
  CTE (`WITH RECURSIVE`, SQLite ≥ 3.8.3, already available via aiosqlite) returns the whole
  subtree in one round trip; the depth cap prevents runaway on cyclic data.
  > **Assumption:** including grandchildren is what the operator wants. It is a superset of the
  > minimal behaviour and costs nothing to remove.
- **`blocked_on` is derived, not stored.** It is `depends_on` minus the ids whose status is
  `completed` — the same rule `mission_watcher` uses to decide dispatch eligibility. That lets the
  UI say *why* a draft is waiting.
- **No app.py edit.** `include_router` is left for integration so parallel feature branches stay
  conflict-free. The test file mounts the router on its own `FastAPI()` instance so the
  endpoint is fully exercised without touching `app.py`.
- **Frontend route naming.** The repo uses a page-switch in `App.jsx` rather than a router
  library, so `SwarmView.jsx` is the page file and `swarm`/`swarms` are the page
  keys. `.jsx` not `.tsx`: the project has no TypeScript.

## 7. Assumptions flagged

> **Assumption:** the `swarm_root` tag lives in `missions.tags` as a JSON array string, e.g.
> `'["swarm_root"]'` — matches the swarm planner's contract and the existing `tag` filter in
> `GET /api/missions`.

> **Assumption:** "latest session" means the most recent `started_at` (ties broken by rowid
> order), not the highest-cost or the only-completed session. This matches how `MissionDetail`
> orders sessions.

> **Assumption:** 5 s poll interval (the lower bound of the 5–10 s target) is acceptable API
> load; it equals the existing sidebar poll.

> **Assumption:** the swarm list endpoint (R6, R12) is welcome even though the original ask didn't include
> it. Without it the page has no entry point until the swarm planner ships a `POST /swarms` UI.

## 8. Open questions

| # | Question | Owner | Blocks |
|---|---|---|---|
| Q1 | Should the swarm planner's `GET /swarms/{id}` status roll-up and this `GET /swarms/{id}/tree` share one `summary` shape? This PR defines one; the planner could reuse it. | Swarm planner owner | Nothing now; avoids two roll-up formats at integration |
| Q2 | Cost attribution: should a mission's cost be its *latest* session or the *sum* of all its sessions (retries)? This PR exposes latest per node and sums latest across the tree. | Maintainers | Nightly roll-up consistency |
| Q3 | Once the swarm planner lands, should the swarm list page also offer "Launch swarm"? | Maintainers | UI follow-up only |
| Q4 | Is a hard depth cap of 20 acceptable, or should the endpoint also expose `truncated: true` when hit? (It does expose it.) | Integrator | None |

## 9. Rollout

Additive only. Ships dark until the single `include_router` line is added; the frontend
page renders an explanatory error until then. No migration, no env var, no feature flag needed
because nothing existing changes.
