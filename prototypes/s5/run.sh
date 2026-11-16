#!/usr/bin/env bash
# Run prototype S5 against an installed Hermes checkout, in a throwaway home.
#
#   run.sh <hermes-checkout> <hermes-python> <A|B|C|D>
#
# <hermes-python> is the python of Hermes' committed dependency venv (see prototypes/s1/README.md:
# the store interpreter does not start with a throwaway HERMES_HOME). The probe directory must live
# in a fresh /tmp/hya-s5-XXXXXX directory; everything a run writes (HOME, the probe's own HERMES_HOME,
# every scene's home with its default profile and p2, the plugin copies, gateway locks, TMPDIR) stays
# under it, so removing that directory is the full cleanup. The probe binds only ephemeral 127.0.0.1
# ports (scripted LLM, the plugin's listener, the "foreign" listener) and never reaches the network.
# The probe spawns the gateway as CHILD processes (one at a time); the RSS watchdog counts the probe
# and every live child together.
#
# A: RecoveryTests + lifecycle (L1-L6, R6); B: busy port (P1, P2 at DEBUG); C: restart scenes R1-R5
# with interactive_resume=false and no restart notification; D: restart scenes D1-D2 with Hermes'
# defaults (interactive_resume=true, notification on). One probe process per group.
set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "usage: $0 <hermes-checkout> <hermes-python> <A|B|C|D>" >&2
    exit 2
fi
CHECKOUT=$1
PY=$2
GROUP=$3
case "$GROUP" in
    A) CLS=RecoveryTests,LifecycleGroup ;;
    B) CLS=PortGroup ;;
    C) CLS=RestartGroup ;;
    D) CLS=DefaultResumeGroup ;;
    *) echo "group must be A, B, C or D" >&2; exit 2 ;;
esac

HERE=$(cd "$(dirname "$0")" && pwd)
BASE=$(dirname "$HERE")
case "$(basename "$BASE")" in
    hya-s5-*) ;;
    *) echo "refusing: $HERE must be inside a hya-s5-* temp directory" >&2; exit 2 ;;
esac

ROOT="$BASE/root-$GROUP"
rm -rf -- "$ROOT"
mkdir -p "$ROOT/home/.hermes" "$ROOT/locks" "$ROOT/tmp" "$ROOT/scenes"
cd "$ROOT"

TIMER=()
if [ -x /usr/bin/time ]; then TIMER=(/usr/bin/time -v); fi

# env -i: nothing from the caller's environment - no real HERMES_HOME, proxies or tokens.
# S5_RSS_LIMIT_MB=500: the probe (~100 MB with the test fakes) plus one gateway (~250 MB), with headroom;
# the watchdog kills every child and exits 137 above it. choom -n 1000 makes the kernel pick the probe,
# not the live gateway, if memory still runs out (children inherit it).
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
    S5_CHECKOUT="$CHECKOUT" \
    S5_RSS_LIMIT_MB=500 \
    timeout 1500 nice -n 19 choom -n 1000 -- ${TIMER[@]+"${TIMER[@]}"} "$PY" -I -u -c '
import sys, runpy
checkout, here, classes = sys.argv[1:4]
sys.path.insert(0, checkout)
sys._hermes_pin_default_home = True
import hermes_bootstrap  # noqa: F401  (sets up the Hermes runtime site-packages)
sys.path.insert(0, here)
sys.argv = ["probe_s5", "-v", *("probe_s5." + c for c in classes.split(","))]
runpy.run_module("unittest", run_name="__main__", alter_sys=True)
' "$CHECKOUT" "$HERE" "$CLS"
