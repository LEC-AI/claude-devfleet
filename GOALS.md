# Persistent goals — Track 3

Goals now live in SQLite's `goals_registry`, so their text, status, iteration
count, and stopping reason survive process restarts. This is the persistence
layer for the swarm and auto-loop integration. It does not start agents or
resume execution automatically.

## API

The router is mounted in `app.py` using the existing `/api` convention.

| Request | Body | Response |
| --- | --- | --- |
| `POST /api/projects/{id}/goals` | `{"goal_text":"Add JWT auth","max_iterations":20}` | 201, full goal record |
| `GET /api/projects/{id}/goals` | None | 200, array of all project goals, newest first |
| `PATCH /api/goals/{id}` | `{"status":"paused","reason":"Waiting for review"}` | 200, updated goal record |

PATCH accepts `paused` (pause), `active` (resume), or `stopped` (stop). Completion
is recorded through the Python helper by the future orchestrator. A status
change currently updates only the registry; it does not control running agents
or the existing auto-loop. Unknown projects/goals return 404; invalid bodies
return 422. Goal text is trimmed and must be nonblank. `max_iterations` defaults
to 20 and must be a positive integer. Unknown request fields are rejected.

Record shape:

```json
{
  "id": "goal-uuid",
  "project_id": "project-uuid",
  "goal_text": "Add JWT auth",
  "status": "active",
  "max_iterations": 20,
  "current_iteration": 0,
  "created_at": "2026-09-24T12:00:00+00:00",
  "updated_at": "2026-09-24T12:00:00+00:00",
  "stopped_reason": null
}
```

## Python contract

Import from `backend/goals.py` using the repo's backend module path:

```python
from goals import create_goal, get_active_goal, increment_iteration, update_goal_status

goal_id = await create_goal(project_id, "Add JWT auth", max_iterations=20)
await increment_iteration(goal_id)
goal = await get_active_goal(project_id)
await update_goal_status(goal_id, "paused", reason="Waiting for review")
await update_goal_status(goal_id, "active")
await update_goal_status(goal_id, "complete", reason="Acceptance criteria met")
```

- `create_goal(...) -> str`: creates an active goal with iteration zero.
- `get_active_goal(project_id) -> dict | None`: newest active goal, ordered by
  creation time then ID descending for deterministic ties. Multiple active goals
  are allowed; there is no project-level uniqueness constraint. Updating or
  resuming an older goal does not change its creation order.
- `list_goals(project_id) -> list[dict]`: all statuses in the same order.
- `get_goal(goal_id) -> dict | None`: look up any individual goal.
- `update_goal_status(goal_id, status, reason=None)`: accepts `active`, `paused`,
  `complete`, `stopped`. Sets `updated_at` and replaces `stopped_reason`; `active`
  clears the reason. Transition policy is left to callers.
- `increment_iteration(goal_id)`: atomic SQL increment, safe against lost updates
  between concurrent callers. Sets `updated_at`; preserves status and reason.

Writes commit before returning and close their DB connections. Missing write
targets raise `LookupError`; invalid helper inputs raise `ValueError`. Read
helpers return `None`/`[]` for absent records. The registry stores iteration limits;
the future orchestrator enforces them. Reaching a limit does not mean success.
Project deletion cascades to its goals, matching existing project-owned tables.

## Verification

Using the backend dependencies and Python 3.11+:

```bash
python backend/test_goals.py
```

No test framework, fixtures, API keys, running server, or paid agent calls are
needed. The script uses a temporary database and checks existing-database
migration, repeated initialization, CRUD, concurrent increments, project
isolation/deletion, HTTP validation, and a real abrupt process kill followed by a
fresh process reading the identical active goal with three recorded iterations.

## Integration handoff

The append-only migration in `db.py` and goals router registration are included.
The five other tracks were absent from `main` when this track was built.

During the cross-track integration pass:

1. Track 1 replaces its stub lookup with `get_active_goal(project_id)` and reads
   `goal_text` from the returned record. Handle `None` explicitly.
2. Auto-loop startup uses the durable record and checks for existing active goals
   before creating a duplicate. Recovery must distinguish a persisted active goal
   from a live asyncio task; a persisted row alone does not prove a loop is running.
3. The loop records iterations and stop/completion reasons via these helpers,
   respects paused/stopped status, and enforces the stored iteration limit.
4. Register the remaining routers, integrate night-window guards and capacity,
   connect nightly summaries, then run the real overnight swarm smoke test.

`autoloop.py`, `mission_watcher.py`, `scheduler.py`, `sdk_engine.py`, and other
tracks' files are unchanged. This track does not address the pre-merge quality
gate limitation described in the feature spec.
