#pragma once

#include <cstdint>
#include <optional>
#include <span>
#include <string>
#include <vector>

#include "protocol.hpp"

using namespace std;

enum class InputDecodeMode {
    Byte,
    Bit,
    Hybrid,
};

class S4GenericProtocol final : public Protocol {
public:
    explicit S4GenericProtocol(uint8_t base_channel = 0,
                               uint8_t input_deadzone = 0,
                               InputDecodeMode decode_mode = InputDecodeMode::Byte);

    string name() const override;
    vector<MidiMessage> decode_input(span<const uint8_t> report) override;
    optional<vector<uint8_t>> encode_output(span<const uint8_t> midi_message) override;

private:
    uint8_t base_channel_ = 0;
    uint8_t input_deadzone_ = 0;
    InputDecodeMode decode_mode_ = InputDecodeMode::Byte;
    vector<uint8_t> previous_report_;
    bool warned_about_led_protocol_ = false;
};