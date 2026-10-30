"""Offline tests for Yandex AI Studio provider; never call the real service."""
from __future__ import annotations

import io
import json
import os
import unittest
from unittest.mock import patch

import app
import ai_server
import yandex_provider


class YandexProviderTests(unittest.TestCase):
    def setUp(self):
        self.scenario = ai_server.SCENARIOS[1]
        self.env = patch.dict(os.environ, {
            "AI_PROVIDER": "yandex",
            "YANDEX_API_KEY": "fake-yandex-key-for-tests",
            "YANDEX_FOLDER_ID": "b1gexamplefolder123",
            "YANDEX_MODEL": "aliceai-llm-flash",
            "PAYWALL_ENABLED": "0",
        })
        self.env.start()
        self.client = app.app.test_client()

    def tearDown(self):
        self.env.stop()

    def fake_model_response(self):
        model_output = {
            "reaction": "Договорились, обсудим завтра.",
            "feedback": "Ты прямо обозначил отказ и сохранил спокойный тон.",
            "rewrite": "Сегодня я не останусь. Обсудим задачи завтра.",
            "signals": {"clarity": 2, "respect": 2, "boundaries": 2},
            "final_summary": "",
            "next_practice": "",
        }
        data = {"choices": [{"message": {"content": json.dumps(model_output, ensure_ascii=False)}}]}
        return io.BytesIO(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def test_yandex_selection_overrides_gemini(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "a-second-key"}):
            self.assertEqual(app.active_provider(), ("yandex", "aliceai-llm-flash"))
            status = self.client.get("/api/status")
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json["provider"], "yandex")

    def test_missing_yandex_key_fails_closed(self):
        with patch.dict(os.environ, {"YANDEX_API_KEY": ""}):
            self.assertEqual(app.active_provider(), (None, None))
            rv = self.client.post("/api/chat", json={"scenario_id": 1, "round": 1,
                    "message": "Сегодня не останусь.", "history": [], "consent": True})
            self.assertEqual(rv.status_code, 503)

    def test_makes_openai_compatible_json_schema(self):
        payload = yandex_provider.make_payload(self.scenario, 3, "Сегодня не смогу.",
                                               [], os.environ["YANDEX_FOLDER_ID"])
        self.assertEqual(payload["model"], "gpt://b1gexamplefolder123/aliceai-llm-flash")
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertIn("signals", payload["response_format"]["json_schema"]["schema"]["properties"])
        self.assertIn("Сегодня не смогу", payload["messages"][1]["content"])

    def test_yandex_reply_end_to_end_without_network(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = json.loads(request.data)
            return self.fake_model_response()

        with patch.object(yandex_provider, "urlopen", side_effect=fake_urlopen):
            rv = self.client.post("/api/chat", json={"scenario_id": 1, "round": 1,
                "message": "Сегодня не останусь.", "history": [], "consent": True})
        self.assertEqual(rv.status_code, 200, rv.get_data(as_text=True))
        self.assertEqual(captured["url"], yandex_provider.ENDPOINT)
        self.assertEqual(captured["headers"]["Authorization"], "Api-Key fake-yandex-key-for-tests")
        self.assertEqual(captured["headers"]["Openai-project"], "b1gexamplefolder123")
        self.assertEqual(captured["body"]["model"], "gpt://b1gexamplefolder123/aliceai-llm-flash")
        self.assertEqual(rv.json["signals"]["clarity"], 2)
        self.assertEqual(rv.json["daily_remaining"], 17)

    def test_health_probe_does_not_need_ai_or_database(self):
        with patch.dict(os.environ, {"YANDEX_API_KEY": "", "PAYWALL_ENABLED": "1"}):
            rv = self.client.get("/healthz")
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.json, {"ok": True})


if __name__ == "__main__":
    unittest.main()
