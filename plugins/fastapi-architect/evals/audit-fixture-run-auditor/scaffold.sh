#!/bin/bash
# Scaffold for the audit-fixture-run-auditor case.
#
# claude plugin eval runs this only when --scaffold is passed. It starts in the
# run's empty workspace, outside the agent sandbox, with a minimal environment.
# Its only job is to copy the fixture project next to the agent so that
# sandboxed shell tools (confined to the working directory) can read it.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
src="$here/fixtures/sample_app"
if [ ! -d "$src" ]; then
  echo "scaffold: fixture not found at $src" >&2
  exit 1
fi
cp -R "$src" ./sample_app
echo "scaffold: copied sample_app ($(find ./sample_app -name '*.py' | wc -l | tr -d ' ') python files)"
