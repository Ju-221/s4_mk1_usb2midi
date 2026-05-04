# S4 MK1 USB to MIDI

[![build](https://github.com/Ju-221/s4_mk1_usb2midi/actions/workflows/build.yml/badge.svg)](https://github.com/Ju-221/s4_mk1_usb2midi/actions/workflows/build.yml)

A standalone macOS Sonoma 14 (Intel/Apple Silicon) application that bridges the Traktor S4 MK1 USB controller to virtual CoreMIDI ports, letting you use it with any MIDI-capable software (Traktor, Mixxx, Ableton, etc.) without a driver. It behaves like any other generic midi device you connect to your DAW/DJ software and can be remapped as needed.

Heavily inspired by [Opa-/x1-mk1-usb2midi](https://github.com/Opa-/x1-mk1-usb2midi). Enough has changed in architecture, protocol handling, and tooling that this lives as its own repo rather than a fork.

> **Status: work in progress.** The generic bridge infrastructure is solid. The S4 MK1-specific protocol decoding (mapping, semantic controls, LED feedback) is the remaining gap.

## How the code works

The program runs in one of two modes selected on the command line: `probe` or `bridge`.

**Probe mode** opens the USB device, prints its descriptors, and optionally streams raw byte packets from an input endpoint to stdout. You can also point it at an output endpoint and have it write raw hex payloads — useful for brute-forcing LED state before the protocol is understood. Nothing else happens; no MIDI ports are created.

**Bridge mode** is the main loop. On startup it opens the USB device, calls `s4_mk1_init()` to perform any hardware handshake needed, and then creates two virtual CoreMIDI ports via `CoreMidiBridge`. From that point it runs two concurrent data paths:

- **USB → MIDI (input path).** The main thread reads raw USB HID reports in a tight loop. Each report is handed to `S4GenericProtocol::decode_input()`, which compares it byte-by-byte against the previous report. For every byte that changed, the protocol produces one or more MIDI CC messages — either from an explicit mapping table you fill in, or from a generic fallback that maps each changed byte or bit to a unique CC number so Traktor/Mixxx can learn it. Those CC messages are then sent out on the virtual MIDI output port.

- **MIDI → USB (output/feedback path).** A CoreMIDI callback fires on a background thread whenever an incoming MIDI message arrives on the feedback port (e.g. from Traktor telling an LED to turn on). That message is passed to `S4GenericProtocol::encode_output()`. Currently the only thing it understands is a raw SysEx packet (`F0 7D 53 34 ... F7`), which it writes verbatim to the USB output endpoint. All other MIDI messages are ignored with a one-time warning until the LED protocol is reverse-engineered.

If the USB device disconnects at runtime, the bridge detects it, prints a message, and retries the open + init sequence every second until the device comes back — without crashing or requiring a restart.

The `running` flag is a global `atomic_bool` set to `false` by SIGINT/SIGTERM (`Ctrl-C`), which causes both loops to exit cleanly.

### What works now

- Talks to any USB HID/bulk device via `libusb`
- Creates virtual MIDI ports with CoreMIDI
- `probe` mode — inspect USB descriptors, stream raw reports, and write raw payloads to output endpoints
- `bridge` mode — translates raw USB report changes into learnable MIDI CC messages (bit-change, byte-value, or hybrid decode modes)
- `S4GenericProtocol` — configurable deadzone, base channel, and decode mode; unit-tested without hardware
- Raw LED USB packet injection over MIDI SysEx for testing output before the LED protocol is decoded
- Python GUI (`tools/midi_dev_tester.py`) for live MIDI traffic monitoring and rule-based mapping

### What is not done yet

- Decode the Traktor S4 MK1 HID report format into named, semantic controls (knobs, buttons, faders)
- Implement `S4Mk1Protocol` to replace `S4GenericProtocol` for production use
- Encode MIDI feedback from Traktor/Mixxx into native S4 LED packets
- Restore / re-enumerate the S4's built-in audio interface after USB detach

## Build

Built with clang C++20. Requires the following brew packages:

```bash
brew install cmake pkg-config libusb
```

Then build:

```bash
sh build.sh
```

Or manually with CMake:

```bash
cmake -S . -B build
cmake --build build
```

The binary lands at `bin/s4_mk1_usb2midi` (also copied to `build/`).

## Lint And Tests

## Lint and Tests

No hardware required. `S4GenericProtocol` has unit tests that run entirely in software.

```bash
./lint.sh       # clang warnings + optional clang-tidy
./run_tests.sh  # builds and runs test_s4_generic_protocol
```

Or via CMake directly:

```bash
cmake -S . -B build
cmake --build build --target lint
ctest --test-dir build --output-on-failure
```

## Debugging Tools

Three tools are available for iterative hardware debugging, all usable without writing any protocol code first.

### 1 — Probe Mode

Use this first. Discovers USB descriptors and streams raw reports so you can identify the right interface, endpoint, and report size for the S4.

```bash
# Print descriptors only
./build/s4_mk1_usb2midi probe --vid 0x17cc --pid 0x0000

# Stream incoming HID reports
./build/s4_mk1_usb2midi probe --vid 0x17cc --pid 0x0000 \
  --interface 3 --in-ep 0x81 --report-size 64 --stream
```

You can also probe output endpoints by sending raw payloads and optionally reading the response:

```bash
./build/s4_mk1_usb2midi probe \
  --vid 0x17cc \
  --pid 0xbaff \
  --interface 0 \
  --alt-setting 1 \
  --in-ep 0x84 \
  --out-ep 0x01 \
  --report-size 512 \
  --write-hex "05 05 05 05 05 05 05 05" \
  --write-repeat 10 \
  --write-interval-ms 50 \
  --stream
```

`--stream` causes probe mode to attempt one input read after each write and print `RX ...` when data comes back.

### 2 — Bridge Mode

Starts the virtual MIDI ports and begins translating USB reports into MIDI CC messages. Use this once you have the right USB parameters from probe.

```bash
./build/s4_mk1_usb2midi bridge \
  --vid 0x17cc \
  --pid 0x0000 \
  --interface 3 \
  --alt-setting 0 \
  --in-ep 0x81 \
  --out-ep 0x01 \
  --report-size 64 \
  --midi-channel 1
```

Creates two virtual MIDI ports:

| Port | Direction | Purpose |
|---|---|---|
| `S4 MK1 USB2MIDI` | OUT (to DAW) | controller → MIDI CC |
| `S4 MK1 USB2MIDI Feedback` | IN (from DAW) | MIDI → USB LED writes |

The bridge emits two parallel MIDI views of every raw report:

- **bit-change view** — each bit flip becomes a CC message on channels 1–16
- **byte-value view** — each byte is a CC value starting at the configured base channel

All CC values are clamped to 7-bit, so Traktor/Mixxx can learn them with no extra filtering.

### 3 — MIDI Dev Tester GUI

[tools/midi_dev_tester.py](tools/midi_dev_tester.py) is a lightweight Python/Tkinter GUI for real-time traffic inspection and rule-based mapping — useful for identifying which MIDI message corresponds to which physical control without touching the C++ code.

Install dependencies:

```bash
python3 -m pip install -r tools/requirements-midi-dev.txt
```

Run:

```bash
python3 tools/midi_dev_tester.py
```

Typical workflow:

1. Start the bridge binary.
2. In the GUI, select MIDI input `S4 MK1 USB2MIDI` and MIDI output `S4 MK1 USB2MIDI Feedback`.
3. Press **Connect**, then move one physical control at a time and watch which CC fires.
4. Add mapping rules (incoming key → outgoing message) and use the manual sender to test LED writes.

## Raw LED Packet Injection

Until the LED protocol is fully decoded, you can write arbitrary USB output packets from any MIDI tool via SysEx:

```text
F0 7D 53 34 <raw usb payload bytes...> F7
```

`7D 53 34` is the private SysEx manufacturer ID used by this bridge. We recorded via wireshark the setup packets sent out to the s4 and replay them at initialization to allow us to let the s4 to interact with the software.

## Next Steps

The generic infrastructure is complete. What remains is protocol-specific work:

1. **Identify the S4 MK1 USB parameters** — VID, PID, interface number, endpoint addresses, and report size — using `probe` mode and tools like Wireshark with USBPcap or `usbmon` on Linux.
2. **Map the HID report layout** — run `bridge --stream` while moving one control at a time; use the MIDI Dev Tester GUI to record which bytes/bits change.
3. **Implement `S4Mk1Protocol`** — a concrete `Protocol` subclass (see `include/protocol.hpp`) that replaces `S4GenericProtocol` with stable named control IDs decoded from the report, and encodes MIDI feedback back into S4 LED packets.
4. **Restore the audio interface** — the S4 has a built-in audio interface that may need re-enumeration after the bridge claims the USB device.
5. **Verfy the LED mapping** - we may think that the native instruments protocol is just a rewrap of i2c but honestly I could be wrong with the assumption. Let's check if once we verify the LEDs it adds up to the logic. (thanks Alex)
