#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_DIR="${1:-/tmp/auto-agents-codex-demo}"
IDEA_SOURCE="$ROOT_DIR/examples/codex_demo/idea.md"
IDEA_TARGET="$PROJECT_DIR/idea.md"

if ! command -v codex >/dev/null 2>&1; then
  echo "codex CLI was not found in PATH" >&2
  exit 1
fi

rm -rf "$PROJECT_DIR"
python3 -m auto_agents init --project "$PROJECT_DIR" --name codex-demo --provider codex >/dev/null
cp "$IDEA_SOURCE" "$IDEA_TARGET"

python3 - "$PROJECT_DIR" <<'PY'
import json
import sys
from pathlib import Path

project_dir = Path(sys.argv[1])
config_path = project_dir / ".auto-agents" / "config.json"
data = json.loads(config_path.read_text(encoding="utf-8"))
data["gates"]["commands"] = ["python3 -m unittest discover -s tests"]
config_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

python3 -m auto_agents validate --project "$PROJECT_DIR"
python3 -m auto_agents run \
  --project "$PROJECT_DIR" \
  --idea-file "$IDEA_TARGET" \
  --auto-approve \
  --max-tasks 1

echo
echo "Git log:"
git -C "$PROJECT_DIR" log --oneline --decorate

echo
echo "Git status:"
git -C "$PROJECT_DIR" status --short
