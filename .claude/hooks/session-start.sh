#!/bin/bash
# Install the SiftStack skill library into .claude/skills/ for this project, so every
# session and every agent working in this repo (cloud or local) starts with all
# current skills available. Source is this checkout (install.py's local path), so it
# needs no network.
set -euo pipefail

cd "${CLAUDE_PROJECT_DIR:-.}"

# Reinstall only when the installer, the manifest or any skill/plugin source changed.
# install.py alone skips a package whose version marker matches, which would miss an
# edit made without a version bump, so a change here reinstalls with --force.
hashes=.claude/skills/.session-start.sha256
digest() {
  { sha256sum .claude/hooks/session-start.sh install.py skills/manifest.json
    git ls-files -s skills plugins; } | sha256sum
}
if [ "$(cat "$hashes" 2>/dev/null)" != "$(digest)" ]; then
  rm -f "$hashes"
  python3 install.py --dest .claude/skills --force
  # Superseded packages compete with their replacement for the same request, so
  # remove any the manifest marks superseded (left over from an earlier --all).
  python3 -c '
import json, shutil
m = json.load(open("skills/manifest.json"))
for e in m["skills"]:
    if e.get("superseded") or e.get("superseded_by"):
        shutil.rmtree(".claude/skills/" + e["name"], ignore_errors=True)
'
  digest > "$hashes"
fi
