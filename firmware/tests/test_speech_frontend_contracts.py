"""Source-level lifecycle guards; acoustic recognition needs board testing."""
from pathlib import Path
import unittest


FIRMWARE = Path(__file__).resolve().parents[1]


class SpeechFrontendContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pipeline = (FIRMWARE / "main/services/assistant/audio_pipeline.cpp").read_text(
            encoding="utf-8"
        )

    def test_cancel_playback_keeps_local_recognition_running(self):
        flush = self.pipeline.split("void AudioPipeline::FlushOutput()", 1)[1].split(
            "bool AudioPipeline::IsOutputDrained()", 1
        )[0]
        self.assertIn("xQueueReset", flush)
        self.assertNotIn("frontend_.Stop", flush)
        self.assertNotIn("running_.store(false)", flush)

    def test_stop_joins_frontend_before_freeing_its_output_queue(self):
        stop = self.pipeline.split("void AudioPipeline::Stop()", 1)[1].split(
            "bool AudioPipeline::EnqueueAudio", 1
        )[0]
        self.assertIn("if (!frontend_.Stop()) return;", stop)
        self.assertLess(stop.index("Audio task shutdown timed out"), stop.index("frontend_.Stop"))
        self.assertLess(stop.index("frontend_.Stop"), stop.index("vQueueDelete"))

    def test_requested_wake_variants_share_one_action(self):
        frontend = (FIRMWARE / "main/services/assistant/speech_frontend.cpp").read_text(
            encoding="utf-8"
        )
        for phonemes in (
            "hd DnM", "hd DcM", "hd DeM", "hd DbM", "hi DnM",
            "hd", "DnM", "DcM", "DeM", "DbM",
        ):
            self.assertIn(f'"{phonemes}"', frontend)
        self.assertIn("esp_mn_commands_add(1, phonemes)", frontend)
        self.assertIn("CONFIG_DOMOS_SR_LINEAR_GAIN_PERCENT / 100", frontend)
        self.assertIn("self->mn->detect(self->mn_data, recognition.data())", frontend)

    def test_armed_audio_stays_local_when_model_is_ready(self):
        service = (FIRMWARE / "main/services/assistant/assistant_service.cpp").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "state == AssistantState::Armed && !pipeline_.HasLocalSpeech()",
            service,
        )

    def test_vad_end_waits_for_ordered_uplink_drain(self):
        service = (FIRMWARE / "main/services/assistant/assistant_service.cpp").read_text(
            encoding="utf-8"
        )
        frontend = (FIRMWARE / "main/services/assistant/speech_frontend.cpp").read_text(
            encoding="utf-8"
        )
        self.assertIn("if (!pipeline_.IsMicInputDrained())", service)
        self.assertIn("local_end_pending_.store(end)", service)
        self.assertIn("upload_paused = true", frontend)
        self.assertIn("context.mode == SpeechMode::Listening && !upload_paused", frontend)

    def test_mic_queue_absorbs_network_jitter_in_psram(self):
        self.assertIn("constexpr size_t kMicQueueDepth = 48", self.pipeline)
        self.assertIn("MALLOC_CAP_SPIRAM", self.pipeline)

    def test_uplink_encodes_each_pcm_frame_without_blocking_capture(self):
        self.assertIn("self->opus_encoder_.Encode", self.pipeline)
        self.assertIn("self->cfg_.on_mic_data(encoded, encoded_bytes)", self.pipeline)


if __name__ == "__main__":
    unittest.main()
