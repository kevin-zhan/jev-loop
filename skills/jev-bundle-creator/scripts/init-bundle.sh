#!/bin/sh
# Scaffold a new Jev Loop bundle with the installed CLI.
#
#   scripts/init-bundle.sh <name> [--project-root DIR] [--description TEXT]
#
# This is a thin wrapper: templates ship inside the jev-loop package, so it works from an
# installed wheel outside any source checkout.
set -eu

if ! command -v jev-loop >/dev/null 2>&1; then
    echo "init-bundle: 'jev-loop' is not on PATH. Install the package (for example: uv tool install jev-loop," \
         "or pip install jev-loop) and retry." >&2
    exit 3
fi

if [ "$#" -lt 1 ]; then
    echo "usage: init-bundle.sh <name> [--project-root DIR] [--description TEXT]" >&2
    exit 3
fi

exec jev-loop bundle init "$@"
