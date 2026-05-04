#include "s4_generic_protocol.hpp"

#include <algorithm>
#include <iostream>
#include <optional>
#include <vector>

using namespace std;

namespace {
// Small MIDI helpers
MidiMessage make_cc(uint8_t channel, uint8_t controller, uint8_t value) {
    return MidiMessage{.bytes = {
        static_cast<uint8_t>(0xB0 | (channel & 0x0F)),
        static_cast<uint8_t>(controller & 0x7F),
        static_cast<uint8_t>(value & 0x7F),
    }};
}

// ---------------------------------------------------------------------------
// EDIT HERE: S4 -> MIDI mapping tables
// ---------------------------------------------------------------------------
// midi_channel is 1-16.
// cc is 0-127.
// For byte mappings: value comes from (raw_byte >> 1) & 0x7F.
// For bit mappings: value is 127 for ON, 0 for OFF (or inverted when needed).
//
// Tip: keep kEmitUnmapped* = true while discovering, then set false once your
// custom map is complete.

struct ByteMapEntry {
    size_t byte_index;
    uint8_t midi_channel;
    uint8_t cc;
    int deadzone_override; // -1 uses global --input-deadzone
};

struct BitMapEntry {
    size_t byte_index;
    uint8_t bit_index;
    uint8_t midi_channel;
    uint8_t cc;
    bool invert;
};

static constexpr bool kEmitUnmappedByte = true;
static constexpr bool kEmitUnmappedBit = true;

static const vector<ByteMapEntry> kByteMappings = {
    // Example format:
    // { 83, 1, 10, 2 }, // byte 83 -> ch1 cc10, deadzone 2
};

static const vector<BitMapEntry> kBitMappings = {
    // Left deck buttons (provisional from your capture order: Shift/Sync/Cue/Play)
    {4, 7, 1, 20, false}, // Shift L
    {4, 5, 1, 21, false}, // Sync L
    {4, 3, 1, 22, false}, // Cue L
    {4, 1, 1, 23, false}, // Play L

    // Candidate extra left-deck toggle seen frequently during tests.
    {7, 3, 1, 24, false},
};

optional<ByteMapEntry> find_byte_mapping(size_t byte_index) {
    for (const auto& entry : kByteMappings) {
        if (entry.byte_index == byte_index) {
            return entry;
        }
    }
    return nullopt;
}

optional<BitMapEntry> find_bit_mapping(size_t byte_index, uint8_t bit_index) {
    for (const auto& entry : kBitMappings) {
        if (entry.byte_index == byte_index && entry.bit_index == bit_index) {
            return entry;
        }
    }
    return nullopt;
}

uint8_t ch1_to_status_nibble(uint8_t channel_1_based, uint8_t fallback_zero_based) {
    if (channel_1_based >= 1 && channel_1_based <= 16) {
        return static_cast<uint8_t>(channel_1_based - 1);
    }
    return fallback_zero_based;
}
} // namespace

S4GenericProtocol::S4GenericProtocol(uint8_t base_channel,
                                     uint8_t input_deadzone,
                                     InputDecodeMode decode_mode)
    : base_channel_(static_cast<uint8_t>(base_channel & 0x0F)),
      input_deadzone_(static_cast<uint8_t>(min<uint8_t>(input_deadzone, 127))),
      decode_mode_(decode_mode) {}

string S4GenericProtocol::name() const {
    return "S4 generic raw protocol";
}

