#pragma once

#include <libusb.h>

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

using namespace std;

struct UsbOptions {
    uint16_t vendor_id = 0;
    uint16_t product_id = 0;
    int interface_number = 0;
    int alternate_setting = 0;
    uint8_t input_endpoint = 0;
    uint8_t output_endpoint = 0;
    int report_size = 0;
    unsigned int timeout_ms = 50;
};

enum class UsbTransferType {
    Bulk,
    Interrupt,
};

class UsbDevice {
public:
    explicit UsbDevice(UsbOptions options);
    ~UsbDevice();

    UsbDevice(const UsbDevice&) = delete;
    UsbDevice& operator=(const UsbDevice&) = delete;

    void print_descriptors() const;
    void open();
    void close();
    bool disconnected() const { return disconnected_; }
    bool is_open() const { return handle_ != nullptr; }
    vector<uint8_t> read_input() const;
    void write_output(const vector<uint8_t>& payload) const;
    void write_to(uint8_t endpoint, const vector<uint8_t>& payload) const;
    void write_to_timeout(uint8_t endpoint, const vector<uint8_t>& payload, unsigned int timeout_ms) const;
    vector<uint8_t> read_from(uint8_t endpoint, int size, unsigned int timeout_ms) const;

    // Expose internals so s4_init can use libusb async API for concurrent IN+OUT
    libusb_context* context() const { return context_; }
    libusb_device_handle* handle() const { return handle_; }

private:
    optional<libusb_device*> find_device() const;
    void resolve_endpoint_types();
    int transfer(uint8_t endpoint, vector<uint8_t>& buffer, int& transferred, UsbTransferType transfer_type) const;
    static string describe_endpoint(const libusb_endpoint_descriptor& endpoint);

    UsbOptions options_;
    libusb_context* context_ = nullptr;
    libusb_device_handle* handle_ = nullptr;
    mutable bool disconnected_ = false;
    UsbTransferType input_transfer_type_ = UsbTransferType::Bulk;
    UsbTransferType output_transfer_type_ = UsbTransferType::Bulk;
};