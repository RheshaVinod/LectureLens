import io
import json
import time
import unittest
import uuid
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from PIL import Image

import app as lecture_app


SAMPLE_RECAP = {
    "title": "Limits lecture",
    "overview": "A grounded review of the lecture.",
    "key_concepts": [{
        "concept": "Limit",
        "explanation": "Describes approach behavior.",
        "evidence_timestamps_ms": [12000],
    }],
    "important_visuals": [],
    "formulas_and_terms": [{"item": "lim", "explanation": "The limit operator."}],
    "review_questions": [{"question": "What is a limit?", "answer": "Approach behavior."}],
    "unresolved_questions": [],
    "next_steps": ["Practice one limit problem."],
}

SAMPLE_ANSWER = {
    "answer": "The professor explained that the limit approaches zero.",
    "visible_evidence": ["This should be removed in transcript-only mode."],
    "inferred_explanation": ["The transcript states the result."],
    "uncertainties": [],
    "transcript_segment_ids": [],
    "status": "supported",
}


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"candidates": [{"content": {"parts": [{"text": json.dumps(SAMPLE_RECAP)}]}}]}


class FakeAsyncClient:
    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, *_args, **_kwargs):
        return FakeResponse()


class FakeAskResponse(FakeResponse):
    def json(self):
        return {"candidates": [{"content": {"parts": [{"text": json.dumps(SAMPLE_ANSWER)}]}}]}


class FakeAskAsyncClient(FakeAsyncClient):
    async def post(self, *_args, **_kwargs):
        return FakeAskResponse()


class RecapApiTest(unittest.TestCase):
    def setUp(self):
        self.session_id = f"sess_test_{uuid.uuid4().hex}"
        with lecture_app.conn() as db:
            db.execute("INSERT INTO sessions (id, started_epoch_ms) VALUES (?,?)",
                       (self.session_id, int(time.time() * 1000)))
            db.execute("""INSERT INTO segments
                       (id, session_id, start_ms, end_ms, speaker, language, text,
                        translated_text, translated_language)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                       (f"seg_{uuid.uuid4().hex}", self.session_id, 10000, 14000,
                        "lecturer", "en", "The limit approaches zero.", None, None))
        self.client = TestClient(lecture_app.app)

    def tearDown(self):
        with lecture_app.conn() as db:
            snapshot_paths = [row["path"] for row in db.execute(
                "SELECT path FROM snapshots WHERE session_id=?", (self.session_id,)).fetchall()]
            db.execute("DELETE FROM recaps WHERE session_id=?", (self.session_id,))
            db.execute("DELETE FROM messages WHERE session_id=?", (self.session_id,))
            db.execute("DELETE FROM segments WHERE session_id=?", (self.session_id,))
            db.execute("DELETE FROM snapshots WHERE session_id=?", (self.session_id,))
            db.execute("DELETE FROM sessions WHERE id=?", (self.session_id,))
        for path in snapshot_paths:
            lecture_app.Path(path).unlink(missing_ok=True)

    def test_phone_join_qr_and_tokenized_snapshot_upload(self):
        join = self.client.get(f"/api/sessions/{self.session_id}/phone-join")
        self.assertEqual(join.status_code, 200, join.text)
        join_data = join.json()
        token = parse_qs(urlparse(join_data["capture_url"]).query)["token"][0]

        qr = self.client.get(join_data["qr_url"])
        self.assertEqual(qr.status_code, 200)
        self.assertEqual(qr.headers["content-type"], "image/png")

        image = io.BytesIO()
        Image.new("RGB", (320, 180), "white").save(image, "JPEG")
        upload = self.client.post(f"/api/sessions/{self.session_id}/snapshots",
                                  files={"image": ("board.jpg", image.getvalue(), "image/jpeg")},
                                  data={"source": "phone", "capture_reason": "phone",
                                        "join_token": token})
        self.assertEqual(upload.status_code, 200, upload.text)
        self.assertEqual(upload.json()["source"], "phone")
        self.assertEqual(upload.json()["capture_reason"], "phone")

        rejected = self.client.post(f"/api/sessions/{self.session_id}/snapshots",
                                    files={"image": ("board.jpg", image.getvalue(), "image/jpeg")},
                                    data={"source": "phone", "capture_reason": "phone",
                                          "join_token": "wrong"})
        self.assertEqual(rejected.status_code, 403)

    @patch.object(lecture_app.httpx, "AsyncClient", FakeAskAsyncClient)
    def test_answer_from_audio_transcript_without_snapshot(self):
        response = self.client.post(f"/api/sessions/{self.session_id}/ask", json={
            "question": "Explain what the professor just said.",
            "language": "en",
            "asked_at_ms": 100000,
        })
        self.assertEqual(response.status_code, 200, response.text)
        message = response.json()
        self.assertIsNone(message["snapshot_id"])
        self.assertEqual(message["result"]["visible_evidence"], [])
        self.assertIn("limit approaches zero", message["result"]["answer"])

    @patch.object(lecture_app.httpx, "AsyncClient", FakeAsyncClient)
    def test_generate_persist_and_download_markdown_recap(self):
        response = self.client.post(f"/api/sessions/{self.session_id}/recap", json={"language": "en"})
        self.assertEqual(response.status_code, 200, response.text)
        recap = response.json()
        self.assertEqual(recap["recap"]["title"], "Limits lecture")

        latest = self.client.get(f"/api/sessions/{self.session_id}/recap")
        self.assertEqual(latest.status_code, 200)
        self.assertEqual(latest.json()["recap_id"], recap["recap_id"])

        context = self.client.get(f"/api/sessions/{self.session_id}/context")
        self.assertEqual(context.status_code, 200)
        self.assertEqual(context.json()["recaps"][0]["recap_id"], recap["recap_id"])

        markdown = self.client.get(recap["markdown_url"])
        self.assertEqual(markdown.status_code, 200)
        self.assertIn("# Limits lecture", markdown.text)
        self.assertIn("0:12", markdown.text)


if __name__ == "__main__":
    unittest.main()
