#!/usr/bin/env bash
# run-gates.sh — the single quality-gate entry point, used by CI and locally.
# Runs the same checks in the same order as .github/workflows/ci.yml.
# Requires the uv-managed environment (uv sync) to exist.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> [1/4] ruff check"
uv run ruff check .

echo "==> [2/4] ruff format --check"
uv run ruff format --check .

echo "==> [3/4] mypy --strict (src/)"
uv run mypy src

# Coverage thresholds are configured in pyproject.toml (fail_under); this
# script only asks pytest to produce the term table and coverage.xml for CI.
# Hardware tests (tests/hardware/) self-skip without DEVKIT_TEST_DEVICE.
echo "==> [4/4] pytest with coverage"
uv run pytest --cov=steamos_devkit --cov-report=term-missing --cov-report=xml

echo "==> all gates passed"
