#!/usr/bin/env bash
# precommit-scrub.sh — pre-publish safety check for OSS contributions
#
# Scans the staged diff (or any path) for patterns that usually shouldn't
# land in a public repo:
#   1. absolute host paths        (real /home/<user>/ paths; placeholders allowed)
#   2. common API-key shapes      (Anthropic, OpenAI, GitHub, AWS, Slack, JWT)
#   3. specific calendar dates    (Month-Day-Year or ISO; usually incident refs)
#   4. internal-looking endpoints (any .com/.ai/.io/.net not on the allowlist)
#   5. optional extra patterns    (one-per-line file via --extra <path> or
#                                  $DEVFLEET_SCRUB_EXTRA env var)
#
# Use the --extra mechanism for tenant-private words (codenames, contributor
# names, customer names) you keep OUTSIDE the public repo, e.g.
# `~/.devfleet/private-names.txt`. The list itself never enters this repo.
#
# Usage:
#   scripts/precommit-scrub.sh                          # scan staged + modified + untracked
#   scripts/precommit-scrub.sh --staged-only            # scan only `git add`'d diff (hook mode)
#   scripts/precommit-scrub.sh --all                    # scan all tracked files
#   scripts/precommit-scrub.sh path/to/file.py ...      # scan specific paths
#   scripts/precommit-scrub.sh --extra ~/private.txt    # add private patterns
#
# Wire as a git pre-commit hook:
#   echo '#!/bin/sh' > .git/hooks/pre-commit
#   echo 'exec ./scripts/precommit-scrub.sh --staged-only' >> .git/hooks/pre-commit
#   chmod +x .git/hooks/pre-commit
#
# Exit codes: 0 clean, 1 hits found, 2 usage / setup error.

set -u

# ── Endpoints we explicitly allow (regex pieces, OR'd together) ───────
ALLOW_DOMAINS='anthropic\.com|claude\.ai|docs\.anthropic|github\.com|astral\.sh|raw\.githubusercontent|astral-sh|pypi\.org|python\.org|fastapi\.tiangolo|hub\.docker\.com|host\.docker\.internal|localhost|127\.0\.0\.1|0\.0\.0\.0|example\.com|example\.org|example\.ai|example\.io|example\.net'

# Common placeholder usernames used in OSS docs — don't flag these.
ALLOW_PLACEHOLDER_USERS='/home/(user|users|USER|USERNAME|me|you|example|your-name|your_name|YOUR_NAME|<user>|placeholder)/|/Users/(USER|USERNAME|you|example|your-name|YOUR_NAME|me)/'

# ── Built-in pattern groups (these are safe to publish — generic) ─────
# 1. Absolute host paths
PAT_PATHS='/home/[a-zA-Z][a-zA-Z0-9_-]*/|/Users/[A-Za-z]|^C:\\|[\\/]var[\\/]lib[\\/][a-z]+[\\/][a-z]+'

# 2. Common API key shapes
PAT_SECRETS='sk-[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abps]-[A-Za-z0-9-]{20,}|AKIA[0-9A-Z]{16}|AIza[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}'

# 3. Specific calendar dates that smell like incident anchors
PAT_DATES='\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* [0-9]{1,2}(,? 20[0-9]{2})?\b|\b20[0-9]{2}-[0-9]{2}-[0-9]{2}\b|\b[Qq][1-4] 20[0-9]{2}\b'

# 4. Internal-looking URLs (will be filtered against ALLOW_DOMAINS later)
PAT_URLS='https?://[a-zA-Z0-9.-]+\.(ai|com|io|net|co|org|app|dev)'

# ── Args ──────────────────────────────────────────────────────────────
EXTRA_FILE="${DEVFLEET_SCRUB_EXTRA:-}"
MODE="staged"
PATHS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --extra)
            EXTRA_FILE="$2"; shift 2 ;;
        --all)
            MODE="all"; shift ;;
        --staged-only)
            MODE="staged-only"; shift ;;
        -h|--help)
            sed -n '2,32p' "$0"; exit 0 ;;
        --)
            shift; PATHS+=("$@"); break ;;
        -*)
            echo "Unknown flag: $1" >&2; exit 2 ;;
        *)
            MODE="paths"; PATHS+=("$1"); shift ;;
    esac
