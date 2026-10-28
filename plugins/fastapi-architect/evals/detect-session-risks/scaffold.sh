#!/bin/bash
# Copies fixtures/risky_app into the run workspace so sandboxed shell tools can read it.
# Runs only with --scaffold, as you, outside the agent sandbox.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
src="$here/fixtures/risky_app"
[ -d "$src" ] || { echo "scaffold: fixture not found at $src" >&2; exit 1; }
cp -R "$src" ./risky_app
echo "scaffold: copied risky_app ($(find ./risky_app -name '*.py' | wc -l | tr -d ' ') python files)"
