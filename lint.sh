#!/bin/sh

set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if ! command -v pkg-config >/dev/null 2>&1; then
  echo "pkg-config is required for linting." >&2
  exit 1
fi

CXX=${CXX:-clang++}
LIBUSB_CFLAGS=$(pkg-config --cflags libusb-1.0 | sed 's/-I/-isystem /g')
BASE_FLAGS="-std=c++20 -Wall -Wextra -Wpedantic -Wno-unqualified-std-cast-call -I$ROOT_DIR/include $LIBUSB_CFLAGS"

echo "[lint] Syntax + warning checks"
for file in "$ROOT_DIR"/src/*.cpp "$ROOT_DIR"/tests/*.cpp; do
  echo "[lint] $file"
  "$CXX" $BASE_FLAGS -fsyntax-only "$file"
done

if command -v clang-tidy >/dev/null 2>&1; then
  echo "[lint] clang-tidy (best-effort)"
  for file in "$ROOT_DIR"/src/main.cpp "$ROOT_DIR"/src/s4_generic_protocol.cpp "$ROOT_DIR"/tests/test_s4_generic_protocol.cpp; do
    echo "[lint] clang-tidy $file"
    clang-tidy "$file" -- $BASE_FLAGS || true
  done
else
  echo "[lint] clang-tidy not found; skipped (syntax/warning checks still passed)."
fi

echo "[lint] done"
