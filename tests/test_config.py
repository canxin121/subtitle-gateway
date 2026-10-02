import unittest

from gateway.config import parse_args


class OpenAIConfigTests(unittest.TestCase):
    def test_openai_api_key_defaults_to_disabled(self):
        self.assertEqual(parse_args([]).openai_api_key, "")

    def test_openai_api_key_can_be_configured_from_cli(self):
        cfg = parse_args(["--openai-api-key", "test-key"])
        self.assertEqual(cfg.openai_api_key, "test-key")


if __name__ == "__main__":
    unittest.main()
