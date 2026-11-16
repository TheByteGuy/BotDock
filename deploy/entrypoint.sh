#!/bin/sh
# Starts as root only to make /data writable for the unprivileged user, then drops to it.
# If the container is already started as a non-root user, it just runs.
set -e
DATA="${BOTDOCK_DATA:-/data}"
PUID="${PUID:-568}"; PGID="${PGID:-568}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA" "$HOME"
    if [ "$(stat -c %u:%g "$DATA")" != "$PUID:$PGID" ]; then
        chown -R "$PUID:$PGID" "$DATA"
    fi
    chown "$PUID:$PGID" "$HOME"
else
    mkdir -p "$HOME" 2>/dev/null || true
fi

# Keep the first-run password out of the process environment, where every bot could read it
# from /proc. BotDock reads this file once at startup and deletes it.
if [ -n "${BOTDOCK_PASSWORD:-}" ]; then
    SEED="$DATA/.seed-password"
    ( umask 077; printf '%s' "$BOTDOCK_PASSWORD" > "$SEED" )
    [ "$(id -u)" = "0" ] && chown "$PUID:$PGID" "$SEED"
    export BOTDOCK_PASSWORD_FILE="$SEED"
    unset BOTDOCK_PASSWORD
fi

if [ "$(id -u)" = "0" ]; then
    exec setpriv --reuid "$PUID" --regid "$PGID" --clear-groups "$@"
fi
exec "$@"
