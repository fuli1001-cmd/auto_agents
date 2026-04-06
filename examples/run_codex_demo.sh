#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_DIR="${1:-/tmp/auto-agents-codex-demo}"
SPEC_SOURCE="$ROOT_DIR/examples/codex_demo/spec.md"
SPEC_TARGET="$PROJECT_DIR/spec.md"

if ! command -v codex >/dev/null 2>&1; then
  echo "codex CLI was not found in PATH" >&2
  exit 1
fi

rm -rf "$PROJECT_DIR"
python3 -m auto_agents init --project "$PROJECT_DIR" --name codex-demo >/dev/null
cp "$SPEC_SOURCE" "$SPEC_TARGET"

python3 -m auto_agents validate --project "$PROJECT_DIR"
python3 -m auto_agents run \
  --project "$PROJECT_DIR" \
  --spec-file "$SPEC_TARGET" \
  --provider codex \
  --auto-approve \
  --max-tasks 1

echo
echo "Git log:"
git -C "$PROJECT_DIR" log --oneline --decorate

echo
echo "Git status:"
git -C "$PROJECT_DIR" status --short
