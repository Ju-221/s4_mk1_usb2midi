#!/bin/sh

set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BUILD_DIR="$ROOT_DIR/build"
TOOLS_DIR="$BUILD_DIR/tools"
OUTPUT="$TOOLS_DIR/test_s4_generic_protocol"

mkdir -p "$TOOLS_DIR"

xcrun clang++ \
  -std=c++20 \
  -Wall \
  -Wextra \
  -Wpedantic \
  -I"$ROOT_DIR/include" \
  "$ROOT_DIR/tools/tests/test_s4_generic_protocol.cpp" \
  "$ROOT_DIR/src/s4_generic_protocol.cpp" \
  -o "$OUTPUT"

"$OUTPUT"
