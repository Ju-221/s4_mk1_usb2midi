#include "core_midi_bridge.hpp"
#include "s4_generic_protocol.hpp"
#include "s4_init.hpp"
#include "usb_device.hpp"

#include <atomic>
#include <csignal>
#include <cstdint>
#include <chrono>
#include <iomanip>
#include <iostream>
#include <optional>
#include <bit>
#include <span>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

using namespace std;

namespace {
// Global state and types
atomic_bool running = true;

enum class Mode {
    Probe,
    Bridge,
};

struct AppOptions {
    Mode mode = Mode::Probe;
    bool stream = false;
    UsbOptions usb;
    uint8_t midi_base_channel = 0;
    uint8_t input_deadzone = 0;
    InputDecodeMode input_decode_mode = InputDecodeMode::Byte;
    bool input_debug = false;
    bool input_debug_buttons_only = false;
    vector<uint8_t> write_payload;
    int write_repeat = 1;
    int write_interval_ms = 50;
};


// Forward declarations
AppOptions parse_args(int argc, char** argv);
void print_usage();


void print_input_deltas(span<const uint8_t> previous, span<const uint8_t> current, bool buttons_only);
InputDecodeMode parse_decode_mode(const string& text);

uint16_t parse_u16(const string& text);
uint8_t parse_u8(const string& text);
int parse_i32(const string& text);

vector<uint8_t> parse_hex_bytes(const string& text);

int run_probe(const AppOptions& options);
int run_bridge(const AppOptions& options);
void handle_signal(int);
string hex_dump(span<const uint8_t> bytes);
} 


int main(int argc, char** argv) {
    signal(SIGINT, handle_signal);
    signal(SIGTERM, handle_signal);

    try {
        const AppOptions options = parse_args(argc, argv);
        switch (options.mode) {
            case Mode::Probe:
                return run_probe(options);
            case Mode::Bridge:
                return run_bridge(options);
        }
    } catch (const exception& error) {
        cerr << error.what() << endl;
        print_usage();
        return 1;
    }

    // Unreachable in normal flow, but keeps all compilers happy for switch exhaustiveness.
    return 1;
}

