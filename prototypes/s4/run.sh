#!/usr/bin/env bash
# Run prototype S4 against an installed Hermes checkout, in a throwaway home.
#
#   run.sh <hermes-checkout> <hermes-python> <A|B|C>
#
# <hermes-python> is the python of Hermes' committed dependency venv (see prototypes/s1/README.md:
# the store interpreter does not start with a throwaway HERMES_HOME). The probe directory must live
# in a fresh /tmp/hya-s4-XXXXXX directory; everything a run writes (HOME, HERMES_HOME with the
# default profile and profiles p2/p3, the plugin copy, victim directories, gateway locks, TMPDIR)
# stays under it, so removing that directory is the full cleanup. The probe binds only ephemeral
# 127.0.0.1 ports (scripted LLM, two Bot API stand-ins) and never reaches Telegram.
#
# A: owner's private chat (BookTests + 12 gateway scenes); B: group chat without an owner id
# (approvals disabled); C: group chat with ALICE_TELEGRAM_OWNER_ID. One gateway process per group.
set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "usage: $0 <hermes-checkout> <hermes-python> <A|B|C>" >&2
    exit 2
fi
CHECKOUT=$1
PY=$2
GROUP=$3
case "$GROUP" in
    A) CLS=BookTests,GroupA ;;
    B) CLS=GroupB ;;
    C) CLS=GroupC ;;
    *) echo "group must be A, B or C" >&2; exit 2 ;;
esac

HERE=$(cd "$(dirname "$0")" && pwd)
BASE=$(dirname "$HERE")
case "$(basename "$BASE")" in
    hya-s4-*) ;;
    *) echo "refusing: $HERE must be inside a hya-s4-* temp directory" >&2; exit 2 ;;
esac

ROOT="$BASE/root-$GROUP"
rm -rf -- "$ROOT"
mkdir -p "$ROOT/home/.hermes" "$ROOT/locks" "$ROOT/tmp"
cd "$ROOT"

TIMER=()
if [ -x /usr/bin/time ]; then TIMER=(/usr/bin/time -v); fi

# env -i: nothing from the caller's environment - no real HERMES_HOME, proxies or tokens.
# HERMES_TELEGRAM_*: no fallback-IP discovery (DoH) and no text-batch debounce, as Hermes' own
# Telegram e2e driver sets them. S4_RSS_LIMIT_MB arms the probe's RSS watchdog (exit 137 above it);
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
    HERMES_TELEGRAM_DISABLE_FALLBACK_IPS=1 \
    HERMES_TELEGRAM_TEXT_BATCH_DELAY_SECONDS=0 \
    HERMES_TELEGRAM_TEXT_BATCH_SPLIT_DELAY_SECONDS=0 \
    S4_RSS_LIMIT_MB=300 \
    timeout 900 nice -n 19 choom -n 1000 -- ${TIMER[@]+"${TIMER[@]}"} "$PY" -I -u -c '
import sys, runpy
checkout, here, classes = sys.argv[1:4]
sys.path.insert(0, checkout)
sys._hermes_pin_default_home = True
import hermes_bootstrap  # noqa: F401  (sets up the Hermes runtime site-packages)
sys.path.insert(0, here)
sys.argv = ["probe_s4", "-v", *("probe_s4." + c for c in classes.split(","))]
runpy.run_module("unittest", run_name="__main__", alter_sys=True)
' "$CHECKOUT" "$HERE" "$CLS"
