#!/usr/bin/env bash
# Run prototype S6 against an installed Hermes checkout, in a throwaway home.
#
#   run.sh <hermes-checkout> <hermes-python> <A|B|C>
#
# <hermes-python> is the python of Hermes' committed dependency venv (see prototypes/s1/README.md:
# the store interpreter does not start with a throwaway HERMES_HOME). The probe directory must live
# in a fresh /tmp/hya-s6-XXXXXX directory; everything a run writes (HOME, HERMES_HOME with the
# default profile and p2, the plugin copies, cron stores, gateway locks, TMPDIR) stays under it, so
# removing that directory is the full cleanup. The probe binds only ephemeral 127.0.0.1 ports (scripted
# LLM, the Bot API stand-in) and never reaches Telegram.
#
# A: GuardTests + ForwardTests + the plugin only in the default profile (session, compression, clarify,
# cron C1-C4, out-of-turn forward); B: the plugin also in p2 with ALICE_TELEGRAM_CHAT in its .env (the
# hook, H2/H3/H5); C: the plugin also in p2 without that chat (H4). One gateway process per group; the
# cron scenes wait for the gateway's own 60 s ticker and delivery drain (A ~10 min, B ~6 min, C ~1 min).
set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "usage: $0 <hermes-checkout> <hermes-python> <A|B|C>" >&2
    exit 2
fi
CHECKOUT=$1
PY=$2
GROUP=$3
case "$GROUP" in
    A) CLS=GuardTests,ForwardTests,GroupA ;;
    B) CLS=GroupB ;;
    C) CLS=GroupC ;;
    *) echo "group must be A, B or C" >&2; exit 2 ;;
esac

HERE=$(cd "$(dirname "$0")" && pwd)
# BASE: the nearest ancestor named hya-s6-* (the probe may sit at $D/s6 or $D/prototypes/s6).
BASE=$HERE
while [ "$BASE" != "/" ]; do
    case "$(basename "$BASE")" in hya-s6-*) break ;; esac
    BASE=$(dirname "$BASE")
done
if [ "$BASE" = "/" ]; then
    echo "refusing: $HERE must be inside a hya-s6-* temp directory" >&2
    exit 2
fi

ROOT="$BASE/root-$GROUP"
rm -rf -- "$ROOT"
mkdir -p "$ROOT/home/.hermes" "$ROOT/locks" "$ROOT/tmp"
cd "$ROOT"

TIMER=()
if [ -x /usr/bin/time ]; then TIMER=(/usr/bin/time -v); fi

# env -i: nothing from the caller's environment - no real HERMES_HOME, proxies or tokens.
# S6_RSS_LIMIT_MB=450: one gateway with one PTB app is ~260 MB (S1-S5); the watchdog exits 137 above it.
# HERMES_TELEGRAM_*: no fallback-IP discovery (DoH) and no text-batch debounce, as Hermes' own Telegram
# e2e driver sets them. HERMES_GATEWAY_SESSION=1: a real gateway runs each turn in a managed worker that
# sets it (agent/managed_worker.py); the harness runs the agent in-process, and without it cronjob_manage
# is not offered at all (tools/cronjob_tools.py, check_cronjob_requirements). choom -n 1000 makes the
# kernel pick the probe, not the live gateway, if memory still runs out.
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
    HERMES_TELEGRAM_DISABLE_FALLBACK_IPS=1 \
    HERMES_TELEGRAM_TEXT_BATCH_DELAY_SECONDS=0 \
    HERMES_TELEGRAM_TEXT_BATCH_SPLIT_DELAY_SECONDS=0 \
    HERMES_GATEWAY_SESSION=1 \
    S6_RSS_LIMIT_MB=450 \
    timeout 1500 nice -n 19 choom -n 1000 -- ${TIMER[@]+"${TIMER[@]}"} "$PY" -I -u -c '
import sys, runpy
checkout, here, classes = sys.argv[1:4]
sys.path.insert(0, checkout)
sys._hermes_pin_default_home = True
import hermes_bootstrap  # noqa: F401  (sets up the Hermes runtime site-packages)
sys.path.insert(0, here)
sys.argv = ["probe_s6", "-v", *("probe_s6." + c for c in classes.split(","))]
runpy.run_module("unittest", run_name="__main__", alter_sys=True)
' "$CHECKOUT" "$HERE" "$CLS"
