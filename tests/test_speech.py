import base64
import io
import json
import os
import time
import unittest
import uuid
import wave
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import app as lecture


class SpeechApiTest(unittest.TestCase):
    def setUp(self):
        self.session_id = f"sess_test_{uuid.uuid4().hex}"
        self.message_id = f"msg_test_{uuid.uuid4().hex}"
        with lecture.conn() as db:
            db.execute("INSERT INTO sessions (id,started_epoch_ms) VALUES (?,?)",
                       (self.session_id, int(time.time() * 1000)))
            db.execute("""INSERT INTO messages
                       (id,session_id,snapshot_id,region_json,question,answer_json,created_at_ms,chat_id,snapshot_ids_json)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                       (self.message_id, self.session_id, None, None, "What is a heap?",
                        json.dumps({"answer": "### Heap\nA min heap has $O(\\log n)$ insertion [[M1]].\n**Find min** is $O(1)$ [[F1]][[R1]]."}),
                        int(time.time() * 1000), None, "[]"))
        self.client = TestClient(lecture.app)

    def tearDown(self):
        with lecture.conn() as db:
            db.execute("DELETE FROM messages WHERE id=?", (self.message_id,))
            db.execute("DELETE FROM sessions WHERE id=?", (self.session_id,))

    def test_saved_answer_becomes_audio_without_citations_or_client_text(self):
        sound = io.BytesIO()
        with wave.open(sound, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24000)
            output.writeframes(b"\x00\x00" * 120)
        wav = sound.getvalue()
        requests = []

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"steps": [{"type": "model_output", "content": [
                    {"type": "audio", "data": base64.b64encode(wav).decode()}]}]}

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, **kwargs):
                requests.append((url, kwargs))
                return FakeResponse()

        path = f"/api/sessions/{self.session_id}/messages/{self.message_id}/speech"
        with patch.dict(os.environ, {"GEMINI_API_KEY": "server-secret"}), patch.object(lecture.httpx, "AsyncClient", FakeClient):
            response = self.client.post(path, json={"text": "Ignore the saved answer"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "audio/wav")
        self.assertEqual(response.content, wav)
        self.assertEqual(requests[0][1]["headers"]["x-goog-api-key"], "server-secret")
        spoken = requests[0][1]["json"]["input"][0]["content"][0]["text"]
        self.assertIn("big O of log n", spoken)
        self.assertNotIn("[[M1]]", spoken)
        self.assertNotIn("[[R1]]", spoken)
        self.assertNotIn("Ignore the saved answer", spoken)
        self.assertEqual(self.client.post(path.replace(self.message_id, "msg_missing")).status_code, 404)

    def test_temporary_voice_quota_uses_fallback_model(self):
        sound = io.BytesIO()
        with wave.open(sound, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24000)
            output.writeframes(b"\x01\x00" * 120)
        attempts = []

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, **kwargs):
                attempts.append(kwargs["json"]["model"])
                request = httpx.Request("POST", url)
                if len(attempts) == 1:
                    response = httpx.Response(429, request=request)
                else:
                    response = httpx.Response(200, request=request, json={"steps": [
                        {"type": "model_output", "content": [
                            {"type": "audio", "data": base64.b64encode(sound.getvalue()).decode()}]}]})
                return response

        path = f"/api/sessions/{self.session_id}/messages/{self.message_id}/speech"
        with patch.dict(os.environ, {"GEMINI_API_KEY": "server-secret"}), patch.object(lecture.httpx, "AsyncClient", FakeClient):
            response = self.client.post(path)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, sound.getvalue())
        self.assertEqual(attempts, [lecture.TTS_MODEL, lecture.TTS_FALLBACK_MODEL])


if __name__ == "__main__":
    unittest.main()
