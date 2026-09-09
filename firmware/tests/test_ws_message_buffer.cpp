#include "../main/services/assistant/ws_message_buffer.h"
#include <cassert>
#include <iostream>

int main() {
    using R = WsMessageBuffer::Result;
    WsMessageBuffer buffer;
    // One JSON frame delivered by ESP-IDF in two receive callbacks.
    assert(buffer.Append(1, true, 6, 0, "abc", 3) == R::Pending);
    assert(buffer.Append(1, true, 6, 3, "def", 3) == R::Complete);
    assert(buffer.Opcode() == 1 && buffer.Message() == "abcdef");
    buffer.Reset();
    // Odd PCM byte boundary must never become two independently aligned samples.
    const char pcm[] = {1, 2, 3, 4};
    assert(buffer.Append(2, false, 1, 0, pcm, 1) == R::Pending);
    assert(buffer.Append(9, true, 0, 0, nullptr, 0) == R::Control);
    assert(buffer.Append(0, true, 3, 0, pcm + 1, 3) == R::Complete);
    assert(buffer.Opcode() == 2 && buffer.Message() == std::string(pcm, 4));
    buffer.Reset();
    assert(buffer.Append(0, true, 1, 0, pcm, 1) == R::Invalid);
    assert(buffer.Append(1, true, 4, 0, pcm, 1) == R::Pending);
    assert(buffer.Append(1, true, 4, 2, pcm, 1) == R::Invalid);
    std::string large(16385, 'a');
    assert(buffer.Append(1, true, large.size(), 0, large.data(), large.size()) == R::Invalid);
    assert(buffer.Append(1, true, 1, 0, "x", 1) == R::Complete);
    buffer.Reset();
    assert(buffer.Append(2, false, 4, 0, pcm, 1) == R::Pending);
    assert(buffer.Append(0, true, 1, 0, pcm, 1) == R::Invalid);
    std::cout << "WebSocket fragmentation, PCM alignment, bounds and recovery passed\n";
}
