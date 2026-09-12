# ESP-SR on ES3C28P

## Implemented path

ESP-SR is pinned to **2.4.7**, with ESP-IDF 5.3.1 and the existing ES8311
single-microphone 16 kHz PCM interface. Capture/output remain core 1/core 0,
priority 7. Separate bounded feed/fetch tasks handle inference; they never
send WebSocket messages or control the amplifier.
The speech profile uses a 240 MHz CPU, 32 KiB instruction cache, 64-byte data
cache lines and size-optimized firmware. Feed/AFE run on core 1, while
MultiNet/fetch run on core 0 below the output task's priority. This changes CPU
performance/power use, not Wi-Fi credentials, IP routing or hardware pins.

Microphone → AFE (optional WebRTC noise suppression) → VADNet → MultiNet5 English
custom phrase detection → AssistantService → existing cloud STT/LLM/TTS.
AFE output is reframed to 960 samples, preserving Protocol v3's 60 ms cadence.

- **Wake phrases:** MultiNet5 is configured for `Hey Dom`, `Hey`, and `Dom`,
  including `hd DnM` and `hd DbM` (the long-o pronunciation of Dom).
  It corresponds to `HH EY1 / D AA1 M`, verified with Espressif's G2P alphabet.
  This is a custom phrase using a pretrained English model, **not** a WakeNet
  trained specifically on the owner's voice. Threshold: `CONFIG_DOMOS_WAKE_THRESHOLD`,
  default 20 percent, matching Xiaozhi's custom MultiNet threshold. The initial
  65 and 45 percent trials did not activate reliably; acoustic accuracy and
  false-wake frequency still must be measured on the real device.
- **VADNet:** after confirmed speech, two seconds of silence trigger a local
  `listen.stop`. `features.local_vad` is negotiated with gateway 0.7.5+. Older
  gateways continue their existing RMS endpointing. Capture still has a server
  hard time limit and screen taps can end speech manually.
- **Noise suppression:** optional single-channel WebRTC NS through AFE, not
  NSNet. `CONFIG_DOMOS_SR_NOISE_SUPPRESSION` defaults off, matching Xiaozhi's
  recognition-first profile. ESP-SR warns NS may reduce recognition accuracy;
  the initial on-device NS-enabled trial did not activate Hey Dom reliably.
- **Recognition gain:** MultiNet input gain defaults to 400 percent because
  measured ES3C28P speech remained around RMS 500 despite maximum codec gain.
  VAD/cloud audio stays unscaled, and speaker volume is not affected.
- **AEC:** disabled. The board's current mono capture does not expose a verified,
  synchronized playback-reference channel. Audio upload/wake detection remain
  disabled while responding; this is not full-duplex acoustic barge-in.
- **MultiNet commands:** the recognizer is used for the Hey Dom phrase only.
  Vietnamese hardware commands still use existing gateway intents/MCP tools.
  MultiNet officially supports English/Chinese, not Vietnamese commands.
- **Speech synthesis:** existing Vietnamese cloud TTS remains in use. ESP-SR's
  built-in synthesis supports Chinese, so replacing it would lose Vietnamese.

Local recognition is enabled when the assistant's cloud session is armed and
works while another app is foreground. Armed audio stays on-device while the
local model is ready. If model initialization fails, the existing cloud wake
path remains available and a warning is logged. A wake event foregrounds the
Assistant through EventBus; network control runs on the uplink task, not an
inference task.

## Build and models

`idf.py build` produces both `build/domos_firmware.bin` and
`build/srmodels/srmodels.bin`. On Windows set `$env:PYTHONUTF8 = '1'` before
building: the upstream packer prints Unicode. Defaults select:

```text
CONFIG_DOMOS_ESP_SR=y
CONFIG_SR_MN_EN_MULTINET5_SINGLE_RECOGNITION_QUANT8=y
CONFIG_SR_VADN_VADNET1_MEDIUM=y
CONFIG_SR_NSN_WEBRTC=y
```

For an existing build, select these choices in `idf.py menuconfig`; changing
defaults alone does not override saved model choices. Models total 2,465,289
bytes. Private addresses, credentials and board pins are unchanged.

## Storage migration: do not use ordinary flash on an old partition layout

Both 4 MiB OTA slots and NVS stay at their original addresses. LittleFS retains
its offset `0x820000` but decreases from 7 MiB to 5 MiB. The model partition is
`0xd20000`, size `0x2d0000`; coredump moves to `0xff0000`.

An ordinary `idf.py flash` does **not** migrate the old LittleFS image. Before
the first deployment, obtain user approval, back up the complete 16 MiB flash
with esptool, and keep that backup private/off Git. Use a dedicated Python
environment with `littlefs-python==0.19.0` to run:

```text
python scripts/migrate-sr-storage.py --backup <private-flash-backup.bin> --output <new-private-output-directory>
```

The tool verifies the old layout, copies all files/directories/user attributes,
remounts and compares the new image, and produces SHA-256 manifests. It never
flashes the board, formats the backup, or overwrites an existing output folder.
Flash only the new partition table, app, verified migrated LittleFS, model and
preserved coredump at their validated offsets. Do not erase NVS or reset OTA
selection data. Confirm which OTA slot is active before selecting an app target.

Keep the full backup until Wi-Fi, images, app launch, wake, speech endpointing
and speaker playback have all been tested. Restoring the old app alone is not
a full storage rollback; restoring the original layout also requires its matching
LittleFS image and coredump. Never publish backups, model-memory dumps or secrets.

## Verification

`test_speech_endpoint.cpp` exercises the real endpoint policy with 32 ms AFE
frames, short pauses, reset and one-shot events. `test_device_vad.py` verifies
new/legacy endpoint modes, quiet speech, hard limits and empty stops.
`test_sr_storage_migration.py` additionally requires littlefs-python and checks
content/attribute preservation and rejection of corrupt or partial backups.
`test_speech_frontend_contracts.py` guards cancellation/teardown: flushing speaker
audio must not stop recognition, and full shutdown must join the AFE producer
before freeing its output queue.

Reference: [Espressif ESP-SR](https://github.com/espressif/esp-sr), and
[Xiaozhi custom wake recognizer](https://github.com/78/xiaozhi-esp32/blob/main/main/audio/wake_words/custom_wake_word.cc).
