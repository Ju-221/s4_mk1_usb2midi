#include "core_midi_bridge.hpp"

#include <CoreFoundation/CoreFoundation.h>

#include <array>
#include <iostream>
#include <stdexcept>

using namespace std;

namespace {
CFStringRef make_cf_string(const string& value) {
    return CFStringCreateWithCString(kCFAllocatorDefault, value.c_str(), kCFStringEncodingUTF8);
}
}

CoreMidiBridge::CoreMidiBridge(const string& source_name,
                               const string& destination_name,
                               ReceiveCallback callback)
    : callback_(move(callback)) {
    const CFStringRef client_name = make_cf_string("s4_mk1_usb2midi");
    if (MIDIClientCreate(client_name, nullptr, nullptr, &client_) != noErr) {
        CFRelease(client_name);
        throw runtime_error("Failed to create CoreMIDI client");
    }
    CFRelease(client_name);

    const CFStringRef source_cf_name = make_cf_string(source_name);
    if (MIDISourceCreate(client_, source_cf_name, &source_) != noErr) {
        CFRelease(source_cf_name);
        throw runtime_error("Failed to create CoreMIDI virtual source");
    }
    CFRelease(source_cf_name);

    const CFStringRef destination_cf_name = make_cf_string(destination_name);
    if (MIDIDestinationCreate(client_, destination_cf_name, &CoreMidiBridge::read_proc, this, &destination_) != noErr) {
        CFRelease(destination_cf_name);
        throw runtime_error("Failed to create CoreMIDI virtual destination");
    }
    CFRelease(destination_cf_name);
}

CoreMidiBridge::~CoreMidiBridge() {
    if (source_ != 0) {
        MIDIEndpointDispose(source_);
    }
    if (destination_ != 0) {
        MIDIEndpointDispose(destination_);
    }
    if (client_ != 0) {
        MIDIClientDispose(client_);
    }
}

void CoreMidiBridge::send(const vector<uint8_t>& message) const {
    if (message.empty()) {
        return;
    }

    vector<uint8_t> sanitized = message;
    const uint8_t status = sanitized[0];
    if ((status & 0x80) != 0 && status < 0xF0) {
        for (size_t index = 1; index < sanitized.size(); ++index) {
            sanitized[index] = static_cast<uint8_t>(sanitized[index] & 0x7F);
        }
    }

    array<uint8_t, 1024> buffer{};
    MIDIPacketList* packet_list = reinterpret_cast<MIDIPacketList*>(buffer.data());
    MIDIPacket* packet = MIDIPacketListInit(packet_list);
    packet = MIDIPacketListAdd(packet_list, buffer.size(), packet, 0, sanitized.size(), sanitized.data());
    if (packet == nullptr) {
        throw runtime_error("Failed to build CoreMIDI packet list");
    }

    if (MIDIReceived(source_, packet_list) != noErr) {
        throw runtime_error("Failed to publish MIDI packet");
    }
}

void CoreMidiBridge::read_proc(const MIDIPacketList* packet_list, void* read_proc_ref_con, void*) {
    auto* self = static_cast<CoreMidiBridge*>(read_proc_ref_con);
    if (self == nullptr || !self->callback_) {
        return;
    }

    const MIDIPacket* packet = &packet_list->packet[0];
    for (uint32_t index = 0; index < packet_list->numPackets; ++index) {
        self->callback_({packet->data, packet->data + packet->length});
        packet = MIDIPacketNext(packet);
    }
}