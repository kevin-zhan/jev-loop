#!/bin/sh
# Packaging acceptance for the Jev Bundle Specification: build the wheel, install it OUTSIDE
# this checkout, then exercise the new CLI, the packaged templates and a real managed run.
#
#   scripts/wheel-smoke.sh [workdir]
#
# Everything is offline and everything it creates lives under a temporary directory (or the
# workdir you pass). It never installs into the developer's own environment.
set -eu

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
WORKDIR=${1:-$(mktemp -d "${TMPDIR:-/tmp}/jev-loop-wheel-smoke.XXXXXX")}
mkdir -p "$WORKDIR"
DIST="$WORKDIR/dist"
VENV="$WORKDIR/venv"
PROJECT="$WORKDIR/project"

fail() {
    echo "wheel-smoke: FAIL: $*" >&2
    exit 1
}

echo "wheel-smoke: repo=$REPO_ROOT workdir=$WORKDIR"

# 1. Build the wheel from the checkout.
rm -rf "$DIST"
( cd "$REPO_ROOT" && uv build --out-dir "$DIST" >/dev/null ) || fail "uv build failed"
WHEEL=$(ls "$DIST"/jev_loop-*.whl | head -1)
echo "wheel-smoke: built $(basename "$WHEEL")"

# 2. Install it into a throwaway environment outside the checkout.
rm -rf "$VENV" "$PROJECT"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --no-index "$WHEEL" || fail "pip install of the wheel failed"
JEV_LOOP="$VENV/bin/jev-loop"
[ -x "$JEV_LOOP" ] || fail "the jev-loop console script was not installed"

# 3. Run the CLI from a directory that is not the checkout, with an isolated HOME.
mkdir -p "$PROJECT" "$WORKDIR/home"
export HOME="$WORKDIR/home"
export JEV_LOOP_HOME="$WORKDIR/host-home"
cd "$WORKDIR"

"$JEV_LOOP" bundle list --project-root "$PROJECT" --json >/dev/null || fail "bundle list failed"

# 4. Scaffold from the packaged template, then validate and run the author tests.
"$JEV_LOOP" bundle init smoke-bundle --project-root "$PROJECT" --description "wheel smoke" >/dev/null \
    || fail "bundle init failed"
"$JEV_LOOP" bundle validate project:smoke-bundle --project-root "$PROJECT" --json >/dev/null \
    || fail "the generated scaffold did not validate"
"$JEV_LOOP" bundle conformance project:smoke-bundle --project-root "$PROJECT" --run-tests >/dev/null \
    || fail "conformance with the generated author tests failed"

# 5. A scaffold must be refused by the host, before any run exists.
if printf '%s' "{\"action\":\"start\",\"owner_id\":\"smoke\",\"idempotency_key\":\"scaffold\",
  \"project_root\":\"$PROJECT\",\"bundle\":\"project:smoke-bundle\",\"task\":{\"goal\":\"x\"}}" \
    | "$JEV_LOOP" rpc >"$WORKDIR/scaffold.json" 2>/dev/null; then
    fail "starting a scaffold was accepted"
fi
grep -q "scaffold=true" "$WORKDIR/scaffold.json" || fail "the scaffold refusal did not name the reason"

# 6. The shipped reference bundle runs by name through the installed CLI, and stops safely.
cp -R "$REPO_ROOT/.agents/jev-bundle/offline-switchboard" "$PROJECT/.agents/jev-bundle/"
STARTED=$(printf '%s' "{\"action\":\"start\",\"owner_id\":\"smoke\",\"idempotency_key\":\"reference\",
  \"project_root\":\"$PROJECT\",\"bundle\":\"project:offline-switchboard\",
  \"task\":{\"goal\":\"turn on every required switch\"},\"max_runtime_seconds\":30,\"lease_seconds\":30}" \
    | "$JEV_LOOP" rpc) || fail "starting the reference bundle failed"
RUN_ID=$(printf '%s' "$STARTED" | "$VENV/bin/python" -c 'import json,sys; print(json.load(sys.stdin)["run"]["run_id"])')
[ -n "$RUN_ID" ] || fail "start returned no run id"

STATUS=""
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40; do
    INSPECTED=$(printf '%s' "{\"action\":\"inspect\",\"owner_id\":\"smoke\",\"run_id\":\"$RUN_ID\"}" | "$JEV_LOOP" rpc)
    STATUS=$(printf '%s' "$INSPECTED" | "$VENV/bin/python" -c 'import json,sys; print(json.load(sys.stdin)["run"]["status"])')
    case "$STATUS" in
        succeeded|failed|cancelled|expired) break ;;
    esac
    sleep 0.25
done
[ "$STATUS" = "succeeded" ] || fail "the reference run ended as $STATUS"

printf '%s' "{\"action\":\"stop\",\"owner_id\":\"smoke\",\"run_id\":\"$RUN_ID\",\"confirm_seconds\":5}" \
    | "$JEV_LOOP" rpc | "$VENV/bin/python" -c '
import json, sys
run = json.load(sys.stdin)["run"]
assert run["status"] not in {"starting", "running", "stopping"}, run
assert run["resources_released"] is True, run
' || fail "the reference run did not stop with released resources"

echo "wheel-smoke: PASS (wheel install, init, validate, author tests, scaffold refusal, reference run)"
