#!/usr/bin/env bash
# Builds the rules engine (ygopro-core) into a shared library:
#   build/libocgcore.so   (Linux)
#   build/libocgcore.dylib (macOS)
#
# Needs: g++ or clang++ with C++17. No other dependencies.
# Usage: scripts/build_engine.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CORE="$ROOT/vendor/ygopro-core"
OUT="$ROOT/build"
OBJ="$OUT/obj"
CXX="${CXX:-g++}"
JOBS="${JOBS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)}"

if [ ! -f "$CORE/ocgapi.h" ] || [ ! -f "$CORE/lua/src/lapi.c" ]; then
  echo "Engine source missing. Run scripts/fetch_vendor.sh first." >&2
  exit 1
fi

mkdir -p "$OBJ/lua" "$OBJ/core"

case "$(uname -s)" in
  Darwin) LIB="$OUT/libocgcore.dylib"; SHARED="-dynamiclib" ;;
  *)      LIB="$OUT/libocgcore.so";    SHARED="-shared" ;;
esac

# Lua must be compiled as C++ (the engine relies on C++ exceptions passing
# through Lua), with the engine's small config header put in front of each file.
LUA_FILES="lapi lauxlib lbaselib lcode lctype ldebug ldo ldump lfunc lgc liolib
llex lmathlib lmem lobject lopcodes lparser lstate lstring lstrlib ltable
ltablib ltm lundump lvm lzio"

for f in $LUA_FILES; do
  cat "$CORE/lua/luaconf-customize.h" "$CORE/lua/src/$f.c" > "$OBJ/lua/$f.cpp"
done

compile_lua() {
  "$CXX" -std=c++17 -O2 -fPIC -w -I"$CORE/lua/src" -c "$OBJ/lua/$1.cpp" -o "$OBJ/lua/$1.o"
}
compile_core() {
  local base
  base="$(basename "$1" .cpp)"
  "$CXX" -std=c++17 -O2 -fPIC -fno-rtti -fvisibility=hidden -w \
    -DOCGCORE_EXPORT_FUNCTIONS -I"$CORE" -I"$CORE/lua/src" \
    -c "$1" -o "$OBJ/core/$base.o"
}
export -f compile_lua compile_core
export CXX CORE OBJ

echo "==> compiling Lua"
printf '%s\n' $LUA_FILES | xargs -P "$JOBS" -I{} bash -c 'compile_lua {}'

echo "==> compiling ygopro-core"
CORE_SRC=("$CORE"/*.cpp)
if compgen -G "$CORE/RNG/*.cpp" > /dev/null; then
  CORE_SRC+=("$CORE"/RNG/*.cpp)
fi
printf '%s\n' "${CORE_SRC[@]}" | xargs -P "$JOBS" -I{} bash -c 'compile_core {}'

echo "==> linking $LIB"
"$CXX" $SHARED -o "$LIB" "$OBJ"/core/*.o "$OBJ"/lua/*.o

echo "Built $LIB"
