#!/usr/bin/env bash
# Run prototype S1 against an installed Hermes checkout, in a throwaway home.
#
#   run.sh <hermes-checkout> <hermes-python> <A|B>
#
# <hermes-python> is the python of Hermes' committed dependency venv
# (<real HERMES_HOME>/installs/<key>/environments/<generation>/venv/bin/python; the
# site-packages entry of `sys.path` after `import hermes_bootstrap` names it). The store
# interpreter from `hermes --print-runtime-command` does not work here: with a throwaway
# HERMES_HOME it finds no committed environment and refuses to start. The probe directory must live in a fresh
# /tmp/hya-s1-XXXXXX directory; everything the run writes (HOME, HERMES_HOME,
# gateway locks, TMPDIR) stays under it, so removing that directory is the full cleanup.
set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "usage: $0 <hermes-checkout> <hermes-python> <A|B>" >&2
    exit 2
fi
CHECKOUT=$1
PY=$2
GROUP=$3
case "$GROUP" in
    A) CLS=TrackerRuleTests,GroupA ;;
    B) CLS=GroupB ;;
    *) echo "group must be A or B" >&2; exit 2 ;;
esac

HERE=$(cd "$(dirname "$0")" && pwd)
BASE=$(dirname "$HERE")
case "$(basename "$BASE")" in
    hya-s1-*) ;;
    *) echo "refusing: $HERE must be inside a hya-s1-* temp directory" >&2; exit 2 ;;
esac

ROOT="$BASE/root-$GROUP"
rm -rf -- "$ROOT"
mkdir -p "$ROOT/home/.hermes" "$ROOT/locks" "$ROOT/tmp"
cd "$ROOT"

TIMER=()
if [ -x /usr/bin/time ]; then TIMER=(/usr/bin/time -v); fi

# env -i: nothing from the caller's environment (no real HERMES_HOME, no XDG runtime dir).
# choom -n 1000: if memory runs out, the kernel kills the probe, not the live gateway.
# -I implies -E (PYTHON* variables are ignored), so unbuffered output is the -u flag: the
# journal printed so far survives a timeout kill. Hash seed stays random (nothing depends on it).
# timeout 1200 > worst case: boot + 7 scenarios x 180 s wait_for is already above 600 s.
exec env -i \
    PATH=/usr/local/bin:/usr/bin:/bin \
    LANG=C.UTF-8 \
    HOME="$ROOT/home" \
    HERMES_HOME="$ROOT/home/.hermes" \
    HERMES_GATEWAY_LOCK_DIR="$ROOT/locks" \
    TMPDIR="$ROOT/tmp" \
    TZ=UTC \
    HERMES_DISABLE_LAZY_INSTALLS=1 \
    AWS_EC2_METADATA_DISABLED=true \
    ALICE_OWNER_USER_ID=u-probe-owner \
    timeout 1200 nice -n 19 choom -n 1000 -- ${TIMER[@]+"${TIMER[@]}"} "$PY" -I -u -c '
import sys, runpy
checkout, here, classes = sys.argv[1:4]
sys.path.insert(0, checkout)
sys._hermes_pin_default_home = True
import hermes_bootstrap  # noqa: F401  (sets up the Hermes runtime site-packages)
sys.path.insert(0, here)
sys.argv = ["probe_s1", "-v", *("probe_s1." + c for c in classes.split(","))]
runpy.run_module("unittest", run_name="__main__", alter_sys=True)
' "$CHECKOUT" "$HERE" "$CLS"