done

# ── Resolve target file list ──────────────────────────────────────────
if [[ "$MODE" == "staged" ]]; then
    # Default: staged + working-tree modifications + untracked-non-ignored.
    # Pre-commit hooks should still use --cached only; pass --staged-only for that.
    mapfile -t STAGED < <(git diff --cached --name-only --diff-filter=ACMR 2>/dev/null)
    mapfile -t MODIFIED < <(git diff --name-only --diff-filter=ACMR 2>/dev/null)
    mapfile -t UNTRACKED < <(git ls-files --others --exclude-standard 2>/dev/null)
    FILES=("${STAGED[@]}" "${MODIFIED[@]}" "${UNTRACKED[@]}")
    # dedupe
    mapfile -t FILES < <(printf '%s\n' "${FILES[@]}" | awk 'NF && !seen[$0]++')
elif [[ "$MODE" == "staged-only" ]]; then
    mapfile -t FILES < <(git diff --cached --name-only --diff-filter=ACMR 2>/dev/null)
elif [[ "$MODE" == "all" ]]; then
    mapfile -t FILES < <(git ls-files 2>/dev/null)
else
    FILES=("${PATHS[@]}")
fi

# Filter to files that actually exist and look like text
TEXT_FILES=()
for f in "${FILES[@]}"; do
    [[ -f "$f" ]] || continue
    # skip lockfiles, images, binaries
    case "$f" in
        *.png|*.jpg|*.jpeg|*.gif|*.svg|*.ico|*.pdf|*.zip|*.tar*|*.lock|*.lock.json|*.bin)
            continue ;;
    esac
    TEXT_FILES+=("$f")
done

if [[ ${#TEXT_FILES[@]} -eq 0 ]]; then
    echo "[scrub] no files to scan (mode=$MODE)"
    exit 0
fi

# ── Load extra patterns ───────────────────────────────────────────────
PAT_EXTRA=""
if [[ -n "$EXTRA_FILE" ]]; then
    if [[ ! -f "$EXTRA_FILE" ]]; then
        echo "[scrub] WARNING: --extra file not found: $EXTRA_FILE" >&2
    else
        # Join non-empty, non-comment lines with |
        PAT_EXTRA=$(grep -vE '^[[:space:]]*(#|$)' "$EXTRA_FILE" \
                    | sed 's/[[:space:]]*$//' \
                    | paste -sd '|' -)
    fi
fi

# ── Run the lenses ────────────────────────────────────────────────────
hit_count=0
report() {
    local lens="$1" pat="$2" allow_filter="$3"
    [[ -z "$pat" ]] && return 0
    local out
    if [[ -n "$allow_filter" ]]; then
        out=$(grep -HniEr "$pat" "${TEXT_FILES[@]}" 2>/dev/null \
              | grep -vEi "$allow_filter" || true)
    else
        out=$(grep -HniEr "$pat" "${TEXT_FILES[@]}" 2>/dev/null || true)
    fi
    if [[ -n "$out" ]]; then
        echo
        echo "── $lens ──"
        echo "$out" | head -30
        local n
        n=$(echo "$out" | wc -l)
        hit_count=$(( hit_count + n ))
        [[ "$n" -gt 30 ]] && echo "(... $((n-30)) more)"
    fi
}

echo "[scrub] mode=$MODE files=${#TEXT_FILES[@]} ${EXTRA_FILE:+extra=$EXTRA_FILE}"

report "absolute host paths"       "$PAT_PATHS"   "$ALLOW_PLACEHOLDER_USERS"
report "API key / token shapes"    "$PAT_SECRETS" ""
report "specific calendar dates"   "$PAT_DATES"   ""
report "non-allowlisted URLs"      "$PAT_URLS"    "$ALLOW_DOMAINS"
report "extra patterns (private)"  "$PAT_EXTRA"   ""

echo
if [[ $hit_count -eq 0 ]]; then
    echo "[scrub] CLEAN — nothing flagged."
    exit 0
else
    echo "[scrub] $hit_count finding(s). Review above; fix or add to allowlist before committing."
    exit 1
fi
