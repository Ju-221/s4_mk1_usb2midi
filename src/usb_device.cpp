#include "usb_device.hpp"

#include <iomanip>
#include <iostream>
#include <optional>
#include <sstream>
#include <stdexcept>

using namespace std;

namespace {
string hex_byte(uint8_t value) {
    ostringstream stream;
    stream << "0x" << hex << setw(2) << setfill('0') << static_cast<int>(value);
    return stream.str();
}

void check_libusb(int rc, const string& message) {
    if (rc < 0) {
        throw runtime_error(message + ": " + libusb_error_name(rc));
    }
}
}

UsbDevice::UsbDevice(UsbOptions options) : options_(options) {
    check_libusb(libusb_init(&context_), "Failed to initialize libusb");
}

UsbDevice::~UsbDevice() {
    if (handle_ != nullptr) {
        libusb_release_interface(handle_, options_.interface_number);
        libusb_close(handle_);
    }
    if (context_ != nullptr) {
        libusb_exit(context_);
    }
}

void UsbDevice::print_descriptors() const {
    libusb_device** list = nullptr;
    const ssize_t count = libusb_get_device_list(context_, &list);
    if (count < 0) {
        throw runtime_error("Failed to enumerate USB devices");
    }

    for (ssize_t index = 0; index < count; ++index) {
        libusb_device* device = list[index];
        libusb_device_descriptor descriptor{};
        if (libusb_get_device_descriptor(device, &descriptor) != 0) {
            continue;
        }
        if (descriptor.idVendor != options_.vendor_id || descriptor.idProduct != options_.product_id) {
            continue;
        }

           cout << "Found device vid=" << hex_byte(static_cast<uint8_t>(descriptor.idVendor >> 8))
               << hex_byte(static_cast<uint8_t>(descriptor.idVendor & 0xFF)).substr(2)
               << " pid=" << hex_byte(static_cast<uint8_t>(descriptor.idProduct >> 8))
               << hex_byte(static_cast<uint8_t>(descriptor.idProduct & 0xFF)).substr(2)
               << endl;

        libusb_config_descriptor* config = nullptr;
        if (libusb_get_active_config_descriptor(device, &config) != 0) {
            if (libusb_get_config_descriptor(device, 0, &config) != 0) {
                continue;
            }
        }

        for (int iface_index = 0; iface_index < config->bNumInterfaces; ++iface_index) {
            const libusb_interface& iface = config->interface[iface_index];
            for (int alt_index = 0; alt_index < iface.num_altsetting; ++alt_index) {
                const libusb_interface_descriptor& alt = iface.altsetting[alt_index];
                cout << "  interface=" << static_cast<int>(alt.bInterfaceNumber)
                     << " alt=" << static_cast<int>(alt.bAlternateSetting)
                     << " class=0x" << hex << static_cast<int>(alt.bInterfaceClass)
                     << " subclass=0x" << static_cast<int>(alt.bInterfaceSubClass)
                     << dec << endl;
                for (int ep_index = 0; ep_index < alt.bNumEndpoints; ++ep_index) {
                    cout << "    " << describe_endpoint(alt.endpoint[ep_index]) << endl;
                }
            }
        }

        libusb_free_config_descriptor(config);
    }

    libusb_free_device_list(list, 1);
}

void UsbDevice::open() {
    // Enumerate matching devices and reject if more than one is present
    libusb_device** list = nullptr;
    const ssize_t count = libusb_get_device_list(context_, &list);
    if (count < 0) throw runtime_error("Failed to enumerate USB devices");

    int matches = 0;
    libusb_device* target = nullptr;
    for (ssize_t i = 0; i < count; ++i) {
        libusb_device_descriptor desc{};
        if (libusb_get_device_descriptor(list[i], &desc) != 0) continue;
        if (desc.idVendor == options_.vendor_id && desc.idProduct == options_.product_id) {
            ++matches;
            if (target == nullptr) target = list[i];
        }
    }

    if (matches > 1) {
        libusb_free_device_list(list, 1);
        throw runtime_error(
            "Multiple S4 MK1 devices detected — only one device can be used at a time. "
            "Unplug the extra unit and try again.");
    }
    if (target == nullptr) {
        libusb_free_device_list(list, 1);
        throw runtime_error("Unable to open USB device: device not found");
    }

    const int open_rc = libusb_open(target, &handle_);
    libusb_free_device_list(list, 1);
    check_libusb(open_rc, "Failed to open USB device");

    disconnected_ = false;
    check_libusb(libusb_set_auto_detach_kernel_driver(handle_, 1), "Failed to enable auto-detach");
    check_libusb(libusb_claim_interface(handle_, options_.interface_number), "Failed to claim USB interface");
    check_libusb(libusb_set_interface_alt_setting(handle_, options_.interface_number, options_.alternate_setting),
                 "Failed to select alternate setting");
    resolve_endpoint_types();
}