namespace {
// Implementations

void handle_signal(int) {
    running = false;
}

string hex_dump(span<const uint8_t> bytes) {
    ostringstream stream;
    stream << hex << setfill('0');
    for (size_t index = 0; index < bytes.size(); ++index) {
        if (index != 0) {
            stream << ' ';
        }
        stream << setw(2) << static_cast<int>(bytes[index]);
    }
    return stream.str();
}

void print_input_deltas(span<const uint8_t> previous, span<const uint8_t> current, bool buttons_only) {
    const size_t byte_count = min(previous.size(), current.size());

    size_t changed_bytes = 0;
    for (size_t byte_index = 0; byte_index < byte_count; ++byte_index) {
        if (previous[byte_index] != current[byte_index]) {
            ++changed_bytes;
        }
    }

    // Button-focused mode: only emit small, discrete transitions.
    if (buttons_only && changed_bytes > 2) {
        return;
    }

    for (size_t byte_index = 0; byte_index < byte_count; ++byte_index) {
        const uint8_t prev = previous[byte_index];
        const uint8_t cur = current[byte_index];
        const uint8_t diff = static_cast<uint8_t>(prev ^ cur);
        if (diff == 0) {
            continue;
        }

        if (buttons_only && std::popcount(diff) != 1) {
            continue;
        }

        cout << (buttons_only ? "[btn] " : "[raw] ")
             << "byte=" << byte_index
             << " prev=0x" << hex << setw(2) << setfill('0') << static_cast<int>(prev)
             << " cur=0x" << setw(2) << static_cast<int>(cur) << dec
             << " v7=" << static_cast<int>((prev >> 1) & 0x7F)
             << "->" << static_cast<int>((cur >> 1) & 0x7F)
             << " bits=";

        bool first = true;
        for (uint8_t bit = 0; bit < 8; ++bit) {
            if (((diff >> bit) & 0x01) == 0) {
                continue;
            }
            if (!first) {
                cout << ',';
            }
            first = false;
            cout << 'b' << static_cast<int>(bit) << '=' << (((cur >> bit) & 0x01) ? '1' : '0');
        }
        cout << endl;
    }
}

InputDecodeMode parse_decode_mode(const string& text) {
    if (text == "byte") {
        return InputDecodeMode::Byte;
    }
    if (text == "bit") {
        return InputDecodeMode::Bit;
    }
    if (text == "hybrid") {
        return InputDecodeMode::Hybrid;
    }
    throw runtime_error("--input-decode must be one of: byte, bit, hybrid");
}

uint16_t parse_u16(const string& text) {
    return static_cast<uint16_t>(stoul(text, nullptr, 0));
}

uint8_t parse_u8(const string& text) {
    return static_cast<uint8_t>(stoul(text, nullptr, 0));
}

int parse_i32(const string& text) {
    return stoi(text, nullptr, 0);
}

vector<uint8_t> parse_hex_bytes(const string& text) {
    vector<uint8_t> bytes;
    istringstream stream(text);
    string token;
    while (stream >> token) {
        const unsigned long value = stoul(token, nullptr, 16);
        if (value > 0xFF) {
            throw runtime_error("Hex byte out of range in --write-hex: " + token);
        }
        bytes.push_back(static_cast<uint8_t>(value));
    }
    if (bytes.empty()) {
        throw runtime_error("--write-hex requires at least one byte, example: \"00 7f ff\"");
    }
    return bytes;
}

void print_usage() {
    cout << "Usage:\n"
         << "  s4_mk1_usb2midi probe --vid <id> --pid <id> [--interface N --in-ep EP --report-size N --stream] [--out-ep EP --write-hex \"aa bb cc\" --write-repeat N --write-interval-ms N]\n"
         << "  s4_mk1_usb2midi bridge --vid <id> --pid <id> --interface N --in-ep EP --report-size N [--out-ep EP] [--alt-setting N] [--midi-channel 1-16] [--input-deadzone 0-127] [--input-decode byte|bit|hybrid] [--input-debug] [--input-debug-buttons]\n";
}

AppOptions parse_args(int argc, char** argv) {
    // Command shape: s4_mk1_usb2midi <mode> [flags...]
    if (argc < 2) {
        throw runtime_error("Missing mode");
    }

    AppOptions options;
    const string mode = argv[1];
    if (mode == "probe") {
        options.mode = Mode::Probe;
    } else if (mode == "bridge") {
        options.mode = Mode::Bridge;
    } else {
        throw runtime_error("Unknown mode: " + mode);
    }

    for (int index = 2; index < argc; ++index) {
        const string flag = argv[index];
        const auto read_next_value = [&](const string& expected_flag) -> string {
            if (index + 1 >= argc) {
                throw runtime_error("Missing value for " + expected_flag);
            }
            ++index;
            return argv[index];
        };

        if (flag == "--vid") {
            options.usb.vendor_id = parse_u16(read_next_value(flag));
        } else if (flag == "--pid") {
            options.usb.product_id = parse_u16(read_next_value(flag));
        } else if (flag == "--interface") {
            options.usb.interface_number = parse_i32(read_next_value(flag));
        } else if (flag == "--alt-setting") {
            options.usb.alternate_setting = parse_i32(read_next_value(flag));
        } else if (flag == "--in-ep") {
            options.usb.input_endpoint = parse_u8(read_next_value(flag));
        } else if (flag == "--out-ep") {
            options.usb.output_endpoint = parse_u8(read_next_value(flag));
        } else if (flag == "--report-size") {
            options.usb.report_size = parse_i32(read_next_value(flag));
        } else if (flag == "--timeout-ms") {
            options.usb.timeout_ms = static_cast<unsigned int>(parse_i32(read_next_value(flag)));
        } else if (flag == "--stream") {
            options.stream = true;
        } else if (flag == "--write-hex") {
            options.write_payload = parse_hex_bytes(read_next_value(flag));
        } else if (flag == "--write-repeat") {
            options.write_repeat = parse_i32(read_next_value(flag));
            if (options.write_repeat <= 0) {
                throw runtime_error("--write-repeat must be > 0");
            }
        } else if (flag == "--write-interval-ms") {
            options.write_interval_ms = parse_i32(read_next_value(flag));
            if (options.write_interval_ms < 0) {
                throw runtime_error("--write-interval-ms must be >= 0");
            }
        } else if (flag == "--midi-channel") {
            const int channel = parse_i32(read_next_value(flag));
            if (channel < 1 || channel > 16) {
                throw runtime_error("--midi-channel must be in range 1-16");
            }
            options.midi_base_channel = static_cast<uint8_t>(channel - 1);
        } else if (flag == "--input-deadzone") {
            const int deadzone = parse_i32(read_next_value(flag));
            if (deadzone < 0 || deadzone > 127) {
                throw runtime_error("--input-deadzone must be in range 0-127");
            }
            options.input_deadzone = static_cast<uint8_t>(deadzone);
        } else if (flag == "--input-decode") {
            options.input_decode_mode = parse_decode_mode(read_next_value(flag));
        } else if (flag == "--input-debug") {
            options.input_debug = true;
        } else if (flag == "--input-debug-buttons") {
            options.input_debug = true;
            options.input_debug_buttons_only = true;
        } else {
            throw runtime_error("Unknown flag: " + flag);
        }
    }

    if (options.usb.vendor_id == 0 || options.usb.product_id == 0) {
        throw runtime_error("Both --vid and --pid are required");
    }
    if (options.mode == Mode::Bridge) {
        if (options.usb.input_endpoint == 0 || options.usb.report_size <= 0) {
            throw runtime_error("Bridge mode requires --in-ep and --report-size");
        }
    }

    return options;
}

int run_probe(const AppOptions& options) {
    // Probe mode helps inspect descriptors and optional read/write behavior.
    UsbDevice device(options.usb);
    device.print_descriptors();

    if (!options.write_payload.empty()) {
        if (options.usb.output_endpoint == 0) {
            throw runtime_error("--write-hex requires --out-ep");
        }
        device.open();
        cout << "Writing " << options.write_payload.size() << " bytes to out endpoint 0x"
             << hex << setw(2) << setfill('0') << static_cast<int>(options.usb.output_endpoint)
             << dec << " for " << options.write_repeat << " cycle(s)." << endl;

        for (int index = 0; index < options.write_repeat; ++index) {
            device.write_output(options.write_payload);
            if (options.stream) {
                const auto response = device.read_input();
                if (!response.empty()) {
                    cout << "RX " << hex_dump(response) << endl;
                }
            }
            if (index + 1 < options.write_repeat && options.write_interval_ms > 0) {
                this_thread::sleep_for(chrono::milliseconds(options.write_interval_ms));
            }
        }
        cout << "Write probe complete." << endl;
        return 0;
    }

    if (!options.stream) {
        return 0;
    }

    device.open();
    while (running) {
        const auto packet = device.read_input();
        if (!packet.empty()) {
            cout << hex_dump(packet) << endl;
        }
    }
    return 0;
}

int run_bridge(const AppOptions& options) {
    // Bridge mode: USB input -> protocol decode -> virtual MIDI out.
    S4GenericProtocol protocol(options.midi_base_channel,
                               options.input_deadzone,
                               options.input_decode_mode);
    UsbDevice device(options.usb);

    // Attempt to open+init, retrying every second until success or Ctrl-C.
    // Throws immediately (no retry) if multiple matching devices are detected.
    auto connect = [&]() -> bool {
        while (running) {
            try {
                device.close();
                device.open();
                s4_mk1_init(device);
                return true;
            } catch (const exception& e) {
                const string msg = e.what();
                // Hard error: multiple devices — tell the user and stop.
                if (msg.find("Multiple") != string::npos) {
                    cerr << "[error] " << msg << endl;
                    running = false;
                    return false;
                }
                cerr << "[hotswap] " << msg << " — retrying in 1s..." << endl;
                this_thread::sleep_for(chrono::milliseconds(1000));
            }
        }
        return false;
    };

    if (!connect()) return 1;

    CoreMidiBridge midi("S4 MK1 USB2MIDI", "S4 MK1 USB2MIDI Feedback",
                        [&](const vector<uint8_t>& message) {
                            if (device.disconnected()) return;
                            const auto payload = protocol.encode_output(message);
                            if (!payload.has_value()) return;
                            device.write_output(*payload);
                        });

    cout << "Running bridge with protocol: " << protocol.name() << endl;

    vector<uint8_t> previous_debug_packet;

    while (running) {
        const auto packet = device.read_input();
        if (device.disconnected()) {
            cerr << "[hotswap] Device disconnected — waiting for reconnect..." << endl;
            if (!connect()) break;
            cerr << "[hotswap] Device reconnected." << endl;
            previous_debug_packet.clear();
            continue;
        }
        if (packet.empty()) continue;
        if (options.input_debug) {
            if (!previous_debug_packet.empty()) {
                print_input_deltas(previous_debug_packet, packet, options.input_debug_buttons_only);
            }
            previous_debug_packet = packet;
        }
        for (const auto& message : protocol.decode_input(packet)) {
            midi.send(message.bytes);
        }
    }

    return 0;
}
} // namespace