cmake_minimum_required(VERSION 3.20)

project(s4_mk1_usb2midi LANGUAGES CXX)

# Pin the project language level to C++20 and force cache overrides back to 20.
set(CMAKE_CXX_STANDARD 20 CACHE STRING "C++ standard" FORCE)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(PkgConfig REQUIRED)
pkg_check_modules(LIBUSB REQUIRED IMPORTED_TARGET libusb-1.0)

find_library(COREMIDI_FRAMEWORK CoreMIDI REQUIRED)
find_library(COREFOUNDATION_FRAMEWORK CoreFoundation REQUIRED)

add_executable(s4_mk1_usb2midi
    src/main.cpp
    src/usb_device.cpp
    src/core_midi_bridge.cpp
    src/s4_generic_protocol.cpp
)

target_include_directories(s4_mk1_usb2midi PRIVATE include)
target_link_libraries(s4_mk1_usb2midi
    PRIVATE
    PkgConfig::LIBUSB
    ${COREMIDI_FRAMEWORK}
    ${COREFOUNDATION_FRAMEWORK}
)

target_compile_options(s4_mk1_usb2midi PRIVATE
    -std=c++20
    -Wall
    -Wextra
    -Wpedantic
)
# Lightweight linter target (syntax + warnings, optional clang-tidy if installed)
add_custom_target(lint
    COMMAND "${CMAKE_CURRENT_SOURCE_DIR}/lint.sh"
    WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
    COMMENT "Running C++ linter checks"
)

# Basic protocol tests (no hardware required)
enable_testing()

add_executable(test_s4_generic_protocol
    tests/test_s4_generic_protocol.cpp
    src/s4_generic_protocol.cpp
)

target_include_directories(test_s4_generic_protocol PRIVATE include)
target_compile_options(test_s4_generic_protocol PRIVATE
    -std=c++20
    -Wall
    -Wextra
    -Wpedantic
)

add_test(NAME test_s4_generic_protocol COMMAND test_s4_generic_protocol)
