#!/bin/bash
# Monthly refresh, driven by launchd (local.usmnt-calendar.refresh).
#
# Run it by hand any time:   ./refresh-monthly.sh
# Change the cadence:        edit StartCalendarInterval in the plist, then
#                            launchctl unload && load it again.
#
# Two independent steps, logged separately:
#   1. refresh  -- rebuild usmnt-all-levels.ics from the live sources
#   2. publish  -- if it changed, commit and push so the raw GitHub URL that
#                  your calendar subscribes to serves the new version
#
# The publish step is skipped cleanly until the repo has an 'origin' remote,
# so this script is safe to run before GitHub is set up.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON=/usr/bin/python3
LOG="$HERE/refresh.log"
CALENDAR="usmnt-all-levels.ics"
MAX_LINES=3000

cd "$HERE" || exit 1

log() { echo "$@" >> "$LOG"; }

{
    echo
    echo "================================================================"
    echo "run $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "================================================================"
} >> "$LOG"

# ---------------------------------------------------------------- refresh
# refresh.py resolves every path relative to its own location, and writes the
# calendar atomically (temp file + rename on the same filesystem), so a reader
# never sees a half-written file.
"$PYTHON" "$HERE/refresh.py" >> "$LOG" 2>&1
STATUS=$?

case "$STATUS" in
    0) log "refresh: ok" ;;
    2) log "refresh: REFUSED TO WRITE -- calendar left untouched (see above)" ;;
    *) log "refresh: FAILED with exit code $STATUS" ;;
esac

# ---------------------------------------------------------------- publish
publish() {
    if [ "$STATUS" -ne 0 ]; then
        log "publish: skipped (refresh did not succeed)"
        return 0
    fi
    if ! git -C "$HERE" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        log "publish: skipped (not a git repo yet -- see README > Hosting)"
        return 0
    fi
    if ! git -C "$HERE" remote get-url origin >/dev/null 2>&1; then
        log "publish: skipped (no 'origin' remote yet -- see README > Hosting)"
        return 0
    fi

    # Nothing to publish is the normal monthly outcome: fixtures rarely move.
    if git -C "$HERE" diff --quiet -- "$CALENDAR" 2>/dev/null; then
        log "publish: no change to $CALENDAR, nothing to push"
        return 0
    fi

    local summary
    summary="$(grep -m1 -E '^  [0-9]+ events:' "$LOG" | sed 's/^ *//')"
    [ -n "$summary" ] || summary="calendar updated"

    git -C "$HERE" add "$CALENDAR" >> "$LOG" 2>&1
    git -C "$HERE" commit -m "Refresh calendar $(date '+%Y-%m-%d')" \
        -m "$summary" >> "$LOG" 2>&1 || {
        log "publish: FAILED at commit"
        return 1
    }

    # Non-interactive push. Needs credentials launchd can use without a
    # prompt: an SSH key (passphrase-less, or loaded via the keychain) or a
    # PAT in the macOS keychain. A hang here means git wanted to ask for
    # something, so prompting is disabled and the push fails fast instead.
    if GIT_TERMINAL_PROMPT=0 SSH_ASKPASS="" DISPLAY="" \
        git -C "$HERE" push origin HEAD >> "$LOG" 2>&1; then
        log "publish: pushed"
    else
        log "publish: FAILED at push -- commit is local only."
        log "         Usually means credentials are not available to launchd."
        log "         Check: git -C '$HERE' push origin HEAD"
        return 1
    fi
}

publish
PUBLISH_STATUS=$?

if [ "$STATUS" -eq 0 ] && [ "$PUBLISH_STATUS" -eq 0 ]; then
    log "result: ok"
else
    log "result: ATTENTION NEEDED (refresh=$STATUS publish=$PUBLISH_STATUS)"
fi

# Keep the log from growing without bound. Written to a temp file and moved so
# a reader never sees a half-truncated log.
if [ -f "$LOG" ]; then
    LINES=$(wc -l < "$LOG")
    if [ "$LINES" -gt "$MAX_LINES" ]; then
        TMP="$(mktemp "$HERE/.refresh.log.XXXXXX")"
        tail -n "$MAX_LINES" "$LOG" > "$TMP" && mv "$TMP" "$LOG"
    fi
fi

[ "$STATUS" -eq 0 ] && exit "$PUBLISH_STATUS" || exit "$STATUS"
