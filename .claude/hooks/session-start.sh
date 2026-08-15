#!/bin/bash
set -uo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

if ! pip install -r "$CLAUDE_PROJECT_DIR/requirements.txt"; then
  echo "warning: could not install all of requirements.txt." >&2
  echo "numpy requires pypi.org and files.pythonhosted.org to be allowed in this" >&2
  echo "environment's network egress settings. It's only needed for" >&2
  echo "cronometer_targets.calibrate() -- fetch_targets()/main() work without it." >&2
fi
