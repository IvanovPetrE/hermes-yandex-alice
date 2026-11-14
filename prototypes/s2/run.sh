#!/usr/bin/env bash
# Run prototype S2 against an installed Hermes checkout, in a throwaway home.
#
#   run.sh <hermes-checkout> <hermes-python> <A1|A2|A2g|A3|A4|A0|B> <free-loopback-port>
#
# <hermes-python> is the python of Hermes' committed dependency venv (see prototypes/s1/README.md:
# the store interpreter does not start with a throwaway HERMES_HOME). The probe directory must live
# in a fresh /tmp/hya-s2-XXXXXX directory; everything the run writes (HOME, HERMES_HOME with the
# default profile and profile p2, gateway locks, TMPDIR) stays under it, so removing that directory
# is the full cleanup. The port is bound on 127.0.0.1 by the stub adapter's connect(); check it is
# free first (ss -ltn).
set -euo pipefail

if [ "$#" -ne 4 ]; then
    echo "usage: $0 <hermes-checkout> <hermes-python> <A1|A2|A2g|A3|A4|A0|B> <port>" >&2
    exit 2
fi
CHECKOUT=$1
PY=$2
GROUP=$3
PORT=$4
case "$GROUP" in
    A1|A2|A2g|A3|A4|A0|B) ;;
    *) echo "group must be one of A1 A2 A2g A3 A4 A0 B" >&2; exit 2 ;;
esac
case "$PORT" in
    ''|*[!0-9]*) echo "port must be a number" >&2; exit 2 ;;
esac

HERE=$(cd "$(dirname "$0")" && pwd)
BASE=$(dirname "$HERE")
case "$(basename "$BASE")" in
    hya-s2-*) ;;
    *) echo "refusing: $HERE must be inside a hya-s2-* temp directory" >&2; exit 2 ;;
esac

ROOT="$BASE/root-$GROUP"
rm -rf -- "$ROOT"
mkdir -p "$ROOT/home/.hermes" "$ROOT/locks" "$ROOT/tmp"
cd "$ROOT"

TIMER=()
if [ -x /usr/bin/time ]; then TIMER=(/usr/bin/time -v); fi

# env -i: nothing from the caller's environment. No ALICE_* here on purpose: the probe writes them
# into the profiles' .env files, which is what S2 observes. S2_RSS_LIMIT_MB arms the probe's own RSS
# watchdog (it exits 137 above the limit); choom -n 1000 makes the kernel pick the probe, not the
# live gateway, if memory still runs out. timeout 900 > boot + 5 scenarios x TURN_TIMEOUT.
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
    S2_PROBE_PORT="$PORT" \
    S2_RSS_LIMIT_MB=400 \
    timeout 900 nice -n 19 choom -n 1000 -- ${TIMER[@]+"${TIMER[@]}"} "$PY" -I -u -c '
import sys, runpy
checkout, here, group = sys.argv[1:4]
sys.path.insert(0, checkout)
sys._hermes_pin_default_home = True
import hermes_bootstrap  # noqa: F401  (sets up the Hermes runtime site-packages)
sys.path.insert(0, here)
sys.argv = ["probe_s2", "-v", "probe_s2.Group" + group]
runpy.run_module("unittest", run_name="__main__", alter_sys=True)
' "$CHECKOUT" "$HERE" "$GROUP"
