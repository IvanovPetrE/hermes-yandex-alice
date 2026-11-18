#!/usr/bin/env bash
# Run prototype S7 against an installed Hermes checkout, in a throwaway home.
#
#   run.sh <hermes-checkout> <hermes-python> <A|B|C>
#
# <hermes-python> is the python of Hermes' committed dependency venv (see compat/README.md:
# the store interpreter does not start with a throwaway HERMES_HOME). The probe directory must live
# in a fresh /tmp/hya-s7-XXXXXX directory; everything a run writes (HOME, the probe's own HERMES_HOME,
# every scene's home with its default profile and p2, the plugin copies, gateway locks, TMPDIR) stays
# under it, so removing that directory is the full cleanup. The probe binds only ephemeral 127.0.0.1
# ports (the scripted LLM) and never reaches the network.
# The probe spawns the gateway as CHILD processes (one at a time); the RSS watchdog counts the probe
# and every live child together (S7_RSS_LIMIT_MB=450; the watchdog exits 137 above it).
#
# A: HistoryTests + the routed gateway (route yandex_alice -> p2): scenes A0-A9, validate;
# B: restarts (idle stop, stop mid-turn, SIGKILL inside the final send), each with its own home;
# C: voice in the default profile, no route.
set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "usage: $0 <hermes-checkout> <hermes-python> <A|B|C>" >&2
    exit 2
fi
CHECKOUT=$1
PY=$2
GROUP=$3
case "$GROUP" in
    A) CLS=HistoryTests,GroupA ;;
    B) CLS=GroupB ;;
    C) CLS=GroupC ;;
    *) echo "group must be A, B or C" >&2; exit 2 ;;
esac

HERE=$(cd "$(dirname "$0")" && pwd)
BASE=$(dirname "$HERE")
case "$(basename "$BASE")" in
    hya-s7-*) ;;
    *) echo "refusing: $HERE must be inside a hya-s7-* temp directory" >&2; exit 2 ;;
esac

ROOT="$BASE/root-$GROUP"
rm -rf -- "$ROOT"
mkdir -p "$ROOT/home/.hermes" "$ROOT/locks" "$ROOT/tmp" "$ROOT/scenes"
cd "$ROOT"

TIMER=()
if [ -x /usr/bin/time ]; then TIMER=(/usr/bin/time -v); fi

# env -i: nothing from the caller's environment - no real HERMES_HOME, proxies or tokens.
# choom -n 1000 makes the kernel pick the probe, not the live gateway, if memory still runs out.
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
    S7_CHECKOUT="$CHECKOUT" \
    S7_RSS_LIMIT_MB=450 \
    timeout 1500 nice -n 19 choom -n 1000 -- ${TIMER[@]+"${TIMER[@]}"} "$PY" -I -u -c '
import sys, runpy
checkout, here, classes = sys.argv[1:4]
sys.path.insert(0, checkout)
sys._hermes_pin_default_home = True
import hermes_bootstrap  # noqa: F401  (sets up the Hermes runtime site-packages)
sys.path.insert(0, here)
sys.argv = ["probe_s7", "-v", *("probe_s7." + c for c in classes.split(","))]
runpy.run_module("unittest", run_name="__main__", alter_sys=True)
' "$CHECKOUT" "$HERE" "$CLS"
