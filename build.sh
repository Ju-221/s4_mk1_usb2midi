#!/bin/sh

set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BIN_DIR="$ROOT_DIR/bin"
OUTPUT="$BIN_DIR/s4_mk1_usb2midi"

mkdir -p "$BIN_DIR"

# Keep this build path pinned to C++20; reject external -std overrides.
if [ -n "${CXXFLAGS:-}" ] && printf '%s' "$CXXFLAGS" | grep -Eq '(^|[[:space:]])-std='; then
  echo "build.sh is pinned to -std=c++20; remove -std from CXXFLAGS." >&2
  exit 1
fi

LIBUSB_CFLAGS=$(pkg-config --cflags libusb-1.0)
LIBUSB_LIBS=$(pkg-config --libs libusb-1.0)

xcrun clang++ \
  -std=c++20 \
  -Wall \
  -Wextra \
  -Wpedantic \
  -I"$ROOT_DIR/include" \
  $LIBUSB_CFLAGS \
  "$ROOT_DIR/src/main.cpp" \
  "$ROOT_DIR/src/usb_device.cpp" \
  "$ROOT_DIR/src/core_midi_bridge.cpp" \
  "$ROOT_DIR/src/s4_generic_protocol.cpp" \
  "$ROOT_DIR/src/s4_init.cpp" \
  -framework CoreMIDI \
  -framework CoreFoundation \
  $LIBUSB_LIBS \
  -o "$OUTPUT"

echo "Built $OUTPUT"