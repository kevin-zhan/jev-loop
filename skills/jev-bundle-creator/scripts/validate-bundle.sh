#!/bin/sh
# Validate a Jev Loop bundle, and optionally run its own offline tests.
#
#   scripts/validate-bundle.sh <ref> [--project-root DIR] [--run-tests]
#
#   ref: 'diagnostic', project:<name>, a discovered name, or a manifest path
set -eu

if ! command -v jev-loop >/dev/null 2>&1; then
    echo "validate-bundle: 'jev-loop' is not on PATH. Install the jev-loop package and retry." >&2
    exit 3
fi

if [ "$#" -lt 1 ]; then
    echo "usage: validate-bundle.sh <ref> [--project-root DIR] [--run-tests]" >&2
    exit 3
fi

reference=$1
shift

mode=validate
for argument in "$@"; do
    if [ "$argument" = "--run-tests" ]; then
        mode=conformance
    fi
done

if [ "$mode" = "conformance" ]; then
    exec jev-loop bundle conformance "$reference" "$@"
fi
exec jev-loop bundle validate "$reference" "$@"
