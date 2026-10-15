# Setup runbook (for Claude Code)

This file is written for a Claude Code session asked to "set up claude-devfleet". A human can follow it too. Do the steps in order, run every check, and report the result of each step back to the user. Ask the user only where a step says **Ask**.

## 1. Prerequisites

Check each one. If something is missing, install it with the platform's normal tool (Homebrew on macOS, the distro package manager or the official installer on Linux), telling the user what you're installing. Ask before using `sudo`.

| Need | Check | Expected |
|---|---|---|
| git | `git --version` | any |
| Python 3.11+ | `python3 --version` | ≥ 3.11 |
| Node.js 18+ | `node --version` | ≥ 18 |
| Claude Code | `claude --version` | any |
| Claude login | `claude auth status` | `"loggedIn": true` |

- If Claude Code isn't logged in, ask the user to run `! claude auth login` themselves. It's interactive, so you can't do it for them.
- If `authMethod` is not `claude.ai`, or `ANTHROPIC_API_KEY` is set in the environment: tell the user that agents will be billed per token from that API key instead of using their Claude plan's usage, which is what Night Shift is meant to use. Continue if they're fine with that.

## 2. Get the code

If you aren't already inside a clone of this repo:

```bash
git clone https://github.com/LEC-AI/claude-devfleet.git ~/claude-devfleet
cd ~/claude-devfleet
```

If a clone already exists, run `git pull --ff-only` in it instead of cloning again.

## 3. Configure

```bash
cp -n .env.example .env
```

**Ports.** Check the defaults are free: `lsof -iTCP:18801 -sTCP:LISTEN` and `lsof -iTCP:3100 -sTCP:LISTEN`, or `ss -ltn | grep -E ':(18801|3100) '` on Linux. On a shared machine, another user's DevFleet may already have them. If either is taken, pick a free pair (for example 18821 and 3120) and set `DEVFLEET_PORT` and `DEVFLEET_UI_PORT` in `.env`.

**API address for Claude Code.** If the API port isn't 18801, the `/nightshift` skill needs to know where it is. Add it to the `env` object in `~/.claude/settings.json`, creating the file or object if needed and keeping every other setting:

```json
{ "env": { "DEVFLEET_API_URL": "http://localhost:<DEVFLEET_PORT>" } }
```

It takes effect in new Claude Code sessions.

**Timezone.** Set `DEVFLEET_NIGHT_TZ` in `.env` to the machine's IANA timezone. Find it with `readlink /etc/localtime` (the part after `zoneinfo/`) on macOS and Linux, or `timedatectl show -p Timezone --value` on Linux. Leave the 20:00–08:00 window unless the user asked for different hours.

`<DEVFLEET_PORT>` and `<DEVFLEET_UI_PORT>` below mean the ports chosen here (18801 and 3100 by default).

## 4. Start DevFleet in the background

`start.sh` runs in the foreground until stopped. Start it detached so it outlives this session:

- **macOS:** `mkdir -p data && nohup caffeinate -i ./start.sh > data/devfleet.log 2>&1 &`. `caffeinate` stops the Mac idle-sleeping while DevFleet runs. Tell the user to keep the Mac on power and the lid open, or connected to a display, overnight.
- **Linux:** `mkdir -p data && nohup ./start.sh > data/devfleet.log 2>&1 &`

The first start installs Python and Node dependencies, which can take a few minutes.

## 5. Verify

Wait until these pass (poll for up to about 5 minutes; check `data/devfleet.log` if they don't):

```bash
curl -s localhost:<DEVFLEET_PORT>/api/config/engine   # {"engine":"sdk","sdk_available":true}
ls ~/.claude/skills/nightshift/SKILL.md         # /nightshift skill installed by start.sh
```

The UI is at http://localhost:<DEVFLEET_UI_PORT>. The API only listens on localhost by default. Set `DEVFLEET_HOST=0.0.0.0` in `.env` only if the user wants it on the network, and warn them first: anyone who can reach the port can start coding agents on this machine.

## 6. Register projects

**Ask** the user which repos DevFleet should work on, as absolute paths. Each one must be a git repo with at least one commit. Register each:

```bash
curl -s -X POST localhost:<DEVFLEET_PORT>/api/projects -H 'content-type: application/json' \
  -d '{"name": "<repo dir name>", "path": "<absolute path>"}'
```

Skip any repo already listed by `curl -s localhost:<DEVFLEET_PORT>/api/projects`. If the user doesn't know yet, skip this step: `/nightshift` registers a repo the first time it's used there.

## 7. Hand over

Tell the user:

1. **Plan tonight's work:** open Claude Code in a registered repo, run `/nightshift`, and describe what to build. It asks questions, shows a plan, and queues it once they approve. Agents run between the window's start and end times.
2. **Morning:** run `/nightshift report`. Night work waits on each repo's `nightshift` branch and never touches `main`; merging is up to them.
3. **After a reboot**, start DevFleet again with the step 4 command.
4. **Stop DevFleet:** `pkill -f 'port <DEVFLEET_PORT> --reload'` and `pkill -f 'vite --port <DEVFLEET_UI_PORT>'`, using the actual numbers (`pkill` can only stop your own processes).
