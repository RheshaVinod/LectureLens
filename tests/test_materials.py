import io
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import fitz
from fastapi.testclient import TestClient
from PIL import Image

import app as lecture


class MaterialUploadTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.original_db, self.original_data = lecture.DB, lecture.DATA
        temporary = Path(self.directory.name)
        source = sqlite3.connect(lecture.DB)
        target = sqlite3.connect(temporary / "test.sqlite3")
        source.backup(target)
        source.close()
        target.close()
        lecture.DB = temporary / "test.sqlite3"
        lecture.DATA = temporary
        self.client = TestClient(lecture.app)
        self.session_id = self.client.post("/api/sessions").json()["session_id"]
        self.class_id = self.client.get(f"/api/sessions/{self.session_id}").json()["classes"][0]["class_id"]

    def tearDown(self):
        lecture.DB, lecture.DATA = self.original_db, self.original_data
        self.directory.cleanup()

    def upload(self, name, data):
        response = self.client.post(f"/api/sessions/{self.session_id}/decks",
                                    data={"class_id": self.class_id},
                                    files={"file": (name, data, "application/octet-stream")})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_document_pdf_image_and_file_only_question(self):
        document = io.BytesIO()
        with zipfile.ZipFile(document, "w") as archive:
            archive.writestr("word/document.xml", """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Gradient descent updates weights.</w:t></w:r></w:p></w:body></w:document>""")
        word = self.upload("notes.docx", document.getvalue())
        self.assertEqual(word["kind"], "document")

        pdf_document = fitz.open()
        for index in range(6):
            page = pdf_document.new_page()
            page.insert_text((40, 40), "The loss is minimized iteratively." if index == 0
                             else "Banana theorem explains the optimization diagram." if index == 5
                             else f"Other lecture note {index}.")
        pdf = self.upload("lecture.pdf", pdf_document.tobytes())
        pdf_document.close()
        self.assertEqual(pdf["kind"], "pdf")

        image = io.BytesIO()
        Image.new("RGB", (300, 200), "white").save(image, "PNG")
        diagram = self.upload("diagram.png", image.getvalue())
        self.assertEqual(diagram["kind"], "image")
        notes = self.upload("outline.txt", b"Optimization lecture outline")
        self.assertEqual(notes["kind"], "text")

        captured = []

        class FakeResponse:
            def __init__(self, labels):
                self.labels = labels

            def raise_for_status(self):
                return None

            def json(self):
                result = {"answer": f"The later PDF page and diagram provide context [[{self.labels[0]}]] [[{self.labels[1]}]].", "visible_evidence": [],
                          "inferred_explanation": [], "uncertainties": [], "transcript_segment_ids": [],
                          "slide_references": [f"{diagram['deck_id']}:1"],
                          "citations": [*self.labels, "M999"], "status": "partial"}
                return {"candidates": [{"content": {"parts": [{"text": json.dumps(result)}]}}]}

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, url, **kwargs):
                payload = kwargs["json"]
                captured.append(payload)
                context = json.loads(payload["contents"][0]["parts"][0]["text"].split("\n", 1)[1])
                labels = [next(item["label"] for item in context["source_catalog"] if item["id"] == source_id)
                          for source_id in (f"{pdf['deck_id']}:6", f"{diagram['deck_id']}:1")]
                return FakeResponse(labels)

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test"}), patch.object(lecture.httpx, "AsyncClient", FakeClient):
            response = self.client.post(f"/api/sessions/{self.session_id}/ask",
                                        json={"question": "Explain the banana theorem optimization diagram", "selected_deck_ids":
                                              [word["deck_id"], pdf["deck_id"], diagram["deck_id"], notes["deck_id"]]})
        self.assertEqual(response.status_code, 200, response.text)
        parts = captured[0]["contents"][0]["parts"]
        context = parts[0]["text"]
        self.assertIn("Gradient descent updates weights", context)
        self.assertIn("loss is minimized iteratively", context)
        self.assertEqual(sum("inlineData" in part for part in parts), 4)
        self.assertEqual(response.json()["result"]["slide_references"], ["diagram.png · image 1"])
        sources = response.json()["result"]["sources"]
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0]["id"], f"{pdf['deck_id']}:6")
        self.assertEqual(sources[1]["id"], f"{diagram['deck_id']}:1")
        self.assertEqual(self.client.get(sources[0]["image_url"]).status_code, 200)
        self.assertEqual(self.client.get(sources[1]["image_url"]).status_code, 200)
        self.assertNotIn("M999", response.json()["result"]["citations"])

        unsupported = self.client.post(f"/api/sessions/{self.session_id}/decks",
                                       data={"class_id": self.class_id},
                                       files={"file": ("archive.zip", b"zip", "application/zip")})
        self.assertEqual(unsupported.status_code, 422)

    def test_answer_links_frame_caption_and_uploaded_note(self):
        note = self.upload("limits.txt", b"The limit approaches zero as x grows.")
        frame_image = io.BytesIO()
        Image.new("RGB", (400, 220), "white").save(frame_image, "JPEG")
        snapshot = self.client.post(f"/api/sessions/{self.session_id}/snapshots",
                                    data={"source": "camera"},
                                    files={"image": ("board.jpg", frame_image.getvalue(), "image/jpeg")}).json()
        caption = self.client.post(f"/api/sessions/{self.session_id}/transcript",
                                   json={"start_ms": 0, "end_ms": 5000, "speaker": "lecturer",
                                         "language": "en", "text": "The limit approaches zero."}).json()

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                result = {"answer": "The board and lecture agree [[F1]] [[A1]], as the note confirms [[M1]].",
                          "visible_evidence": ["A formula is on the board"], "inferred_explanation": [],
                          "uncertainties": [], "transcript_segment_ids": [caption["segment_id"]],
                          "slide_references": [f"{note['deck_id']}:1"],
                          "citations": ["F1", "A1", "M1", "F999"], "status": "supported"}
                return {"candidates": [{"content": {"parts": [{"text": json.dumps(result)}]}}]}

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, url, **kwargs):
                return FakeResponse()

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test"}), patch.object(lecture.httpx, "AsyncClient", FakeClient):
            response = self.client.post(f"/api/sessions/{self.session_id}/ask",
                                        json={"question": "Why does the limit approach zero?",
                                              "snapshot_id": snapshot["snapshot_id"],
                                              "selected_deck_ids": [note["deck_id"]]})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertEqual(result["citations"], ["F1", "A1", "M1"])
        self.assertEqual([source["kind"] for source in result["sources"]], ["frame", "transcript", "material"])
        self.assertEqual(result["sources"][1]["excerpt"], "The limit approaches zero.")
        self.assertEqual(self.client.get(result["sources"][0]["image_url"]).status_code, 200)

    def test_powerpoint_embedded_image_is_available_as_visual_context(self):
        picture = io.BytesIO()
        Image.new("RGB", (320, 180), "gold").save(picture, "PNG")
        deck_bytes = io.BytesIO()
        with zipfile.ZipFile(deck_bytes, "w") as archive:
            archive.writestr("ppt/presentation.xml", """<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>""")
            archive.writestr("ppt/_rels/presentation.xml.rels", """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="slides/slide1.xml"/></Relationships>""")
            archive.writestr("ppt/slides/slide1.xml", """<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><a:p><a:r><a:t>Training diagram</a:t></a:r></a:p><a:blip r:embed="rId2"/></p:sld>""")
            archive.writestr("ppt/slides/_rels/slide1.xml.rels", """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId2" Target="../media/diagram.png"/></Relationships>""")
            archive.writestr("ppt/media/diagram.png", picture.getvalue())
        deck = self.upload("training.pptx", deck_bytes.getvalue())
        image_url = f"/api/sessions/{self.session_id}/materials/{deck['deck_id']}/pages/1/image"
        self.assertEqual(self.client.get(image_url).status_code, 200)
        with lecture.conn() as db:
            slide = db.execute("SELECT text,image_path FROM deck_slides WHERE deck_id=?", (deck["deck_id"],)).fetchone()
        self.assertEqual(slide["text"], "Training diagram")
        self.assertTrue(slide["image_path"])


if __name__ == "__main__":
    unittest.main()
