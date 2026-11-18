#!/usr/bin/env bash
# Тесты совместимости плагина с настоящим Hermes (этап 6, срез «б»), во временном HOME/HERMES_HOME.
#
#   run.sh <hermes-checkout> <hermes-python> <P|A|B>
#
# <hermes-python> — python закоммиченного окружения зависимостей Hermes
# (<HERMES_HOME>/installs/<key>/environments/<generation>/venv/bin/python на сервере; в CI — python venv после uv sync).
# Всё, что прогон пишет (HOME, HERMES_HOME, блокировки, TMPDIR), лежит в одном каталоге hya-compat-XXXXXX; он удаляется
# при выходе. Пропущенный тест — отказ (exit 1), как и ноль запущенных тестов.
set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "usage: $0 <hermes-checkout> <hermes-python> <P|A|B|G|Q|I|H|N|Z|L|R|T>" >&2
    exit 2
fi
CHECKOUT=$1
PY=$2
GROUP=$3
case "$GROUP" in
    P) CLS=test_compat_plugin ;;
    A) CLS=test_compat_gateway.GroupA ;;
    B) CLS=test_compat_gateway.GroupB ;;
    G) CLS=test_compat_gateway.GroupG ;;
    Q) CLS=test_compat_gateway.GroupQ ;;
    I) CLS=test_compat_gateway.GroupI ;;
    H) CLS=test_compat_history.GroupH ;;
    N) CLS=test_compat_history.GroupN ;;
    Z) CLS=test_compat_history.GroupZ ;;
    L) CLS=test_compat_restart.GroupL ;;
    R) CLS=test_compat_restart.GroupR ;;
    T) CLS=test_compat_approval.GroupT ;;
    *) echo "group must be P, A, B, G, Q, I, H, N, Z, L, R or T" >&2; exit 2 ;;
esac
[ -d "$CHECKOUT/gateway" ] || { echo "not a Hermes checkout: $CHECKOUT" >&2; exit 2; }
[ -x "$PY" ] || { echo "python not executable: $PY" >&2; exit 2; }

HERE=$(cd "$(dirname "$0")" && pwd)
CHECKOUT=$(cd "$CHECKOUT" && pwd)

ROOT=$(mktemp -d "${TMPDIR:-/tmp}/hya-compat-XXXXXX")
trap 'rm -rf -- "$ROOT"' EXIT
mkdir -p "$ROOT/home/.hermes" "$ROOT/locks" "$ROOT/tmp"
cd "$ROOT"

TIMER=()
if [ -x /usr/bin/time ]; then TIMER=(/usr/bin/time -v); fi
CHOOM=()
if command -v choom >/dev/null 2>&1; then CHOOM=(choom -n 1000 --); fi

# env -i: ничего из окружения вызывающего (ни настоящего HERMES_HOME, ни XDG_RUNTIME_DIR).
# choom -n 1000: при нехватке памяти ядро убьёт прогон, а не живой шлюз. -I -u: без PYTHON*, вывод без буфера.
env -i \
    PATH=/usr/local/bin:/usr/bin:/bin \
    LANG=C.UTF-8 \
    HOME="$ROOT/home" \
    HERMES_HOME="$ROOT/home/.hermes" \
    HERMES_GATEWAY_LOCK_DIR="$ROOT/locks" \
    TMPDIR="$ROOT/tmp" \
    TZ=UTC \
    HERMES_DISABLE_LAZY_INSTALLS=1 \
    AWS_EC2_METADATA_DISABLED=true \
    timeout 1200 nice -n 19 ${CHOOM[@]+"${CHOOM[@]}"} ${TIMER[@]+"${TIMER[@]}"} "$PY" -I -u -c '
import sys, unittest
checkout, here, name = sys.argv[1:4]
sys.path.insert(0, checkout)
sys._hermes_pin_default_home = True
import hermes_bootstrap  # noqa: F401  (рантайм-пакеты Hermes)
sys.path.insert(0, here)
suite = unittest.defaultTestLoader.loadTestsFromName(name)
result = unittest.TextTestRunner(verbosity=2).run(suite)
if result.testsRun == 0 or result.skipped or result.expectedFailures or not result.wasSuccessful():
    print(f"FAIL: run={result.testsRun} skipped={len(result.skipped)}", file=sys.stderr)
    sys.exit(1)
' "$CHECKOUT" "$HERE" "$CLS"
