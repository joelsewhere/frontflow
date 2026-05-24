#!/usr/bin/env bash
# Build the frontflow wheel + zip. Runs the test suite first; if
# any test fails, the build aborts.
#
# Usage: ./build.sh [--skip-tests]
#
# Outputs:
#   dist/frontflow-1.0.0-py3-none-any.whl
#   /mnt/user-data/outputs/frontflow-1.0.0-py3-none-any.whl
#   /mnt/user-data/outputs/frontflow-v1.zip
set -euo pipefail

cd "$(dirname "$0")"

SKIP_TESTS=0
for arg in "$@"; do
    case "$arg" in
        --skip-tests) SKIP_TESTS=1 ;;
        *) echo "unknown arg: $arg"; exit 1 ;;
    esac
done

VENV="${VENV:-/home/claude/.venv-workflow/bin}"
PY="$VENV/python"

if [ "$SKIP_TESTS" -eq 0 ]; then
    echo "==> Running test suite (use --skip-tests to bypass)..."
    # pytest config is in pyproject.toml; no PYTHONPATH gymnastics
    # needed. CI runs the same way (see .github/workflows/tests.yml).
    "$PY" -m pytest -q
    echo
fi

echo "==> Cleaning build artifacts..."
rm -rf dist build
find src -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

echo "==> Building wheel..."
"$PY" -m build --wheel 2>&1 | tail -1

echo "==> Publishing to /mnt/user-data/outputs/..."
mkdir -p /mnt/user-data/outputs
rm -f /mnt/user-data/outputs/frontflow-v1.zip
rm -f /mnt/user-data/outputs/frontflow-1.0.0-py3-none-any.whl
cp dist/frontflow-1.0.0-py3-none-any.whl /mnt/user-data/outputs/

find . -type d \( -name node_modules -o -name __pycache__ -o -name .git -o -name dist -o -name build -o -name .pytest_cache \) -prune -o -type f -print \
    | sed 's|^\./||' \
    | zip -q /mnt/user-data/outputs/frontflow-v1.zip -@

echo
echo "==> Done."
echo "    wheel: /mnt/user-data/outputs/frontflow-1.0.0-py3-none-any.whl"
echo "    zip:   /mnt/user-data/outputs/frontflow-v1.zip"