vector<MidiMessage> S4GenericProtocol::decode_input(span<const uint8_t> report) {
    vector<MidiMessage> messages;

    // First packet becomes the baseline for future delta comparisons.
    if (previous_report_.empty()) {
        previous_report_.assign(report.begin(), report.end());
        return messages;
    }

    const size_t byte_count = min(previous_report_.size(), report.size());

    for (size_t byte_index = 0; byte_index < byte_count; ++byte_index) {
        const uint8_t previous = previous_report_[byte_index];
        const uint8_t current = report[byte_index];
        if (previous == current) {
            continue;
        }

        // diff:  bitmask of which bits flipped (used by the BIT view below).
        // >> 1 drops the lowest status bit; & 0x7F clamps to MIDI range (0-127).
        // delta: absolute movement distance, used to filter out deadzone noise.
        // raw_value: the current position to send as a MIDI CC value.
        const uint8_t diff = static_cast<uint8_t>(previous ^ current);
        const uint8_t previous_value = static_cast<uint8_t>((previous >> 1) & 0x7F);
        const uint8_t current_value = static_cast<uint8_t>((current >> 1) & 0x7F);
        const uint8_t delta = static_cast<uint8_t>(
            previous_value > current_value ? (previous_value - current_value) : (current_value - previous_value));
        const uint8_t raw_value = current_value;

        // BYTE view: good for knobs/faders (0-127 value stream)
        if (decode_mode_ == InputDecodeMode::Byte || decode_mode_ == InputDecodeMode::Hybrid) {

            bool emitted = false;

            if (const auto mapped = find_byte_mapping(byte_index); mapped.has_value()) {
                const int dz =
                    mapped->deadzone_override >= 0 ? mapped->deadzone_override : static_cast<int>(input_deadzone_);
                if (delta > static_cast<uint8_t>(max(0, min(127, dz)))) {
                    const uint8_t channel = ch1_to_status_nibble(mapped->midi_channel, base_channel_);
                    messages.push_back(make_cc(channel, mapped->cc, raw_value));
                    emitted = true;
                }
            }

            // Fallback raw mapping while discovery is still in progress.
            if (!emitted && kEmitUnmappedByte) {
                if (delta > input_deadzone_) {
                    const uint16_t raw_cc_index = static_cast<uint16_t>(byte_index);
                    const uint8_t raw_channel = static_cast<uint8_t>((base_channel_ + (raw_cc_index / 128)) & 0x0F);
                    const uint8_t raw_controller = static_cast<uint8_t>(raw_cc_index % 128);
                    messages.push_back(make_cc(raw_channel, raw_controller, raw_value));
                }
            }
        }

        // BIT view: good for buttons/toggles (on/off events)
        if (decode_mode_ == InputDecodeMode::Bit || decode_mode_ == InputDecodeMode::Hybrid) {
            for (uint8_t bit = 0; bit < 8; ++bit) {
                if (((diff >> bit) & 0x01) == 0) {
                    continue;
                }

                bool emitted = false;
                const bool is_on = ((current >> bit) & 0x01) != 0;
                if (const auto mapped = find_bit_mapping(byte_index, bit); mapped.has_value()) {
                    const uint8_t channel = ch1_to_status_nibble(mapped->midi_channel, base_channel_);
                    const bool final_on = mapped->invert ? !is_on : is_on;
                    const uint8_t bit_value = final_on ? 127 : 0;
                    messages.push_back(make_cc(channel, mapped->cc, bit_value));
                    emitted = true;
                }

                // Fallback raw mapping while discovery is still in progress.
                if (!emitted && kEmitUnmappedBit) {
                    const uint16_t bit_index = static_cast<uint16_t>(byte_index * 8 + bit);
                    const uint8_t bit_channel = static_cast<uint8_t>((base_channel_ + (bit_index / 128)) & 0x0F);
                    const uint8_t bit_controller = static_cast<uint8_t>(bit_index % 128);
                    const uint8_t bit_value = is_on ? 127 : 0;
                    messages.push_back(make_cc(bit_channel, bit_controller, bit_value));
                }
            }
        }
    }

    previous_report_.assign(report.begin(), report.end());
    return messages;
}

optional<vector<uint8_t>> S4GenericProtocol::encode_output(span<const uint8_t> midi_message) {
    if (midi_message.size() >= 5 && midi_message.front() == 0xF0 && midi_message.back() == 0xF7) {
        if (midi_message[1] == 0x7D && midi_message[2] == 0x53 && midi_message[3] == 0x34) {
            return vector<uint8_t>(midi_message.begin() + 4, midi_message.end() - 1);
        }
    }

    if (!warned_about_led_protocol_) {
        cerr << "Ignoring incoming MIDI feedback until the S4 LED protocol is implemented."
             << " Use SysEx F0 7D 53 34 ... F7 to inject raw USB LED payloads." << endl;
        warned_about_led_protocol_ = true;
    }
    return nullopt;
}