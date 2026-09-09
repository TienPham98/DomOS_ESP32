#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

// ESP-IDF can split one WebSocket frame across callbacks, and WebSocket itself
// permits continuation frames. Deliver only complete messages to JSON/PCM.
class WsMessageBuffer {
public:
    enum class Result { Pending, Complete, Invalid, Control };
    Result Append(uint8_t opcode, bool fin, size_t payload_size, size_t offset,
                  const char* data, size_t size) {
        if (opcode >= 8) return Result::Control;
        if (offset == 0) {
            if (opcode == 1 || opcode == 2) {
                if (opcode_ != 0) { Reset(); return Result::Invalid; }
                opcode_ = opcode;
            } else if (opcode != 0 || opcode_ == 0 || frame_offset_ != frame_size_) {
                Reset(); return Result::Invalid;
            }
            frame_offset_ = 0;
            frame_size_ = payload_size;
        }
        if (opcode_ == 0 || offset != frame_offset_ || payload_size != frame_size_ ||
            offset > payload_size || size > payload_size - offset ||
            size > kMaxMessageBytes - message_.size() || (size && !data)) {
            Reset(); return Result::Invalid;
        }
        if (size) message_.append(data, size);
        frame_offset_ += size;
        return fin && frame_offset_ == frame_size_ ? Result::Complete : Result::Pending;
    }
    void Reset() { message_.clear(); opcode_ = 0; frame_offset_ = frame_size_ = 0; }
    uint8_t Opcode() const { return opcode_; }
    const std::string& Message() const { return message_; }

private:
    static constexpr size_t kMaxMessageBytes = 16384;
    std::string message_;
    uint8_t opcode_ = 0;
    size_t frame_offset_ = 0;
    size_t frame_size_ = 0;
};
