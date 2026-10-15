# Claude Code integration

## `/nightshift` skill

Plans new requirements with you (reads the repo, asks about anything ambiguous), then queues them as `night` missions that DevFleet runs unattended inside the night window, resuming after rate limits and landing on a `nightshift` branch for morning review. `/nightshift report` summarises the night.

`./start.sh` installs it into `~/.claude/skills/` for you. Manual install:

```bash
mkdir -p ~/.claude/skills && cp -R integrations/claude-code/skills/nightshift ~/.claude/skills/
```

Set `DEVFLEET_API_URL` if your API is not on `http://localhost:18801`. Full machine setup: [SETUP.md](../../SETUP.md).
