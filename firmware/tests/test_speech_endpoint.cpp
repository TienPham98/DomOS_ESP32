#include "../main/services/assistant/speech_endpoint.h"
#include <cassert>

int main() {
    SpeechEndpoint endpoint;
    for (int i = 0; i < 100; ++i) assert(!endpoint.Feed(false, 512));
    assert(!endpoint.Feed(true, 2048));
    for (int i = 0; i < 62; ++i) assert(!endpoint.Feed(false, 512));
    assert(endpoint.Feed(false, 512)); // 2016 ms at the AFE's 32 ms cadence.
    assert(!endpoint.Feed(false, 512)); // One event per utterance.
    endpoint.Reset();
    assert(!endpoint.Feed(true, 2048));
    assert(!endpoint.Feed(false, 30000));
    assert(!endpoint.Feed(true, 512)); // A short pause is not the end.
    assert(!endpoint.Feed(false, 31999));
    assert(endpoint.Feed(false, 1));
    endpoint.Reset(); // No stale silence carries across Cancel/reconnect.
    assert(!endpoint.Feed(false, 64000));
}
