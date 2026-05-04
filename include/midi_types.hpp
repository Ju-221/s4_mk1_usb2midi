#pragma once

#include <cstdint>
#include <vector>

using namespace std;

struct MidiMessage {
    vector<uint8_t> bytes;
};