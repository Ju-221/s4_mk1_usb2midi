#pragma once

#include <cstdint>
#include <optional>
#include <span>
#include <string>
#include <vector>

#include "midi_types.hpp"

using namespace std;

class Protocol {
public:
    virtual ~Protocol() = default;

    virtual string name() const = 0;
    virtual vector<MidiMessage> decode_input(span<const uint8_t> report) = 0;
    virtual optional<vector<uint8_t>> encode_output(span<const uint8_t> midi_message) = 0;
};