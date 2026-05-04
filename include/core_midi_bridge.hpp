#pragma once

#include <CoreMIDI/CoreMIDI.h>

#include <cstdint>
#include <functional>
#include <string>
#include <vector>

using namespace std;

class CoreMidiBridge {
public:
    using ReceiveCallback = function<void(const vector<uint8_t>&)>;

    CoreMidiBridge(const string& source_name, const string& destination_name, ReceiveCallback callback);
    ~CoreMidiBridge();

    CoreMidiBridge(const CoreMidiBridge&) = delete;
    CoreMidiBridge& operator=(const CoreMidiBridge&) = delete;

    void send(const vector<uint8_t>& message) const;

private:
    static void read_proc(const MIDIPacketList* packet_list, void* read_proc_ref_con, void* src_conn_ref_con);

    MIDIClientRef client_ = 0;
    MIDIEndpointRef source_ = 0;
    MIDIEndpointRef destination_ = 0;
    ReceiveCallback callback_;
};