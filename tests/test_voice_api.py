from __future__ import annotations

import base64
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

from harness.api import voice

FAKE_CLI = """#!{python}
import sys, wave
args = sys.argv[1:]
path = args[args.index("-f") + 1]
with wave.open(path) as w:
    assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
    frames = w.getnframes()
if "--fail" in open(args[args.index("-m") + 1]).read():
    sys.stderr.write("model load failed\\n")
    sys.exit(3)
print(" hello from whisper")
print(" frames=%d lang=%s" % (frames, args[args.index("-l") + 1]))
"""


class VoiceApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        script = os.path.join(self.home, "whisper-cli.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(FAKE_CLI.format(python=sys.executable))
        if os.name == "nt":
            # Windows cannot run a #! script. A .cmd file runs the same script.
            self.cli = os.path.join(self.home, "whisper-cli.cmd")
            with open(self.cli, "w", encoding="utf-8") as f:
                f.write('@"%s" "%s" %%*\r\n' % (sys.executable, script))
        else:
            self.cli = script
            os.chmod(self.cli, os.stat(self.cli).st_mode | stat.S_IEXEC)
        self.env = mock.patch.dict(os.environ, {"HOME": self.home, "USERPROFILE": self.home}, clear=False)
        self.env.start()
        os.environ.pop("HARNESS_WHISPER_MODEL", None)
        os.environ["HARNESS_WHISPER_CLI"] = self.cli

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _model(self, name: str, content: str = "model") -> str:
        os.makedirs(voice.models_dir(), exist_ok=True)
        path = os.path.join(voice.models_dir(), name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def _pcm(self, seconds: float) -> str:
        return base64.b64encode(b"\x00\x01" * int(16000 * seconds)).decode()

    def test_status_names_the_missing_model(self):
        status, payload = voice.get_voice_status()
        self.assertEqual(status, 200)
        self.assertFalse(payload["whisper"]["available"])
        self.assertIn("ggml-small.en.bin", payload["whisper"]["reason"])

    def test_status_names_the_missing_binary(self):
        os.environ["HARNESS_WHISPER_CLI"] = os.path.join(self.home, "absent")
        self._model("ggml-base.en.bin")
        _status, payload = voice.get_voice_status()
        self.assertFalse(payload["whisper"]["available"])
        self.assertIn("whisper-cpp", payload["whisper"]["reason"])

    def test_model_preference_picks_small_en_over_base(self):
        self._model("ggml-base.en.bin")
        self._model("ggml-small.en.bin")
        _status, payload = voice.get_voice_status()
        self.assertTrue(payload["whisper"]["available"])
        self.assertEqual(payload["whisper"]["model"], "ggml-small.en.bin")
        self.assertTrue(payload["whisper"]["english_only"])

    def test_transcribe_runs_whisper_on_a_16k_wav(self):
        self._model("ggml-small.en.bin")
        status, payload = voice.post_voice_transcribe({"pcm16": self._pcm(0.5), "language": "en-US"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["text"], "hello from whisper frames=8000 lang=en")
        self.assertEqual(payload["engine"], "whisper")
        self.assertEqual(payload["seconds"], 0.5)

    def test_english_only_model_refuses_other_languages(self):
        self._model("ggml-small.en.bin")
        status, payload = voice.post_voice_transcribe({"pcm16": self._pcm(0.1), "language": "de-DE"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "unsupported_language")

    def test_multilingual_model_passes_the_language(self):
        self._model("ggml-small.bin")
        status, payload = voice.post_voice_transcribe({"pcm16": self._pcm(0.1), "language": "de-DE"})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["text"].endswith("lang=de"))

    def test_bad_audio_is_rejected_before_whisper_runs(self):
        self._model("ggml-small.en.bin")
        self.assertEqual(voice.post_voice_transcribe({"pcm16": "not base64!"})[0], 400)
        self.assertEqual(voice.post_voice_transcribe({"pcm16": base64.b64encode(b"\x00").decode()})[0], 400)
        self.assertEqual(voice.post_voice_transcribe({"pcm16": ""})[0], 400)
        status, payload = voice.post_voice_transcribe({"pcm16": self._pcm(voice.MAX_SECONDS + 1)})
        self.assertEqual((status, payload["code"]), (413, "audio_too_long"))

    def test_missing_engine_is_503(self):
        status, payload = voice.post_voice_transcribe({"pcm16": self._pcm(0.1)})
        self.assertEqual((status, payload["code"]), (503, "whisper_unavailable"))

    def test_whisper_failure_reports_stderr(self):
        self._model("ggml-small.en.bin", "--fail")
        status, payload = voice.post_voice_transcribe({"pcm16": self._pcm(0.1)})
        self.assertEqual((status, payload["code"]), (502, "whisper_failed"))
        self.assertIn("model load failed", payload["error"])

    def test_routes_are_registered(self):
        from harness import http_routes

        svc = mock.MagicMock()
        self.assertIn("/api/voice/transcribe", http_routes.build_post_json_routes(svc))
        self.assertIn("/api/voice/status", http_routes.build_get_routes(svc))


if __name__ == "__main__":
    unittest.main()
