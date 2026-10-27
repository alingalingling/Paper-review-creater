import json
import os
import tempfile
import unittest
from unittest.mock import patch

from trace_review.config import config_path, load_model_config, save_model_config


@unittest.skipUnless(os.name == "nt", "Windows DPAPI")
class LocalConfigTests(unittest.TestCase):
    def test_encrypted_roundtrip_and_environment_override(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {"TRACE_CONFIG_DIR": folder}, clear=True):
            supplied = {"base": "https://api.deepseek.com", "model": "deepseek-flash", "key": "unit-test-private-key"}
            save_model_config(supplied)
            self.assertNotIn(b"unit-test-private-key", config_path().read_bytes())
            self.assertEqual(load_model_config(), supplied)
            with patch.dict("os.environ", {"TRACE_MODEL": "override-model"}):
                self.assertEqual(load_model_config()["model"], "override-model")

    def test_corrupt_store_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {"TRACE_CONFIG_DIR": folder}, clear=True):
            config_path().write_bytes(b"not-encrypted-data")
            with self.assertRaisesRegex(ValueError, "重新保存"):
                load_model_config()
