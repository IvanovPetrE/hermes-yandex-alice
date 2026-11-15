#!/usr/bin/env bash
# Run prototype S3 against an installed Hermes checkout, in a throwaway home.
#
#   run.sh <hermes-checkout> <hermes-python>
#
# <hermes-python> is the python of Hermes' committed dependency venv (see prototypes/s1/README.md:
# the store interpreter does not start with a throwaway HERMES_HOME). The probe directory must live
# in a fresh /tmp/hya-s3-XXXXXX directory; everything the run writes (HOME, HERMES_HOME with the
# default profile and profiles p2/p3, logs, TMPDIR) stays under it, so removing that directory is
# the full cleanup. The probe binds only ephemeral 127.0.0.1 ports (fake Bot API, proxy recorder,
# listener model) and never reaches Telegram: every send goes to extra.base_url on loopback.
set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "usage: $0 <hermes-checkout> <hermes-python>" >&2
    exit 2
fi
CHECKOUT=$1
PY=$2

HERE=$(cd "$(dirname "$0")" && pwd)
BASE=$(dirname "$HERE")
case "$(basename "$BASE")" in
    hya-s3-*) ;;
    *) echo "refusing: $HERE must be inside a hya-s3-* temp directory" >&2; exit 2 ;;
esac

ROOT="$BASE/root"
rm -rf -- "$ROOT"
mkdir -p "$ROOT/home/.hermes" "$ROOT/tmp"
cd "$ROOT"

TIMER=()
if [ -x /usr/bin/time ]; then TIMER=(/usr/bin/time -v); fi

# env -i: nothing from the caller's environment - no proxies, no HERMES_TELEGRAM_* overrides, no
# real tokens. The probe writes synthetic tokens into the profiles' .env files itself.
# S3_RSS_LIMIT_MB arms the probe's own RSS watchdog (exit 137 above the limit); choom -n 1000 makes
# the kernel pick the probe, not the live gateway, if memory still runs out.
exec env -i \
    PATH=/usr/local/bin:/usr/bin:/bin \
    LANG=C.UTF-8 \
    HOME="$ROOT/home" \
    HERMES_HOME="$ROOT/home/.hermes" \
    TMPDIR="$ROOT/tmp" \
    TZ=UTC \
    HERMES_DISABLE_LAZY_INSTALLS=1 \
    AWS_EC2_METADATA_DISABLED=true \
    S3_RSS_LIMIT_MB=300 \
    timeout 600 nice -n 19 choom -n 1000 -- ${TIMER[@]+"${TIMER[@]}"} "$PY" -I -u -c '
import sys, runpy
checkout, here = sys.argv[1:3]
sys.path.insert(0, checkout)
sys._hermes_pin_default_home = True
import hermes_bootstrap  # noqa: F401  (sets up the Hermes runtime site-packages)
sys.path.insert(0, here)
sys.argv = ["probe_s3", "-v", "probe_s3"]
runpy.run_module("unittest", run_name="__main__", alter_sys=True)
' "$CHECKOUT" "$HERE"
