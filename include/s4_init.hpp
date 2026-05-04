#pragma once

#include "usb_device.hpp"

// Send the S4 MK1 startup handshake and LED initialization sequence.
// Must be called after UsbDevice::open() and before the bridge loop.
// Derived from USB Wireshark capture of a working Windows session.
void s4_mk1_init(UsbDevice& device);
