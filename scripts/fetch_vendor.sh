#!/usr/bin/env bash
# Downloads the open source code and data this project runs on into vendor/.
# Nothing in vendor/ is committed to this repo. It is fetched fresh.
#
#   ygopro-core  (edo9300)        rules engine, AGPL-3.0-or-later
#   CardScripts  (Project Ignis)  per-card Lua logic, AGPL-3.0-or-later
#   BabelCDB     (Project Ignis)  card database (sqlite .cdb files)
#
# Usage: scripts/fetch_vendor.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR="$ROOT/vendor"
mkdir -p "$VENDOR"

# Pinned commits. These are the versions the tests were last run against.
# Bump them on purpose, then re-run the tests.
CORE_URL="https://github.com/edo9300/ygopro-core.git"
CORE_REF="c2e0b697e031d3560c077c9cd2cc76474969dedf"
SCRIPTS_URL="https://github.com/ProjectIgnis/CardScripts.git"
SCRIPTS_REF="7e91edbb481dd6bcbba703e33c4f40840f3b08e0"
CDB_URL="https://github.com/ProjectIgnis/BabelCDB.git"
CDB_REF="268b4e2d720f4c3e1e75277a0da2d37b541c4715"

fetch() {
  local name="$1" url="$2" ref="$3"
  local dir="$VENDOR/$name"
  if [ ! -d "$dir/.git" ]; then
    git init -q "$dir"
    git -C "$dir" remote add origin "$url"
  fi
  echo "==> $name @ ${ref:0:10}"
  git -C "$dir" fetch -q --depth 1 origin "$ref"
  git -C "$dir" checkout -q FETCH_HEAD
}

fetch ygopro-core "$CORE_URL" "$CORE_REF"
# The engine bundles Lua as a git submodule (lua/src).
git -C "$VENDOR/ygopro-core" submodule update -q --init --depth 1

fetch CardScripts "$SCRIPTS_URL" "$SCRIPTS_REF"
fetch BabelCDB "$CDB_URL" "$CDB_REF"

echo "Done. Next: scripts/build_engine.sh"
