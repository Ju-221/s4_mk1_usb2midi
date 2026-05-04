#include "s4_generic_protocol.hpp"

#include <cstdint>
#include <iostream>
#include <string>
#include <vector>

using namespace std;

namespace {
int failures = 0;

void expect_true(bool condition, const string& name) {
    if (!condition) {
        cerr << "[FAIL] " << name << endl;
        ++failures;
    } else {
        cout << "[PASS] " << name << endl;
    }
}

void expect_eq_u8(uint8_t actual, uint8_t expected, const string& name) {
    if (actual != expected) {
        cerr << "[FAIL] " << name << " expected=" << static_cast<int>(expected)
             << " actual=" << static_cast<int>(actual) << endl;
        ++failures;
    } else {
        cout << "[PASS] " << name << endl;
    }
}

void test_name() {
    S4GenericProtocol protocol;
    expect_true(protocol.name() == "S4 generic raw protocol", "name() matches");
}

void test_first_packet_is_baseline() {
    S4GenericProtocol protocol(0, 0, InputDecodeMode::Byte);
    vector<uint8_t> packet = {0x00, 0x00, 0x00};
    const auto messages = protocol.decode_input(packet);
    expect_true(messages.empty(), "first packet emits no messages");
}

void test_byte_mode_emits_raw_unmapped_cc() {
    S4GenericProtocol protocol(0, 0, InputDecodeMode::Byte);

    vector<uint8_t> first = {0x00, 0x00};
    vector<uint8_t> second = {0x04, 0x00}; // v7 on byte0: 0 -> 2

    (void)protocol.decode_input(first);
    const auto messages = protocol.decode_input(second);

    expect_true(messages.size() == 1, "byte mode emits one message for one changed byte");
    if (messages.size() != 1 || messages[0].bytes.size() != 3) {
        return;
    }

    expect_eq_u8(messages[0].bytes[0], 0xB0, "byte mode status byte");
    expect_eq_u8(messages[0].bytes[1], 0x00, "byte mode controller index");
    expect_eq_u8(messages[0].bytes[2], 0x02, "byte mode value from v7");
}

void test_byte_deadzone_filters_small_change() {
    S4GenericProtocol protocol(0, 1, InputDecodeMode::Byte);

    vector<uint8_t> first = {0x00};
    vector<uint8_t> second = {0x02}; // v7 delta = 1, should be filtered by deadzone=1

    (void)protocol.decode_input(first);
    const auto messages = protocol.decode_input(second);

    expect_true(messages.empty(), "byte deadzone filters delta <= deadzone");
}

void test_bit_mode_emits_mapped_button() {
    S4GenericProtocol protocol(0, 0, InputDecodeMode::Bit);

    vector<uint8_t> first(8, 0x00);
    vector<uint8_t> second(8, 0x00);
    second[4] = 0x80; // mapped in table: byte4 bit7 -> CC20

    (void)protocol.decode_input(first);
    const auto messages = protocol.decode_input(second);

    expect_true(messages.size() == 1, "bit mode emits one mapped message for byte4 bit7 on");
    if (messages.size() != 1 || messages[0].bytes.size() != 3) {
        return;
    }

    expect_eq_u8(messages[0].bytes[0], 0xB0, "bit mode status byte");
    expect_eq_u8(messages[0].bytes[1], 20, "bit mode mapped CC for byte4 bit7");
    expect_eq_u8(messages[0].bytes[2], 127, "bit mode mapped on value");
}

void test_encode_output_sysex_passthrough() {
    S4GenericProtocol protocol;

    vector<uint8_t> sysex = {0xF0, 0x7D, 0x53, 0x34, 0x10, 0x11, 0x12, 0xF7};
    const auto out = protocol.encode_output(sysex);

    expect_true(out.has_value(), "encode_output accepts marked SysEx packet");
    if (!out.has_value()) {
        return;
    }
    expect_true(out->size() == 3, "encode_output strips wrapper and returns payload");
    if (out->size() != 3) {
        return;
    }

    expect_eq_u8((*out)[0], 0x10, "encode_output payload byte 0");
    expect_eq_u8((*out)[1], 0x11, "encode_output payload byte 1");
    expect_eq_u8((*out)[2], 0x12, "encode_output payload byte 2");
}

void test_encode_output_rejects_regular_midi() {
    S4GenericProtocol protocol;
    vector<uint8_t> midi = {0x90, 0x3C, 0x7F};

    const auto out = protocol.encode_output(midi);
    expect_true(!out.has_value(), "encode_output rejects non-SysEx feedback packet");
}
} // namespace

int main() {
    test_name();
    test_first_packet_is_baseline();
    test_byte_mode_emits_raw_unmapped_cc();
    test_byte_deadzone_filters_small_change();
    test_bit_mode_emits_mapped_button();
    test_encode_output_sysex_passthrough();
    test_encode_output_rejects_regular_midi();

    if (failures != 0) {
        cerr << "\nTest failures: " << failures << endl;
        return 1;
    }

    cout << "\nAll tests passed." << endl;
    return 0;
}
