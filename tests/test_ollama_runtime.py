import unittest
from unittest.mock import patch

import ollama_runtime


class OllamaRuntimeTests(unittest.TestCase):
    def test_running_server_with_model_is_ready_without_starting_process(self):
        with (
            patch.object(ollama_runtime, "_available_models", return_value={"qwen3:14b"}),
            patch.object(ollama_runtime.subprocess, "Popen") as popen,
        ):
            ready, status = ollama_runtime.ensure_ollama_running()
        self.assertTrue(ready)
        self.assertIn("준비됨", status)
        popen.assert_not_called()

    def test_stopped_local_server_is_started_and_model_checked(self):
        with (
            patch.object(ollama_runtime.config, "OLLAMA_AUTO_START", True),
            patch.object(ollama_runtime, "_local_ollama_url", return_value=True),
            patch.object(ollama_runtime, "_find_ollama_executable", return_value="ollama"),
            patch.object(
                ollama_runtime, "_available_models",
                side_effect=[None, {"qwen3:14b"}],
            ),
            patch.object(ollama_runtime.time, "monotonic", side_effect=[0, 0]),
            patch.object(ollama_runtime.time, "sleep"),
            patch.object(ollama_runtime.subprocess, "Popen") as popen,
        ):
            ready, status = ollama_runtime.ensure_ollama_running()
        self.assertTrue(ready)
        self.assertIn("자동 시작 완료", status)
        popen.assert_called_once()

    def test_running_server_without_required_model_is_not_ready(self):
        with patch.object(ollama_runtime, "_available_models", return_value={"other:latest"}):
            ready, status = ollama_runtime.ensure_ollama_running()
        self.assertFalse(ready)
        self.assertIn("모델 없음", status)

    def test_start_failure_falls_back_to_cloud_instead_of_crashing_bot(self):
        with (
            patch.object(ollama_runtime.config, "OLLAMA_AUTO_START", True),
            patch.object(ollama_runtime, "_local_ollama_url", return_value=True),
            patch.object(ollama_runtime, "_available_models", return_value=None),
            patch.object(ollama_runtime, "_find_ollama_executable", return_value="ollama"),
            patch.object(ollama_runtime.subprocess, "Popen", side_effect=OSError("blocked")),
        ):
            ready, status = ollama_runtime.ensure_ollama_running()
        self.assertFalse(ready)
        self.assertIn("자동 시작 실패", status)


if __name__ == "__main__":
    unittest.main()
