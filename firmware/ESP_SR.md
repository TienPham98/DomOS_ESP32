# ESP-SR on ES3C28P

DomOS uses ESP-SR 2.4.7 with the ES8311 single-microphone 16 kHz input.

## Processing path

```text
Microphone
  -> AFE
  -> optional WebRTC noise suppression
  -> VADNet endpoint detection
  -> MultiNet5 English custom phrase recognition
  -> AssistantService
  -> cloud Vietnamese STT/LLM/TTS
```

The local phrases are `Hey Dom`, `Hey`, and `Dom`. This is a custom command configuration on a pretrained MultiNet model, not a personal WakeNet model trained from the owner's recordings.

- Wake threshold is controlled by `CONFIG_DOMOS_WAKE_THRESHOLD`.
- MultiNet-only input gain is controlled by `CONFIG_DOMOS_SR_LINEAR_GAIN_PERCENT`.
- Noise suppression is optional and disabled by default to preserve quiet wake consonants.
- VADNet ends command capture after the configured silence policy.
- AEC is disabled because the mono board path has no validated synchronized playback reference.
- Vietnamese recognition and synthesis remain cloud-side.

AFE feed/fetch and MultiNet tasks are bounded and lower priority than audio output. They do not send network messages, mutate UI objects, or control the amplifier.

## Build output

ESP-SR configuration is selected in `sdkconfig.defaults` and `idf.py menuconfig`. A normal build produces:

- the DomOS application image;
- the packed `srmodels` partition image.

Do not commit generated models, build configuration, memory dumps, or private firmware artifacts.

## Partition migration

Current storage keeps two 4 MiB OTA slots, 5 MiB LittleFS, a dedicated ESP-SR model partition, NVS, and coredump storage.

An ordinary flash does not safely migrate an older 7 MiB LittleFS layout. Before the first layout migration:

1. Back up the complete flash and keep it private.
2. Confirm the active OTA slot.
3. Run the migration helper against the backup.
4. Verify the generated LittleFS image and checksums.
5. Flash the new partition table, selected app image, migrated LittleFS image, model image, and preserved diagnostics at their validated offsets.
6. Verify Wi-Fi, cached images, app launch, wake, endpointing, and speaker playback before deleting the backup.

Migration helper:

```text
python scripts/migrate-sr-storage.py --backup <PRIVATE_BACKUP> --output <PRIVATE_OUTPUT_DIRECTORY>
```

Never erase NVS or OTA selection data as part of a routine model update.

## Verification

Host tests cover speech endpoint policy, local/legacy VAD negotiation, storage migration, task teardown, and amplifier ownership. Final acceptance still requires real-board tests at multiple distances, voice levels, and noise conditions.