void UsbDevice::close() {
    if (handle_ != nullptr) {
        libusb_release_interface(handle_, options_.interface_number);
        libusb_close(handle_);
        handle_ = nullptr;
    }
    disconnected_ = false;
}

vector<uint8_t> UsbDevice::read_input() const {
    if (handle_ == nullptr) {
        throw runtime_error("USB device is not open");
    }
    if (options_.report_size <= 0) {
        throw runtime_error("Report size must be > 0");
    }

    vector<uint8_t> buffer(static_cast<size_t>(options_.report_size));
    int transferred = 0;
    const int rc = transfer(options_.input_endpoint, buffer, transferred, input_transfer_type_);
    if (rc == LIBUSB_ERROR_TIMEOUT) {
        return {};
    }
    if (rc == LIBUSB_ERROR_NO_DEVICE || rc == LIBUSB_ERROR_IO) {
        disconnected_ = true;
        return {};
    }
    check_libusb(rc, "USB input transfer failed");
    buffer.resize(static_cast<size_t>(transferred));
    return buffer;
}

void UsbDevice::write_output(const vector<uint8_t>& payload) const {
    if (handle_ == nullptr) {
        throw runtime_error("USB device is not open");
    }
    if (options_.output_endpoint == 0) {
        throw runtime_error("No USB output endpoint configured");
    }

    int transferred = 0;
    vector<uint8_t> buffer = payload;
    const int rc = transfer(options_.output_endpoint, buffer, transferred, output_transfer_type_);
    check_libusb(rc, "USB output transfer failed");
    if (transferred != static_cast<int>(payload.size())) {
        throw runtime_error("USB output transfer was short");
    }
}

vector<uint8_t> UsbDevice::read_from(uint8_t endpoint, int size, unsigned int timeout_ms) const {
    if (handle_ == nullptr) { throw runtime_error("USB device is not open"); }
    vector<uint8_t> buffer(static_cast<size_t>(size));
    int transferred = 0;
    const int rc = libusb_bulk_transfer(handle_, endpoint, buffer.data(), size, &transferred, timeout_ms);
    if (rc < 0 && rc != LIBUSB_ERROR_TIMEOUT && rc != LIBUSB_ERROR_OVERFLOW) {
        return {}; // Ignore errors silently during init
    }
    buffer.resize(static_cast<size_t>(transferred));
    return buffer;
}

void UsbDevice::write_to(uint8_t endpoint, const vector<uint8_t>& payload) const {
    if (handle_ == nullptr) {
        throw runtime_error("USB device is not open");
    }
    int transferred = 0;
    vector<uint8_t> buffer = payload;
    const int rc = libusb_bulk_transfer(handle_, endpoint, buffer.data(),
                                        static_cast<int>(buffer.size()), &transferred,
                                        options_.timeout_ms);
    check_libusb(rc, "USB write_to transfer failed on endpoint " + hex_byte(endpoint));
    if (transferred != static_cast<int>(payload.size())) {
        throw runtime_error("USB write_to was short on endpoint " + hex_byte(endpoint));
    }
}

