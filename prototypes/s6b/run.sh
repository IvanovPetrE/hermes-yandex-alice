#!/usr/bin/env bash
# Run prototype S6b against an installed Hermes checkout, in a throwaway home.
#
#   run.sh <hermes-checkout> <hermes-python> <B|C|L|P>
#
# <hermes-python> is the python of Hermes' committed dependency venv (see prototypes/s1/README.md).
# The probe directories prototypes/s6b AND prototypes/s6 (its harness) must sit side by side in a fresh
# /tmp/hya-s6b-XXXXXX directory; everything a run writes (HOME, HERMES_HOME with the default profile and p2,
# the plugin copies, cron stores, gateway locks, TMPDIR) stays under it, so removing that directory is the
# full cleanup. The probe binds only ephemeral 127.0.0.1 ports and never reaches Telegram.
#
# B: the plugin also in p2 with ALICE_TELEGRAM_CHAT in its .env (W0-W3); C: the plugin in p2 without that
# chat (W0, W1, W4); L: as B, but p2's plugin is a SYMLINK to the default profile's copy (one copy on disk:
# L0-L2); P: the pure layer only (GuardTests, ForwardTests, CopyTests), no gateway.
# Both profiles get gateway.managed_workers: true. HERMES_EXEC_ASK=1 is what start_gateway() sets (and what
# makes cronjob_manage available); HERMES_GATEWAY_SESSION is deliberately NOT set (S6 set it by hand).
set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "usage: $0 <hermes-checkout> <hermes-python> <B|C|L|P>" >&2
    exit 2
fi
CHECKOUT=$1
PY=$2
GROUP=$3
case "$GROUP" in
    B) CLS=GroupB ;;
    C) CLS=GroupC ;;
    L) CLS=GroupL ;;
    P) CLS=GuardTests,ForwardTests,CopyTests ;;
    *) echo "group must be B, C, L or P" >&2; exit 2 ;;
esac

HERE=$(cd "$(dirname "$0")" && pwd)
BASE=$HERE
while [ "$BASE" != "/" ]; do
    case "$(basename "$BASE")" in hya-s6b-*) break ;; esac
    BASE=$(dirname "$BASE")
done
if [ "$BASE" = "/" ]; then
    echo "refusing: $HERE must be inside a hya-s6b-* temp directory" >&2
    exit 2
fi
if [ ! -f "$HERE/../s6/probe_s6.py" ]; then
    echo "refusing: the S6 harness must sit next to s6b ($HERE/../s6/probe_s6.py)" >&2
    exit 2
fi

ROOT="$BASE/root-$GROUP"
rm -rf -- "$ROOT"
mkdir -p "$ROOT/home/.hermes" "$ROOT/locks" "$ROOT/tmp"
cd "$ROOT"

TIMER=()
if [ -x /usr/bin/time ]; then TIMER=(/usr/bin/time -v); fi

# env -i: nothing from the caller's environment. S6_RSS_LIMIT_MB=450: the watchdog of the S6 harness exits 137
# above it. choom -n 1000 makes the kernel pick the probe, not the live gateway, if memory runs out.
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
    HERMES_EXEC_ASK=1 \
    S6_RSS_LIMIT_MB=450 \
    timeout 1500 nice -n 19 choom -n 1000 -- ${TIMER[@]+"${TIMER[@]}"} "$PY" -I -u -c '
import sys, runpy
checkout, here, classes = sys.argv[1:4]
sys.path.insert(0, checkout)
sys._hermes_pin_default_home = True
import hermes_bootstrap  # noqa: F401  (sets up the Hermes runtime site-packages)
sys.path.insert(0, here)
sys.argv = ["probe_s6b", "-v", *("probe_s6b." + c for c in classes.split(","))]
runpy.run_module("unittest", run_name="__main__", alter_sys=True)
' "$CHECKOUT" "$HERE" "$CLS"