void UsbDevice::write_to_timeout(uint8_t endpoint, const vector<uint8_t>& payload, unsigned int timeout_ms) const {
    if (handle_ == nullptr) {
        throw runtime_error("USB device is not open");
    }
    int transferred = 0;
    vector<uint8_t> buffer = payload;
    const int rc = libusb_bulk_transfer(handle_, endpoint, buffer.data(),
                                        static_cast<int>(buffer.size()), &transferred,
                                        timeout_ms);
    check_libusb(rc, "USB write_to transfer failed on endpoint " + hex_byte(endpoint));
    if (transferred != static_cast<int>(payload.size())) {
        throw runtime_error("USB write_to was short on endpoint " + hex_byte(endpoint));
    }
}

    optional<libusb_device*> UsbDevice::find_device() const {
    libusb_device** list = nullptr;
    const ssize_t count = libusb_get_device_list(context_, &list);
    if (count < 0) {
        return nullopt;
    }

    optional<libusb_device*> result;
    for (ssize_t index = 0; index < count; ++index) {
        libusb_device* device = list[index];
        libusb_device_descriptor descriptor{};
        if (libusb_get_device_descriptor(device, &descriptor) != 0) {
            continue;
        }
        if (descriptor.idVendor == options_.vendor_id && descriptor.idProduct == options_.product_id) {
            result = device;
            break;
        }
    }

    libusb_free_device_list(list, 1);
    return result;
}

void UsbDevice::resolve_endpoint_types() {
    const auto device = find_device();
    if (!device.has_value()) {
        throw runtime_error("Unable to resolve USB endpoint types because the device is no longer present");
    }

    libusb_config_descriptor* config = nullptr;
    if (libusb_get_active_config_descriptor(*device, &config) != 0) {
        check_libusb(libusb_get_config_descriptor(*device, 0, &config), "Failed to get USB config descriptor");
    }

    bool found_input = false;
    bool found_output = options_.output_endpoint == 0;

    const libusb_interface& iface = config->interface[options_.interface_number];
    for (int alt_index = 0; alt_index < iface.num_altsetting; ++alt_index) {
        const libusb_interface_descriptor& alt = iface.altsetting[alt_index];
        if (alt.bAlternateSetting != options_.alternate_setting) {
            continue;
        }

        for (int ep_index = 0; ep_index < alt.bNumEndpoints; ++ep_index) {
            const libusb_endpoint_descriptor& endpoint = alt.endpoint[ep_index];
            const auto transfer_type = (endpoint.bmAttributes & LIBUSB_TRANSFER_TYPE_MASK) == LIBUSB_TRANSFER_TYPE_INTERRUPT
                ? UsbTransferType::Interrupt
                : UsbTransferType::Bulk;

            if (endpoint.bEndpointAddress == options_.input_endpoint) {
                input_transfer_type_ = transfer_type;
                found_input = true;
            }
            if (options_.output_endpoint != 0 && endpoint.bEndpointAddress == options_.output_endpoint) {
                output_transfer_type_ = transfer_type;
                found_output = true;
            }
        }
    }

    libusb_free_config_descriptor(config);

    if (!found_input) {
        throw runtime_error("Configured input endpoint was not found on the selected interface/alternate setting");
    }
    if (!found_output) {
        throw runtime_error("Configured output endpoint was not found on the selected interface/alternate setting");
    }
}

int UsbDevice::transfer(uint8_t endpoint, vector<uint8_t>& buffer, int& transferred, UsbTransferType transfer_type) const {
    auto* data = buffer.empty() ? nullptr : buffer.data();
    const int length = static_cast<int>(buffer.size());
    if (transfer_type == UsbTransferType::Interrupt) {
        return libusb_interrupt_transfer(handle_, endpoint, data, length, &transferred, options_.timeout_ms);
    }
    return libusb_bulk_transfer(handle_, endpoint, data, length, &transferred, options_.timeout_ms);
}

string UsbDevice::describe_endpoint(const libusb_endpoint_descriptor& endpoint) {
    ostringstream stream;
    stream << "endpoint=" << hex_byte(endpoint.bEndpointAddress)
           << " attributes=0x" << hex << static_cast<int>(endpoint.bmAttributes)
           << dec << " max_packet=" << endpoint.wMaxPacketSize
           << " interval=" << static_cast<int>(endpoint.bInterval);
    return stream.str();
}