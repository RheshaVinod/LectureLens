"""Single-process LectureLens hackathon prototype."""
import asyncio
import base64
import io
import json
import logging
import os
import posixpath
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode
from xml.etree import ElementTree

import httpx
import pymupdf as fitz
import qrcode
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
DB = DATA / "lecturelens.sqlite3"
MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
TTS_MODEL = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.getenv("OPENAI_TTS_VOICE", "marin")
logger = logging.getLogger("lecturelens")
MAX_UPLOAD = 8 * 1024 * 1024
MAX_SLIDE_UPLOAD = 30 * 1024 * 1024
PHONE_JOIN_LIFETIME_MS = 6 * 60 * 60 * 1000
ANSWER_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "answer": {"type": "STRING"},
        "visible_evidence": {"type": "ARRAY", "items": {"type": "STRING"}},
        "inferred_explanation": {"type": "ARRAY", "items": {"type": "STRING"}},
        "uncertainties": {"type": "ARRAY", "items": {"type": "STRING"}},
        "transcript_segment_ids": {"type": "ARRAY", "items": {"type": "STRING"}},
        "status": {"type": "STRING", "enum": ["supported", "partial", "unsupported"]},
    },
    "required": ["answer", "visible_evidence", "inferred_explanation", "uncertainties",
                 "transcript_segment_ids", "status"],
}
RECAP_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "overview": {"type": "string"},
        "key_concepts": {"type": "array", "items": {"type": "object", "properties": {
            "concept": {"type": "string"}, "explanation": {"type": "string"},
            "evidence_timestamps_ms": {"type": "array", "items": {"type": "integer"}},
        }, "required": ["concept", "explanation", "evidence_timestamps_ms"], "additionalProperties": False}},
        "important_visuals": {"type": "array", "items": {"type": "object", "properties": {
            "snapshot_id": {"type": "string"}, "timestamp_ms": {"type": "integer"},
            "description": {"type": "string"},
        }, "required": ["snapshot_id", "timestamp_ms", "description"], "additionalProperties": False}},
        "formulas_and_terms": {"type": "array", "items": {"type": "object", "properties": {
            "item": {"type": "string"}, "explanation": {"type": "string"},
        }, "required": ["item", "explanation"], "additionalProperties": False}},
        "review_questions": {"type": "array", "items": {"type": "object", "properties": {
            "question": {"type": "string"}, "answer": {"type": "string"},
        }, "required": ["question", "answer"], "additionalProperties": False}},
        "unresolved_questions": {"type": "array", "items": {"type": "string"}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "overview", "key_concepts", "important_visuals",
                 "formulas_and_terms", "review_questions", "unresolved_questions", "next_steps"],
    "additionalProperties": False,
}
MAX_DECK_UPLOAD = 20 * 1024 * 1024
MAX_DECK_SLIDES = 100
MAX_QUESTION_FRAMES = 4
MATERIAL_EXTENSIONS = {".ppt", ".pptx", ".pdf", ".doc", ".docx", ".png", ".jpg", ".jpeg", ".webp", ".txt", ".md"}
Image.MAX_IMAGE_PIXELS = 24_000_000

app = FastAPI(title="LectureLens")
app.mount("/assets", StaticFiles(directory=ROOT / "static"), name="assets")


@contextmanager
def conn():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    finally:
        db.close()


with conn() as db:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, started_epoch_ms INTEGER NOT NULL,
      join_token TEXT, join_expires_epoch_ms INTEGER);
    CREATE TABLE IF NOT EXISTS snapshots (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
      captured_at_ms INTEGER NOT NULL, source TEXT NOT NULL, width_px INTEGER NOT NULL,
      height_px INTEGER NOT NULL, path TEXT NOT NULL, deck_id TEXT, slide_number INTEGER,
      capture_reason TEXT NOT NULL DEFAULT 'timer');
    CREATE TABLE IF NOT EXISTS segments (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
      start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, speaker TEXT NOT NULL,
      language TEXT NOT NULL, text TEXT NOT NULL, translated_text TEXT,
      translated_language TEXT);
    CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
      snapshot_id TEXT, region_json TEXT, question TEXT NOT NULL,
      answer_json TEXT NOT NULL, created_at_ms INTEGER NOT NULL,
      chat_id TEXT, snapshot_ids_json TEXT);
    CREATE TABLE IF NOT EXISTS recaps (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
      language TEXT NOT NULL, recap_json TEXT NOT NULL, created_at_ms INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
      title TEXT NOT NULL, created_at_ms INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS classes (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
      name TEXT NOT NULL, created_at_ms INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS decks (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
      class_id TEXT NOT NULL, filename TEXT NOT NULL, slide_count INTEGER NOT NULL,
      created_at_ms INTEGER NOT NULL, source_path TEXT);
    CREATE TABLE IF NOT EXISTS deck_slides (deck_id TEXT NOT NULL, slide_number INTEGER NOT NULL,
      title TEXT NOT NULL, text TEXT NOT NULL, image_path TEXT, PRIMARY KEY(deck_id, slide_number));
    """)
    columns = {row["name"] for row in db.execute("PRAGMA table_info(segments)").fetchall()}
    if "translated_text" not in columns:
        db.execute("ALTER TABLE segments ADD COLUMN translated_text TEXT")
    if "translated_language" not in columns:
        db.execute("ALTER TABLE segments ADD COLUMN translated_language TEXT")
    snapshot_columns = {row["name"] for row in db.execute("PRAGMA table_info(snapshots)").fetchall()}
    if "deck_id" not in snapshot_columns:
        db.execute("ALTER TABLE snapshots ADD COLUMN deck_id TEXT")
    if "slide_number" not in snapshot_columns:
        db.execute("ALTER TABLE snapshots ADD COLUMN slide_number INTEGER")
    if "capture_reason" not in snapshot_columns:
        db.execute("ALTER TABLE snapshots ADD COLUMN capture_reason TEXT NOT NULL DEFAULT 'timer'")
    message_columns = {row["name"]: row for row in db.execute("PRAGMA table_info(messages)").fetchall()}
    if message_columns["snapshot_id"]["notnull"]:
        db.executescript("""
        ALTER TABLE messages RENAME TO messages_with_required_snapshot;
        CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
          snapshot_id TEXT, region_json TEXT, question TEXT NOT NULL,
          answer_json TEXT NOT NULL, created_at_ms INTEGER NOT NULL,
          chat_id TEXT, snapshot_ids_json TEXT);
        """)
        chat_expression = "chat_id" if "chat_id" in message_columns else "NULL"
        snapshot_ids_expression = "snapshot_ids_json" if "snapshot_ids_json" in message_columns else "NULL"
        db.execute(f"""INSERT INTO messages
                    (id,session_id,snapshot_id,region_json,question,answer_json,created_at_ms,chat_id,snapshot_ids_json)
                    SELECT id,session_id,snapshot_id,region_json,question,answer_json,created_at_ms,
                           {chat_expression},{snapshot_ids_expression}
                    FROM messages_with_required_snapshot""")
        db.execute("DROP TABLE messages_with_required_snapshot")
    session_columns = {row["name"] for row in db.execute("PRAGMA table_info(sessions)").fetchall()}
    if "join_token" not in session_columns:
        db.execute("ALTER TABLE sessions ADD COLUMN join_token TEXT")
    if "join_expires_epoch_ms" not in session_columns:
        db.execute("ALTER TABLE sessions ADD COLUMN join_expires_epoch_ms INTEGER")
    now_ms = int(time.time() * 1000)
    for row in db.execute("SELECT id FROM sessions WHERE join_token IS NULL").fetchall():
        db.execute("UPDATE sessions SET join_token=?, join_expires_epoch_ms=? WHERE id=?",
                   (secrets.token_urlsafe(18), now_ms + PHONE_JOIN_LIFETIME_MS, row["id"]))
    if "chat_id" not in {column[1] for column in db.execute("PRAGMA table_info(messages)")}:
        db.execute("ALTER TABLE messages ADD COLUMN chat_id TEXT")
    if "snapshot_ids_json" not in {column[1] for column in db.execute("PRAGMA table_info(messages)")}:
        db.execute("ALTER TABLE messages ADD COLUMN snapshot_ids_json TEXT")
    if "image_path" not in {column[1] for column in db.execute("PRAGMA table_info(deck_slides)")}:
        db.execute("ALTER TABLE deck_slides ADD COLUMN image_path TEXT")
    if "source_path" not in {column[1] for column in db.execute("PRAGMA table_info(decks)")}:
        db.execute("ALTER TABLE decks ADD COLUMN source_path TEXT")
    # Older sessions had a single message stream. Keep it as one saved chat.
    legacy_sessions = db.execute("SELECT DISTINCT session_id FROM messages WHERE chat_id IS NULL").fetchall()
    for row in legacy_sessions:
        sid = row["session_id"]
        first = db.execute("SELECT question, created_at_ms FROM messages WHERE session_id=? ORDER BY created_at_ms LIMIT 1", (sid,)).fetchone()
        chat_id = f"chat_{uuid.uuid4().hex}"
        db.execute("INSERT INTO chats (id,session_id,title,created_at_ms) VALUES (?,?,?,?)", (chat_id, sid, first["question"][:64], first["created_at_ms"]))
        db.execute("UPDATE messages SET chat_id=? WHERE session_id=? AND chat_id IS NULL", (chat_id, sid))
    if "class_id" not in {column[1] for column in db.execute("PRAGMA table_info(chats)")}:
        db.execute("ALTER TABLE chats ADD COLUMN class_id TEXT")
    for row in db.execute("SELECT id FROM sessions").fetchall():
        sid = row["id"]
        for name in ("Analysis of Algorithms", "Deep Learning"):
            if not db.execute("SELECT 1 FROM classes WHERE session_id=? AND name=?", (sid, name)).fetchone():
                db.execute("INSERT INTO classes VALUES (?,?,?,?)",
                           (f"class_{uuid.uuid4().hex}", sid, name, int(time.time() * 1000)))
        default_class = db.execute("SELECT id FROM classes WHERE session_id=? AND name='Analysis of Algorithms'", (sid,)).fetchone()["id"]
        db.execute("UPDATE chats SET class_id=? WHERE session_id=? AND class_id IS NULL", (default_class, sid))


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex}"


def session(db, session_id):
    row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Session not found")
    return row


def phone_join_credentials(db, session_id):
    row = session(db, session_id)
    now_ms = int(time.time() * 1000)
    if not row["join_token"] or not row["join_expires_epoch_ms"] or row["join_expires_epoch_ms"] <= now_ms:
        token = secrets.token_urlsafe(18)
        expires = now_ms + PHONE_JOIN_LIFETIME_MS
        db.execute("UPDATE sessions SET join_token=?, join_expires_epoch_ms=? WHERE id=?",
                   (token, expires, session_id))
        return token, expires
    return row["join_token"], row["join_expires_epoch_ms"]


def snapshot_public(row):
    return {"snapshot_id": row["id"], "session_id": row["session_id"],
            "captured_at_ms": row["captured_at_ms"], "source": row["source"],
            "width_px": row["width_px"], "height_px": row["height_px"],
            "deck_id": row["deck_id"], "slide_number": row["slide_number"],
            "capture_reason": row["capture_reason"],
            "image_url": f"/api/snapshots/{row['id']}/image"}


def save_snapshot(db, session_id, picture, source, captured_at_ms=None, deck_id=None,
                  slide_number=None, capture_reason="timer"):
    started = session(db, session_id)["started_epoch_ms"]
    timestamp = max(0, int(time.time() * 1000) - started) if captured_at_ms is None else captured_at_ms
    picture = picture.convert("RGB")
    picture.thumbnail((1920, 1080))
    sid = new_id("snap")
    path = DATA / f"{sid}.jpg"
    picture.save(path, "JPEG", quality=88)
    db.execute("""INSERT INTO snapshots
               (id, session_id, captured_at_ms, source, width_px, height_px, path, deck_id,
                slide_number, capture_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
               (sid, session_id, timestamp, source, picture.width, picture.height,
                str(path), deck_id, slide_number, capture_reason))
    return db.execute("SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone()


def deck_public(row):
    suffix = Path(row["filename"]).suffix.lower()
    kind = ("slides" if suffix in (".ppt", ".pptx") else "pdf" if suffix == ".pdf"
            else "image" if suffix in (".png", ".jpg", ".jpeg", ".webp")
            else "document" if suffix in (".doc", ".docx") else "text")
    return {"deck_id": row["id"], "class_id": row["class_id"],
            "filename": row["filename"], "slide_count": row["slide_count"],
            "kind": kind,
            "created_at_ms": row["created_at_ms"]}


def pptx_bytes(raw, filename):
    if filename.lower().endswith(".pptx"):
        return raw
    converter = shutil.which("soffice") or shutil.which("libreoffice")
    if not converter:
        raise HTTPException(422, "Legacy .ppt files need LibreOffice on the server. Save as .pptx and upload again.")
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "lecture.ppt"
        source.write_bytes(raw)
        try:
            conversion = subprocess.run(
                [converter, "--headless", "--convert-to", "pptx", "--outdir", directory, str(source)],
                capture_output=True, timeout=45, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise HTTPException(422, "Could not convert this .ppt. Save it as .pptx and upload again.")
        converted = Path(directory) / "lecture.pptx"
        if conversion.returncode != 0 or not converted.exists():
            raise HTTPException(422, "Could not convert this .ppt. Save it as .pptx and upload again.")
        return converted.read_bytes()


def extract_deck_slides(raw):
    drawing = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    presentation = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
    relationship = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    package = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            files = {info.filename: info for info in archive.infolist()}
            if "ppt/presentation.xml" not in files or "ppt/_rels/presentation.xml.rels" not in files:
                raise HTTPException(422, "This is not a readable PowerPoint deck")
            if sum(info.file_size for info in files.values()) > 80 * 1024 * 1024:
                raise HTTPException(413, "PowerPoint deck is too large after extraction")
            root = ElementTree.fromstring(archive.read("ppt/presentation.xml"))
            rels = ElementTree.fromstring(archive.read("ppt/_rels/presentation.xml.rels"))
            targets = {item.get("Id"): item.get("Target", "") for item in rels.findall(f"{package}Relationship")}
            slide_names = []
            for item in root.iter(f"{presentation}sldId"):
                target = targets.get(item.get(f"{relationship}id"), "")
                name = target.lstrip("/") if target.startswith("/") else f"ppt/{target}"
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", name) and name in files:
                    slide_names.append(name)
            if not slide_names or len(slide_names) > MAX_DECK_SLIDES:
                raise HTTPException(422, f"Deck must have 1 to {MAX_DECK_SLIDES} readable slides")
            slides = []
            for number, name in enumerate(slide_names, 1):
                if files[name].file_size > 2 * 1024 * 1024:
                    raise HTTPException(413, "A slide contains too much XML data")
                slide = ElementTree.fromstring(archive.read(name))
                lines = []
                for paragraph in slide.iter(f"{drawing}p"):
                    line = "".join(node.text or "" for node in paragraph.iter(f"{drawing}t")).strip()
                    if line:
                        lines.append(line)
                text = "\n".join(lines)[:6000]
                item = {"slide_number": number, "title": lines[0][:120] if lines else f"Slide {number}", "text": text}
                rels_name = f"ppt/slides/_rels/{posixpath.basename(name)}.rels"
                if rels_name in files:
                    slide_rels = ElementTree.fromstring(archive.read(rels_name))
                    media_targets = {rel.get("Id"): rel.get("Target", "")
                                     for rel in slide_rels.findall(f"{package}Relationship")}
                    for blip in slide.iter(f"{drawing}blip"):
                        target = media_targets.get(blip.get(f"{relationship}embed"), "")
                        media_name = posixpath.normpath(posixpath.join("ppt/slides", target))
                        if not media_name.startswith("ppt/media/") or media_name not in files or files[media_name].file_size > 8 * 1024 * 1024:
                            continue
                        try:
                            image = Image.open(io.BytesIO(archive.read(media_name))).convert("RGB")
                            image.thumbnail((1600, 1600))
                            output = io.BytesIO()
                            image.save(output, "JPEG", quality=85)
                            item["image_bytes"] = output.getvalue()
                            break
                        except (UnidentifiedImageError, ValueError, OSError, Image.DecompressionBombError):
                            continue
                slides.append(item)
            if not any(slide["text"] or slide.get("image_bytes") for slide in slides):
                raise HTTPException(422, "No readable text or images found in the slides")
            return slides
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError, RuntimeError):
        raise HTTPException(422, "This is not a readable PowerPoint deck")


def text_sections(text, title):
    clean = text.strip()
    if not clean:
        raise HTTPException(422, "No readable text found in this file")
    chunks = [clean[index:index + 1200] for index in range(0, min(len(clean), 36000), 1200)]
    return [{"slide_number": index, "title": f"{title} · section {index}", "text": chunk}
            for index, chunk in enumerate(chunks, 1)]


def word_bytes(raw, filename):
    if filename.lower().endswith(".docx"):
        return raw
    converter = shutil.which("soffice") or shutil.which("libreoffice")
    if not converter:
        raise HTTPException(422, "Legacy .doc files need LibreOffice. Save as .docx and upload again.")
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "lecture.doc"
        source.write_bytes(raw)
        try:
            result = subprocess.run([converter, "--headless", "--convert-to", "docx", "--outdir", directory, str(source)],
                                    capture_output=True, timeout=45, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise HTTPException(422, "Could not convert this .doc. Save as .docx and upload again.")
        converted = Path(directory) / "lecture.docx"
        if result.returncode != 0 or not converted.exists():
            raise HTTPException(422, "Could not convert this .doc. Save as .docx and upload again.")
        return converted.read_bytes()


def extract_material(raw, filename):
    suffix = Path(filename).suffix.lower()
    if suffix in (".ppt", ".pptx"):
        return extract_deck_slides(pptx_bytes(raw, filename))
    if suffix in (".doc", ".docx"):
        try:
            with zipfile.ZipFile(io.BytesIO(word_bytes(raw, filename))) as archive:
                if sum(info.file_size for info in archive.infolist()) > 80 * 1024 * 1024:
                    raise HTTPException(413, "Word document is too large after extraction")
                document = archive.read("word/document.xml")
            root = ElementTree.fromstring(document)
            namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            lines = ["".join(node.text or "" for node in paragraph.iter(f"{namespace}t"))
                     for paragraph in root.iter(f"{namespace}p")]
            return text_sections("\n".join(line for line in lines if line.strip()), filename)
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
            raise HTTPException(422, "This is not a readable Word document")
    if suffix in (".txt", ".md"):
        try:
            return text_sections(raw.decode("utf-8-sig"), filename)
        except UnicodeDecodeError:
            raise HTTPException(422, "Text file must use UTF-8 encoding")
    if suffix == ".pdf":
        try:
            with fitz.open(stream=raw, filetype="pdf") as document:
                if not 1 <= document.page_count <= MAX_DECK_SLIDES:
                    raise HTTPException(422, "PDF must have 1 to 100 pages")
                slides = []
                for index, page in enumerate(document, 1):
                    text = page.get_text().strip()[:6000]
                    item = {"slide_number": index, "title": f"Page {index}", "text": text}
                    if index <= 4:
                        pixmap = page.get_pixmap(matrix=fitz.Matrix(1.2, 1.2), alpha=False)
                        image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")
                        image.thumbnail((1600, 1600))
                        output = io.BytesIO()
                        image.save(output, "JPEG", quality=82)
                        item["image_bytes"] = output.getvalue()
                    slides.append(item)
                return slides
        except HTTPException:
            raise
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(422, "This is not a readable PDF") from exc
    if suffix in (".png", ".jpg", ".jpeg", ".webp"):
        try:
            image = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
            image.thumbnail((1600, 1600))
            output = io.BytesIO()
            image.save(output, "JPEG", quality=85)
            return [{"slide_number": 1, "title": filename, "text": "", "image_bytes": output.getvalue()}]
        except (UnidentifiedImageError, ValueError, OSError, Image.DecompressionBombError) as exc:
            raise HTTPException(422, "This is not a readable image") from exc
    raise HTTPException(422, "Supported files: PPT, PPTX, PDF, DOC, DOCX, PNG, JPG, WEBP, TXT, MD")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/phone")
def phone():
    return FileResponse(ROOT / "static" / "phone.html")


@app.post("/api/sessions")
def create_session():
    sid = new_id("sess")
    started = int(time.time() * 1000)
    token = secrets.token_urlsafe(18)
    with conn() as db:
        db.execute("""INSERT INTO sessions
                   (id, started_epoch_ms, join_token, join_expires_epoch_ms)
                   VALUES (?,?,?,?)""",
                   (sid, started, token, started + PHONE_JOIN_LIFETIME_MS))
        for name in ("Analysis of Algorithms", "Deep Learning"):
            db.execute("INSERT INTO classes VALUES (?,?,?,?)", (new_id("class"), sid, name, started))
    return {"session_id": sid, "started_epoch_ms": started}


def phone_capture_url(request, session_id, token):
    base = str(request.base_url).rstrip("/")
    return f"{base}/phone?{urlencode({'session': session_id, 'token': token})}"


@app.get("/api/sessions/{session_id}/phone-join")
def phone_join(session_id: str, request: Request):
    with conn() as db:
        token, expires = phone_join_credentials(db, session_id)
    capture_url = phone_capture_url(request, session_id, token)
    return {"session_id": session_id, "capture_url": capture_url,
            "qr_url": f"/api/sessions/{session_id}/phone-join-qr",
            "expires_epoch_ms": expires,
            "localhost_warning": request.url.hostname in ("127.0.0.1", "localhost", "::1")}


@app.get("/api/sessions/{session_id}/phone-join-qr")
def phone_join_qr(session_id: str, request: Request):
    with conn() as db:
        token, _ = phone_join_credentials(db, session_id)
    image = qrcode.make(phone_capture_url(request, session_id, token))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return Response(output.getvalue(), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    with conn() as db:
        current = session(db, session_id)
        rows = db.execute("SELECT * FROM snapshots WHERE session_id=? ORDER BY captured_at_ms DESC, slide_number", (session_id,)).fetchall()
        messages = db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY created_at_ms, rowid", (session_id,)).fetchall()
        segments = db.execute("SELECT * FROM segments WHERE session_id=? ORDER BY start_ms DESC LIMIT 100", (session_id,)).fetchall()
        chats = db.execute("SELECT id, title, class_id, created_at_ms FROM chats WHERE session_id=? ORDER BY created_at_ms DESC, rowid DESC", (session_id,)).fetchall()
        classes = db.execute("SELECT id, name FROM classes WHERE session_id=? ORDER BY created_at_ms, rowid", (session_id,)).fetchall()
        decks = db.execute("SELECT * FROM decks WHERE session_id=? ORDER BY created_at_ms DESC, rowid DESC", (session_id,)).fetchall()
    return {"session_id": session_id, "started_epoch_ms": current["started_epoch_ms"],
            "server_epoch_ms": int(time.time() * 1000),
            "snapshots": [snapshot_public(r) for r in rows],
            "transcripts": [dict(r) for r in reversed(segments)],
            "messages": [message_public(r) for r in messages],
            "chats": [{"chat_id": r["id"], "title": r["title"], "class_id": r["class_id"], "created_at_ms": r["created_at_ms"]} for r in chats],
            "classes": [{"class_id": r["id"], "name": r["name"]} for r in classes],
            "decks": [deck_public(r) for r in decks]}


@app.post("/api/sessions/{session_id}/decks")
async def upload_deck(session_id: str, file: UploadFile = File(...), class_id: str = Form(...)):
    filename = Path(file.filename or "").name[:120]
    if Path(filename).suffix.lower() not in MATERIAL_EXTENSIONS:
        raise HTTPException(422, "Supported files: PPT, PPTX, PDF, DOC, DOCX, PNG, JPG, WEBP, TXT, MD")
    with conn() as db:
        session(db, session_id)
        if not db.execute("SELECT 1 FROM classes WHERE id=? AND session_id=?", (class_id, session_id)).fetchone():
            raise HTTPException(404, "Class not found in this session")
    raw = await file.read(MAX_DECK_UPLOAD + 1)
    if len(raw) > MAX_DECK_UPLOAD:
        raise HTTPException(413, "File exceeds 20 MB")
    slides = await asyncio.to_thread(extract_material, raw, filename)
    deck_id = new_id("deck")
    saved_paths = []
    source_path = None
    if Path(filename).suffix.lower() == ".pdf":
        source_path = DATA / f"{deck_id}.pdf"
        source_path.write_bytes(raw)
        saved_paths.append(source_path)
    for slide in slides:
        if slide.get("image_bytes"):
            path = DATA / f"{deck_id}_{slide['slide_number']}.jpg"
            path.write_bytes(slide.pop("image_bytes"))
            slide["image_path"] = str(path)
            saved_paths.append(path)
    with conn() as db:
        try:
            db.execute("INSERT INTO decks (id,session_id,class_id,filename,slide_count,created_at_ms,source_path) VALUES (?,?,?,?,?,?,?)",
                       (deck_id, session_id, class_id, filename, len(slides), int(time.time() * 1000),
                        str(source_path) if source_path else None))
            db.executemany("INSERT INTO deck_slides (deck_id,slide_number,title,text,image_path) VALUES (?,?,?,?,?)",
                           [(deck_id, slide["slide_number"], slide["title"], slide["text"], slide.get("image_path")) for slide in slides])
            deck = db.execute("SELECT * FROM decks WHERE id=?", (deck_id,)).fetchone()
        except Exception:
            for path in saved_paths:
                path.unlink(missing_ok=True)
            raise
    return deck_public(deck)


def material_image_path(deck, slide):
    if slide["image_path"] and Path(slide["image_path"]).exists():
        return Path(slide["image_path"])
    if Path(deck["filename"]).suffix.lower() != ".pdf" or not deck["source_path"]:
        return None
    source = Path(deck["source_path"])
    if not source.exists():
        return None
    with fitz.open(source) as document:
        page = document[slide["slide_number"] - 1]
        pixmap = page.get_pixmap(matrix=fitz.Matrix(1.2, 1.2), alpha=False)
        image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")
        image.thumbnail((1600, 1600))
        path = DATA / f"{deck['id']}_{slide['slide_number']}.jpg"
        image.save(path, "JPEG", quality=82)
    with conn() as db:
        db.execute("UPDATE deck_slides SET image_path=? WHERE deck_id=? AND slide_number=?",
                   (str(path), deck["id"], slide["slide_number"]))
    return path


@app.get("/api/sessions/{session_id}/materials/{deck_id}/pages/{page_number}/image")
def material_page_image(session_id: str, deck_id: str, page_number: int):
    with conn() as db:
        deck = db.execute("SELECT * FROM decks WHERE id=? AND session_id=?", (deck_id, session_id)).fetchone()
        slide = db.execute("SELECT * FROM deck_slides WHERE deck_id=? AND slide_number=?", (deck_id, page_number)).fetchone() if deck else None
    if not deck or not slide:
        raise HTTPException(404, "Uploaded page not found")
    path = material_image_path(deck, slide)
    if not path:
        raise HTTPException(404, "This page has no visual preview")
    return FileResponse(path, media_type="image/jpeg")


class ClassInput(BaseModel):
    name: str = Field(min_length=2, max_length=60)


@app.post("/api/sessions/{session_id}/classes")
def create_class(session_id: str, body: ClassInput):
    name = body.name.strip()
    if len(name) < 2:
        raise HTTPException(422, "Course name is too short")
    with conn() as db:
        session(db, session_id)
        if db.execute("SELECT 1 FROM classes WHERE session_id=? AND lower(name)=lower(?)", (session_id, name)).fetchone():
            raise HTTPException(409, "That course already exists")
        class_id = new_id("class")
        db.execute("INSERT INTO classes VALUES (?,?,?,?)", (class_id, session_id, name, int(time.time() * 1000)))
    return {"class_id": class_id, "name": name}


class ChatInput(BaseModel):
    class_id: str | None = None


@app.post("/api/sessions/{session_id}/chats")
def create_chat(session_id: str, body: ChatInput | None = None):
    with conn() as db:
        session(db, session_id)
        class_id = body.class_id if body else None
        if class_id:
            if not db.execute("SELECT 1 FROM classes WHERE id=? AND session_id=?", (class_id, session_id)).fetchone():
                raise HTTPException(404, "Class not found in this session")
        else:
            class_id = db.execute("SELECT id FROM classes WHERE session_id=? ORDER BY created_at_ms LIMIT 1", (session_id,)).fetchone()["id"]
        chat_id = new_id("chat")
        created_at_ms = int(time.time() * 1000)
        db.execute("INSERT INTO chats (id,session_id,title,created_at_ms,class_id) VALUES (?,?,?,?,?)", (chat_id, session_id, "New chat", created_at_ms, class_id))
    return {"chat_id": chat_id, "title": "New chat", "class_id": class_id, "created_at_ms": created_at_ms}


@app.patch("/api/sessions/{session_id}/chats/{chat_id}")
def move_chat(session_id: str, chat_id: str, body: ChatInput):
    with conn() as db:
        session(db, session_id)
        if not db.execute("SELECT 1 FROM chats WHERE id=? AND session_id=?", (chat_id, session_id)).fetchone():
            raise HTTPException(404, "Chat not found in this session")
        if not body.class_id or not db.execute("SELECT 1 FROM classes WHERE id=? AND session_id=?", (body.class_id, session_id)).fetchone():
            raise HTTPException(404, "Class not found in this session")
        db.execute("UPDATE chats SET class_id=? WHERE id=?", (body.class_id, chat_id))
    return {"chat_id": chat_id, "class_id": body.class_id}


@app.delete("/api/sessions/{session_id}/chats/{chat_id}")
def delete_chat(session_id: str, chat_id: str):
    with conn() as db:
        session(db, session_id)
        if not db.execute("SELECT 1 FROM chats WHERE id=? AND session_id=?", (chat_id, session_id)).fetchone():
            raise HTTPException(404, "Chat not found in this session")
        db.execute("DELETE FROM messages WHERE chat_id=? AND session_id=?", (chat_id, session_id))
        db.execute("DELETE FROM chats WHERE id=? AND session_id=?", (chat_id, session_id))
    return {"deleted_chat_id": chat_id}


@app.get("/api/sessions/{session_id}/context")
def get_full_context(session_id: str):
    with conn() as db:
        current = session(db, session_id)
        snapshots = db.execute("SELECT * FROM snapshots WHERE session_id=? ORDER BY captured_at_ms, slide_number", (session_id,)).fetchall()
        segments = db.execute("SELECT * FROM segments WHERE session_id=? ORDER BY start_ms", (session_id,)).fetchall()
        messages = db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY created_at_ms", (session_id,)).fetchall()
        recaps = db.execute("SELECT * FROM recaps WHERE session_id=? ORDER BY created_at_ms", (session_id,)).fetchall()
    return {"session_id": session_id, "started_epoch_ms": current["started_epoch_ms"],
            "transcripts": [dict(row) for row in segments],
            "snapshots": [snapshot_public(row) for row in snapshots],
            "messages": [message_public(row) for row in messages],
            "recaps": [recap_public(row) for row in recaps]}


@app.post("/api/sessions/{session_id}/snapshots")
async def add_snapshot(session_id: str, image: UploadFile = File(...), source: str = Form("camera"),
                       capture_reason: str = Form("timer"), join_token: str | None = Form(None)):
    if source not in ("camera", "screen", "phone"):
        raise HTTPException(422, "source must be camera, screen, or phone")
    if capture_reason not in ("start", "timer", "caption", "manual", "phone"):
        raise HTTPException(422, "invalid capture reason")
    if source == "phone":
        with conn() as db:
            current = session(db, session_id)
            valid_token = bool(join_token and current["join_token"] and
                               secrets.compare_digest(join_token, current["join_token"]))
            if not valid_token or not current["join_expires_epoch_ms"] or current["join_expires_epoch_ms"] <= int(time.time() * 1000):
                raise HTTPException(403, "Phone join link is invalid or expired")
    raw = await image.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "Image too large")
    try:
        opened = Image.open(io.BytesIO(raw))
        opened.verify()
        picture = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    except (UnidentifiedImageError, ValueError, OSError, Image.DecompressionBombError):
        raise HTTPException(422, "Invalid image")
    if picture.width < 320 or picture.height < 180:
        raise HTTPException(422, "Image is too small")
    with conn() as db:
        row = save_snapshot(db, session_id, picture, source, capture_reason=capture_reason)
    return snapshot_public(row)


@app.post("/api/sessions/{session_id}/slides")
async def add_slides(session_id: str, files: list[UploadFile] = File(...)):
    deck_id = new_id("deck")
    pictures = []
    for upload in files:
        raw = await upload.read(MAX_SLIDE_UPLOAD + 1)
        if len(raw) > MAX_SLIDE_UPLOAD:
            raise HTTPException(413, f"{upload.filename or 'Slide file'} is too large")
        content_type = upload.content_type or ""
        if content_type == "application/pdf" or (upload.filename or "").lower().endswith(".pdf"):
            try:
                document = fitz.open(stream=raw, filetype="pdf")
                if document.page_count > 100:
                    raise HTTPException(422, "Slide deck exceeds 100 pages")
                for page in document:
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                    pictures.append(Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB"))
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(422, "Invalid PDF slide deck") from exc
        else:
            try:
                opened = Image.open(io.BytesIO(raw))
                opened.verify()
                pictures.append(ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB"))
            except (UnidentifiedImageError, ValueError, OSError, Image.DecompressionBombError) as exc:
                raise HTTPException(422, f"Invalid slide image: {upload.filename or 'upload'}") from exc
    if not pictures:
        raise HTTPException(422, "Upload at least one PDF or slide image")
    with conn() as db:
        session(db, session_id)
        uploaded_at_ms = max(0, int(time.time() * 1000) - session(db, session_id)["started_epoch_ms"])
        rows = [save_snapshot(db, session_id, picture, "slide", uploaded_at_ms,
                              deck_id, index + 1, "slide-upload")
                for index, picture in enumerate(pictures)]
    return {"deck_id": deck_id, "slides": [snapshot_public(row) for row in rows]}


@app.get("/api/snapshots/{snapshot_id}/image")
def image(snapshot_id: str):
    with conn() as db:
        row = db.execute("SELECT path FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Snapshot not found")
    return FileResponse(row["path"], media_type="image/jpeg")


@app.get("/api/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: str):
    with conn() as db:
        row = db.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Snapshot not found")
    return snapshot_public(row)


class Region(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)


class Question(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    language: str = Field(default="en", pattern="^(en|es|fr|zh|hi|ko|ja|de|pt|ar)$")
    snapshot_id: str | None = None
    snapshot_ids: list[str] | None = None
    region: Region | None = None
    asked_at_ms: int | None = Field(default=None, ge=0)
    chat_id: str | None = None
    selected_deck_ids: list[str] | None = None


class Segment(BaseModel):
    segment_id: str | None = None
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    speaker: str = "lecturer"
    language: str = "en"
    text: str = Field(min_length=1)
    translated_text: str | None = None
    translated_language: str | None = None


class RecapRequest(BaseModel):
    language: str = Field(default="en", pattern="^(en|es|fr|zh|hi|ko|ja|de|pt|ar)$")


@app.get("/api/sessions/{session_id}/transcript")
def get_transcript(session_id: str):
    with conn() as db:
        session(db, session_id)
        rows = db.execute("SELECT id, start_ms, end_ms, speaker, language, text FROM segments "
                          "WHERE session_id=? ORDER BY start_ms DESC, rowid DESC LIMIT 20", (session_id,)).fetchall()
    return {"segments": [{"segment_id": row["id"], "start_ms": row["start_ms"],
                          "end_ms": row["end_ms"], "speaker": row["speaker"],
                          "language": row["language"], "text": row["text"]} for row in reversed(rows)]}


@app.post("/api/sessions/{session_id}/transcript")
def add_segment(session_id: str, segment: Segment):
    if segment.end_ms < segment.start_ms:
        raise HTTPException(422, "end_ms precedes start_ms")
    with conn() as db:
        session(db, session_id)
        ident = segment.segment_id or new_id("seg")
        db.execute("""INSERT INTO segments
                   (id, session_id, start_ms, end_ms, speaker, language, text,
                    translated_text, translated_language)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     start_ms=excluded.start_ms, end_ms=excluded.end_ms,
                     speaker=excluded.speaker, language=excluded.language,
                     text=excluded.text, translated_text=excluded.translated_text,
                     translated_language=excluded.translated_language""",
                   (ident, session_id, segment.start_ms, segment.end_ms,
                    segment.speaker, segment.language, segment.text,
                    segment.translated_text, segment.translated_language))
    return {"segment_id": ident, "session_id": session_id}


def message_public(row):
    return {"message_id": row["id"], "chat_id": row["chat_id"], "snapshot_id": row["snapshot_id"],
            "snapshot_ids": json.loads(row["snapshot_ids_json"]) if row["snapshot_ids_json"] else ([row["snapshot_id"]] if row["snapshot_id"] else []),
            "region": json.loads(row["region_json"]) if row["region_json"] else None,
            "question": row["question"], "result": json.loads(row["answer_json"]),
            "created_at_ms": row["created_at_ms"]}


def spoken_math(text):
    text = re.sub(r"\\{2,}", lambda _: "\\", text)
    text = re.sub(r"\\frac\{([^{}]+)\}\{([^{}]+)\}", r"\1 over \2", text)
    text = re.sub(r"\\sqrt\{([^{}]+)\}", r"square root of \1", text)
    text = re.sub(r"\\text\{([^{}]+)\}", r"\1", text)
    for command, words in ((r"\\(?:leq|le)\b", " less than or equal to "),
                           (r"\\(?:geq|ge)\b", " greater than or equal to "),
                           (r"\\(?:times|cdot)\b", " times "),
                           (r"\\(?:rightarrow|to)\b", " approaches ")):
        text = re.sub(command, words, text)
    text = re.sub(r"\\([a-zA-Z]+)\b", r"\1", text)
    text = re.sub(r"\bO\s*\(([^)]+)\)", r"big O of \1", text)
    return re.sub(r"\s+", " ", text.replace("{", "").replace("}", "")).strip()


def answer_for_speech(answer):
    text = re.sub(r"(?im)^\s*Sources:\s*(?:\[\[[FAM]\d+\]\]\s*)+$", "", answer)
    text = re.sub(r"\[\[(?:[FAM]\d+|R1)\]\]", "", text)
    text = re.sub(r"!?\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\$\$([\s\S]*?)\$\$|\$([^$\n]+)\$|\\\((.*?)\\\)",
                  lambda match: spoken_math(next(part for part in match.groups() if part is not None)), text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*|^\s*[-*+]\s+|^\s*\d+\.\s+", "", text)
    text = re.sub(r"\*\*|__|`", "", text)
    text = re.sub(r"(?m)^\s*>\s*", "", text)
    text = re.sub(r"\n+", ". ", text)
    text = re.sub(r"(?:\.\s*){2,}", ". ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+([,.!?;:])", r"\1", text)


@app.post("/api/sessions/{session_id}/messages/{message_id}/speech")
async def answer_speech(session_id: str, message_id: str):
    with conn() as db:
        session(db, session_id)
        row = db.execute("SELECT answer_json FROM messages WHERE id=? AND session_id=?",
                         (message_id, session_id)).fetchone()
    if not row:
        raise HTTPException(404, "Answer not found in this lecture")
    text = answer_for_speech(json.loads(row["answer_json"]).get("answer", ""))
    if not text:
        raise HTTPException(422, "This answer has no readable text")
    if len(text) > 4096:
        raise HTTPException(413, "This answer is too long for read aloud")
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise HTTPException(503, "Set OPENAI_API_KEY in .env and restart the server to enable read aloud")
    payload = {"model": TTS_MODEL, "input": text, "voice": TTS_VOICE,
               "instructions": "Speak warmly and naturally, like a patient tutor explaining a concept.",
               "response_format": "wav"}
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post("https://api.openai.com/v1/audio/speech",
                                         headers={"Authorization": f"Bearer {key}"}, json=payload)
        response.raise_for_status()
        audio = response.content
        if len(audio) > 25 * 1024 * 1024 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
            raise ValueError("Invalid WAV audio")
    except httpx.HTTPStatusError as exc:
        logger.warning("TTS model=%s HTTP status=%s", TTS_MODEL, exc.response.status_code)
        raise HTTPException(502, model_error_detail(exc.response.status_code)) from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "Voice generation took too long. Try again.") from exc
    except httpx.RequestError as exc:
        logger.warning("TTS network error: %s", type(exc).__name__)
        raise HTTPException(502, "Could not connect to the voice service. Try again.") from exc
    except ValueError as exc:
        logger.warning("TTS model=%s returned invalid audio", TTS_MODEL)
        raise HTTPException(502, "The voice service returned no playable audio. Try again.") from exc
    return Response(audio, media_type="audio/wav", headers={"Cache-Control": "private, no-store"})


def image_part(data):
    return {"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(data).decode(),
            "detail": "high"}


def nearby_segments(db, session_id, anchor_ms, limit=12):
    rows = db.execute("""
        SELECT * FROM segments
        WHERE session_id=:session_id
          AND start_ms<=:window_end
          AND end_ms>=:window_start
        ORDER BY
          CASE
            WHEN end_ms<:anchor THEN :anchor-end_ms
            WHEN start_ms>:anchor THEN start_ms-:anchor
            ELSE 0
          END,
          ABS(end_ms-:anchor),
          start_ms DESC
        LIMIT :limit
    """, {"session_id": session_id, "window_end": anchor_ms + 15000,
          "window_start": max(0, anchor_ms - 45000), "anchor": anchor_ms,
          "limit": limit}).fetchall()
    return sorted(rows, key=lambda row: row["start_ms"])


def rank_material_pages(decks, slides_by_deck, question, limit=10):
    stop = {"what", "which", "when", "where", "why", "how", "does", "this", "that", "these", "those",
            "the", "and", "for", "with", "from", "about", "can", "you", "please", "explain", "lecture"}
    terms = set(re.findall(r"\w+", question.lower())) - stop
    terms = {term for term in terms if len(term) > 2}
    ranked = []
    for deck_index, deck in enumerate(decks):
        filename_words = set(re.findall(r"\w+", deck["filename"].lower()))
        for slide in slides_by_deck[deck["id"]]:
            title_words = set(re.findall(r"\w+", slide["title"].lower()))
            body_words = set(re.findall(r"\w+", slide["text"].lower()))
            score = 3 * len(terms & (filename_words | title_words)) + len(terms & body_words)
            ranked.append((score, deck_index, slide["slide_number"], deck, slide))
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    chosen = []
    seen_decks = set()
    for item in ranked:
        if item[3]["id"] not in seen_decks and len(chosen) < limit:
            chosen.append(item)
            seen_decks.add(item[3]["id"])
    selected_ids = {(item[3]["id"], item[2]) for item in chosen}
    for item in ranked:
        if len(chosen) >= limit:
            break
        if (item[3]["id"], item[2]) not in selected_ids:
            chosen.append(item)
            selected_ids.add((item[3]["id"], item[2]))
    chosen.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [(item[3], item[4]) for item in chosen]


def deck_context(decks, slides_by_deck, visual_ids=None, labels=None):
    if not decks:
        return None, {}
    visual_ids = visual_ids or set()
    labels = labels or {}
    count = sum(len(slides_by_deck[deck["id"]]) for deck in decks)
    per_slide = max(60, min(1200, 16000 // max(1, count) - 90))
    references = {}
    entries = []
    for deck in decks:
        kind = deck_public(deck)["kind"]
        unit = "slide" if kind == "slides" else "page" if kind == "pdf" else "section" if kind in ("document", "text") else "image"
        slides = []
        for slide in slides_by_deck[deck["id"]]:
            slide_id = f"{deck['id']}:{slide['slide_number']}"
            references[slide_id] = f"{deck['filename']} · {unit} {slide['slide_number']}"
            slides.append({"source_label": labels.get(slide_id), "slide_id": slide_id, "slide_number": slide["slide_number"],
                           "title": slide["title"], "extracted_text": slide["text"][:per_slide],
                           "visual_supplied": slide_id in visual_ids,
                           "visual_note": "Embedded raster image only; slide shapes and charts are not rendered" if kind == "slides" and slide_id in visual_ids else None})
        entries.append({"filename": deck["filename"], "kind": kind, "slides": slides})
    return {"decks": entries, "limitation": "Only the most relevant uploaded pages are included. Visuals are available only when visual_supplied is true. PPT visual context is an embedded raster image, not a rendered slide; slide shapes/charts and Word embedded images are not extracted."}, references


ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "visible_evidence": {"type": "array", "items": {"type": "string"}},
        "inferred_explanation": {"type": "array", "items": {"type": "string"}},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
        "transcript_segment_ids": {"type": "array", "items": {"type": "string"}},
        "slide_references": {"type": "array", "items": {"type": "string"}},
        "citations": {"type": "array", "items": {"type": "string"}},
        "status": {"type": "string", "enum": ["supported", "partial", "unsupported"]},
    },
    "required": ["answer", "visible_evidence", "inferred_explanation", "uncertainties", "transcript_segment_ids", "slide_references", "citations", "status"],
    "additionalProperties": False,
}


def parse_model_json(response):
    data = response.json()
    if data.get("status") != "completed":
        raise ValueError("Incomplete model response")
    content = [part.get("text", "") for item in data.get("output", []) if item.get("type") == "message"
               for part in item.get("content", []) if part.get("type") == "output_text"]
    if not content:
        raise ValueError("Model returned no text")
    result = json.loads("".join(content))
    if not isinstance(result, dict):
        raise ValueError("Model returned invalid JSON")
    return result


def model_error_detail(status_code):
    return {
        400: "The model rejected this request. Try a different capture; check the server log if it repeats.",
        401: "The OpenAI API key was rejected. Check OPENAI_API_KEY in .env and restart the server.",
        403: "This OpenAI API key cannot access the model. Check the key's project and model access.",
        413: "The captured images are too large for the model request. Try again with a different capture.",
        429: "OpenAI rate limit or quota reached. Wait and retry, or check your project's quota.",
        500: "OpenAI had a temporary server error. Retry this question.",
        503: "OpenAI is temporarily unavailable. Retry this question shortly.",
    }.get(status_code, f"OpenAI returned HTTP {status_code}. Retry this question or check the server log.")


@app.post("/api/sessions/{session_id}/ask")
async def ask(session_id: str, body: Question):
    if body.snapshot_ids is not None:
        if not 1 <= len(body.snapshot_ids) <= MAX_QUESTION_FRAMES or len(set(body.snapshot_ids)) != len(body.snapshot_ids):
            raise HTTPException(422, "Select 1 to 4 distinct frames")
        if body.snapshot_id and body.snapshot_id != body.snapshot_ids[0]:
            raise HTTPException(422, "The primary snapshot must be the first selected frame")
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise HTTPException(503, "Set OPENAI_API_KEY in .env and restart the server to enable answers")
    with conn() as db:
        current_session = session(db, session_id)
        if body.chat_id:
            chat = db.execute("SELECT id, class_id, created_at_ms FROM chats WHERE id=? AND session_id=?", (body.chat_id, session_id)).fetchone()
            if not chat:
                raise HTTPException(404, "Chat not found in this session")
            chat_id = chat["id"]
        else:
            chat = db.execute("SELECT id, class_id, created_at_ms FROM chats WHERE session_id=? ORDER BY created_at_ms LIMIT 1", (session_id,)).fetchone()
            chat_id = chat["id"] if chat else None
        class_row = ({"id": chat["class_id"]} if chat else db.execute(
            "SELECT id FROM classes WHERE session_id=? ORDER BY created_at_ms LIMIT 1", (session_id,)).fetchone())
        if not class_row:
            class_id = new_id("class")
            db.execute("INSERT INTO classes (id,session_id,name,created_at_ms) VALUES (?,?,?,?)",
                       (class_id, session_id, "Current lecture", int(time.time() * 1000)))
        else:
            class_id = class_row["id"]
        available_decks = db.execute("SELECT * FROM decks WHERE session_id=? AND class_id=? ORDER BY created_at_ms DESC, rowid DESC",
                                     (session_id, class_id)).fetchall()
        selected_ids = body.selected_deck_ids
        if selected_ids is None:
            selected_ids = [available_decks[0]["id"]] if available_decks else []
        if len(selected_ids) > 10 or len(selected_ids) != len(set(selected_ids)):
            raise HTTPException(422, "Select up to 10 distinct files")
        available_by_id = {row["id"]: row for row in available_decks}
        if any(deck_id not in available_by_id for deck_id in selected_ids):
            raise HTTPException(422, "A selected file does not belong to this course")
        decks = [available_by_id[deck_id] for deck_id in selected_ids]
        slides_by_deck = {deck["id"]: db.execute("SELECT * FROM deck_slides WHERE deck_id=? ORDER BY slide_number",
                                                 (deck["id"],)).fetchall() for deck in decks}
        if body.snapshot_ids is not None:
            selected_shots = [db.execute("SELECT * FROM snapshots WHERE id=? AND session_id=?", (ident, session_id)).fetchone()
                              for ident in body.snapshot_ids]
            if any(item is None for item in selected_shots):
                raise HTTPException(422, "A selected frame does not belong to this session")
            shot = selected_shots[0]
        elif body.snapshot_id:
            shot = db.execute("SELECT * FROM snapshots WHERE id=? AND session_id=?", (body.snapshot_id, session_id)).fetchone()
            if not shot:
                raise HTTPException(404, "Snapshot not found in this lecture")
            selected_shots = [shot]
        else:
            shot = None
            selected_shots = []
        chat_start_ms = max(0, chat["created_at_ms"] - current_session["started_epoch_ms"]) if chat else 0
        recent_start_ms = 0 if shot and shot["captured_at_ms"] <= chat_start_ms else chat_start_ms
        recent_shots = (db.execute("SELECT * FROM snapshots WHERE session_id=? AND captured_at_ms<=? AND captured_at_ms>? AND id!=? ORDER BY captured_at_ms DESC LIMIT 2",
                                   (session_id, shot["captured_at_ms"], recent_start_ms, shot["id"])).fetchall()
                        if shot and len(selected_shots) == 1 else [])
        previous = db.execute(
            "SELECT question, answer_json FROM messages WHERE session_id=? AND (? IS NULL OR chat_id=?) ORDER BY created_at_ms DESC LIMIT 8",
            (session_id, chat_id, chat_id)).fetchall()
        latest_segment = db.execute("SELECT end_ms FROM segments WHERE session_id=? ORDER BY end_ms DESC LIMIT 1",
                                    (session_id,)).fetchone()
        if selected_shots:
            segment_by_id = {}
            for selected_shot in selected_shots:
                selected_anchor = (body.asked_at_ms if selected_shot["source"] == "slide" and body.asked_at_ms is not None
                                   else selected_shot["captured_at_ms"])
                segment_by_id.update({row["id"]: row for row in nearby_segments(db, session_id, selected_anchor)})
            segments = sorted(segment_by_id.values(), key=lambda row: row["start_ms"])
            anchor_ms = shot["captured_at_ms"]
        else:
            anchor_ms = body.asked_at_ms if body.asked_at_ms is not None else (latest_segment["end_ms"] if latest_segment else 0)
            segments = nearby_segments(db, session_id, anchor_ms)
            if not segments and latest_segment:
                anchor_ms = latest_segment["end_ms"]
                segments = nearby_segments(db, session_id, anchor_ms)
    if not shot and not segments and not decks:
        raise HTTPException(409, "No transcript, visual, or uploaded file context is available yet")
    if body.region and not shot:
        raise HTTPException(422, "A selected region requires a snapshot")
    if body.region and (body.region.x + body.region.width > 1.000001 or body.region.y + body.region.height > 1.000001):
        raise HTTPException(422, "Region extends outside image")
    image_bytes = Path(shot["path"]).read_bytes() if shot else None
    selected_materials = rank_material_pages(decks, slides_by_deck, body.question)
    selected_pages = {deck["id"]: [] for deck in decks}
    material_labels = {}
    for index, (deck, slide) in enumerate(selected_materials, 1):
        selected_pages[deck["id"]].append(slide)
        material_labels[f"{deck['id']}:{slide['slide_number']}"] = f"M{index}"
    image_slots = min(4, max(0, 6 - len(selected_shots) - len(recent_shots) - (1 if body.region else 0)))
    material_images = []
    for deck, slide in selected_materials:
        if len(material_images) >= image_slots:
            break
        path = material_image_path(deck, slide)
        if path:
            material_images.append((deck, slide, path))
    visual_ids = {f"{deck['id']}:{slide['slide_number']}" for deck, slide, _ in material_images}
    slides_context, valid_slide_ids = deck_context(decks, selected_pages, visual_ids, material_labels)
    source_map = {}
    frame_rows = [*selected_shots, *reversed(recent_shots)]
    for index, frame in enumerate(frame_rows, 1):
        source_map[f"F{index}"] = {"label": f"F{index}", "kind": "frame", "id": frame["id"],
                                   "title": f"Board frame · {frame['captured_at_ms'] // 60000}:{frame['captured_at_ms'] // 1000 % 60:02d}",
                                   "captured_at_ms": frame["captured_at_ms"],
                                   "image_url": f"/api/snapshots/{frame['id']}/image"}
    for index, segment in enumerate(segments, 1):
        source_map[f"A{index}"] = {"label": f"A{index}", "kind": "transcript", "id": segment["id"],
                                   "title": f"Lecture caption · {segment['start_ms'] // 60000}:{segment['start_ms'] // 1000 % 60:02d}",
                                   "start_ms": segment["start_ms"], "end_ms": segment["end_ms"],
                                   "excerpt": segment["text"]}
    for deck, slide in selected_materials:
        slide_id = f"{deck['id']}:{slide['slide_number']}"
        label = material_labels[slide_id]
        source_map[label] = {"label": label, "kind": "material", "id": slide_id,
                             "title": valid_slide_ids[slide_id], "excerpt": slide["text"][:1200],
                             "image_url": (f"/api/sessions/{session_id}/materials/{deck['id']}/pages/{slide['slide_number']}/image"
                                           if slide_id in visual_ids else None)}
    recent_conversation = []
    for row in reversed(previous):
        prior_result = json.loads(row["answer_json"])
        prior_deck_ids = prior_result.get("used_lecture_deck_ids")
        if prior_deck_ids is not None and not set(prior_deck_ids).issubset(selected_ids):
            continue
        if prior_deck_ids is None and (prior_result.get("used_lecture_deck") or prior_result.get("slide_references")):
            continue
        recent_conversation.append({"question": row["question"], "answer": prior_result.get("answer", "")})
    recent_conversation = recent_conversation[-4:]
    parts = [{"type": "input_text", "text": "You are LectureLens, a classroom learning companion. Answer the student's question in the requested language. Use relevant evidence from the provided board frames (F), lecture captions (A), and uploaded material pages (M). Put citations like [[F1]], [[A2]], and [[M3]] directly after the claims they support in answer, and list those exact labels in citations. Cite only labels present in source_catalog. Do not cite a source merely because its timestamp is nearby; check its content. When no live frame is supplied, visible_evidence must be empty: uploaded material images are background context, not proof of what appeared live. F1 is the primary frame and region R1 belongs only to F1. If a region R1 is selected and an answer claim specifically describes that region, place [[R1]] immediately after that claim; otherwise do not use [[R1]]. R1 is a region link, not a source citation. Preserve mathematical notation; flag unreadable details. Distinguish observed content from explanation and unsupported inference. Only claim to see uploaded material images when visual_supplied is true. Include only relevant exact slide IDs in slide_references and actual used transcript IDs in transcript_segment_ids. Use Markdown inside JSON strings and LaTeX delimiters for equations. Return concise structured JSON.\n" +
             json.dumps({"question": body.question, "language": body.language,
                         "region_label": "R1" if body.region else None,
                         "region_bbox_normalized": body.region.model_dump() if body.region else None,
                         "context_mode": "visual_and_transcript" if selected_shots else "transcript_only" if segments else "uploaded_material_only",
                         "context_anchor_ms": anchor_ms,
                         "source_catalog": [{key: value for key, value in source.items() if key not in ("image_url", "excerpt")}
                                            for source in source_map.values()],
                         "transcript": [{**dict(s), "source_label": f"A{index}"}
                                        for index, s in enumerate(segments, 1)],
                         "lecture_deck": slides_context,
                         "recent_conversation": recent_conversation}, ensure_ascii=False)}]
    for index, selected_shot in enumerate(selected_shots, 1):
        parts.append({"type": "input_text", "text": f"Selected frame F{index} at {selected_shot['captured_at_ms']} ms:"})
        parts.append(image_part(image_bytes if index == 1 else Path(selected_shot["path"]).read_bytes()))
    for index, older in enumerate(reversed(recent_shots), len(selected_shots) + 1):
        parts.append({"type": "input_text", "text": f"Earlier frame F{index} at {older['captured_at_ms']} ms. Use for context only; describe changes cautiously:"})
        parts.append(image_part(Path(older["path"]).read_bytes()))
    for deck, slide, path in material_images:
        slide_id = f"{deck['id']}:{slide['slide_number']}"
        parts.append({"type": "input_text", "text": f"Uploaded material {deck['filename']} · {material_labels[slide_id]}. This is background context, not a live camera frame:"})
        parts.append(image_part(path.read_bytes()))
    if body.region and image_bytes:
        im = Image.open(io.BytesIO(image_bytes))
        r = body.region
        box = (round(r.x * im.width), round(r.y * im.height), round((r.x+r.width)*im.width), round((r.y+r.height)*im.height))
        crop = io.BytesIO()
        im.crop(box).save(crop, "JPEG", quality=90)
        parts.append({"type": "input_text", "text": "Selected region R1, enlarged:"})
        parts.append(image_part(crop.getvalue()))
    payload = {"model": MODEL, "input": [{"role": "user", "content": parts}], "store": False,
               "text": {"format": {"type": "json_schema", "name": "lecture_answer",
                                   "strict": True, "schema": ANSWER_SCHEMA}},
               "max_output_tokens": 4096}
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            res = await client.post("https://api.openai.com/v1/responses",
                                    headers={"Authorization": f"Bearer {key}"}, json=payload)
        res.raise_for_status()
        result = parse_model_json(res)
    except httpx.HTTPStatusError as exc:
        logger.warning("Model HTTP status=%s", exc.response.status_code)
        raise HTTPException(502, model_error_detail(exc.response.status_code))
    except httpx.TimeoutException:
        logger.warning("Model request timed out")
        raise HTTPException(504, "The model took too long to answer. Retry this question.")
    except (httpx.RequestError, KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Model response error: %s", type(exc).__name__)
        raise HTTPException(502, "Could not read the model answer. Please try again")
    used_segment_ids = [segment_id for segment_id in result.get("transcript_segment_ids", [])
                        if segment_id in {row["id"] for row in segments}]
    source_timestamps = [{"segment_id": row["id"], "start_ms": row["start_ms"], "end_ms": row["end_ms"]}
                         for row in segments if row["id"] in used_segment_ids]
    answer_text = re.sub(r"\[\[([FAM]\d+)\]\]", lambda match: match.group(0) if match.group(1) in source_map else "", result["answer"])
    if not body.region:
        answer_text = answer_text.replace("[[R1]]", "")
    citations = []
    for label in re.findall(r"\[\[([FAM]\d+)\]\]", answer_text) + (result.get("citations") or []):
        if isinstance(label, str) and label in source_map and label not in citations:
            citations.append(label)
    if body.region and "[[R1]]" in answer_text and "F1" in source_map and "F1" not in citations:
        citations.insert(0, "F1")
    if not citations and result.get("status") != "unsupported":
        used_slide_ids = set(result.get("slide_references") or [])
        citations.extend(source["label"] for source in source_map.values()
                         if source["kind"] == "material" and source["id"] in used_slide_ids)
        citations.extend(source["label"] for source in source_map.values()
                         if source["kind"] == "transcript" and source["id"] in used_segment_ids)
        if shot and result.get("visible_evidence"):
            citations.insert(0, "F1")
    if citations and not re.search(r"\[\[[FAM]\d+\]\]", answer_text):
        answer_text += "\n\nSources: " + " ".join(f"[[{label}]]" for label in citations)
    result = {"answer": answer_text, "answer_language": body.language,
              "visible_evidence": result.get("visible_evidence", []) if shot else [],
              "inferred_explanation": result.get("inferred_explanation", []),
              "uncertainties": result.get("uncertainties", []),
              "transcript_segment_ids": used_segment_ids,
              "source_timestamps": source_timestamps,
              "slide_references": [valid_slide_ids[slide_id] for slide_id in (result.get("slide_references") or [])
                                   if isinstance(slide_id, str) and slide_id in valid_slide_ids],
              "citations": citations,
              "sources": [source_map[label] for label in citations],
              "used_lecture_deck": bool(decks),
              "used_lecture_deck_ids": selected_ids,
              "snapshot_at_ms": shot["captured_at_ms"] if shot else None,
              "status": result.get("status", "partial") if result.get("status") in ("supported", "partial", "unsupported") else "partial"}
    with conn() as db:
        if chat_id is None:
            chat_id = new_id("chat")
            default_class = db.execute("SELECT id FROM classes WHERE session_id=? ORDER BY created_at_ms LIMIT 1", (session_id,)).fetchone()["id"]
            db.execute("INSERT INTO chats (id,session_id,title,created_at_ms,class_id) VALUES (?,?,?,?,?)",
                       (chat_id, session_id, body.question[:64], int(time.time()*1000), default_class))
        else:
            db.execute("UPDATE chats SET title=? WHERE id=? AND title='New chat'", (body.question[:64], chat_id))
        ident = new_id("msg")
        db.execute("INSERT INTO messages (id,session_id,snapshot_id,region_json,question,answer_json,created_at_ms,chat_id,snapshot_ids_json) VALUES (?,?,?,?,?,?,?,?,?)",
                   (ident, session_id, shot["id"] if shot else None, body.region.model_dump_json() if body.region else None,
                    body.question, json.dumps(result, ensure_ascii=False), int(time.time()*1000), chat_id,
                    json.dumps([item["id"] for item in selected_shots])))
        row = db.execute("SELECT * FROM messages WHERE id=?", (ident,)).fetchone()
    return message_public(row)


def representative_snapshots(rows, limit=6):
    rows = list(rows)
    if len(rows) <= limit:
        return rows
    slides = [row for row in rows if row["source"] == "slide"]
    cameras = [row for row in rows if row["source"] != "slide"]

    def sample(items, count):
        if count <= 0 or not items:
            return []
        if len(items) <= count:
            return items
        return [items[round(index * (len(items) - 1) / (count - 1))]
                for index in range(count)] if count > 1 else [items[-1]]

    slide_count = min(3, len(slides))
    chosen = sample(slides, slide_count) + sample(cameras, limit - slide_count)
    return sorted(chosen, key=lambda row: (row["captured_at_ms"], row["slide_number"] or 0))


def recap_public(row):
    return {"recap_id": row["id"], "session_id": row["session_id"],
            "language": row["language"], "created_at_ms": row["created_at_ms"],
            "recap": json.loads(row["recap_json"]),
            "markdown_url": f"/api/recaps/{row['id']}/markdown"}


def elapsed_label(milliseconds):
    seconds = max(0, int(milliseconds) // 1000)
    return f"{seconds // 60}:{seconds % 60:02d}"


def recap_markdown(recap):
    lines = [f"# {recap.get('title', 'Lecture recap')}", "", recap.get("overview", ""), ""]
    lines.extend(["## Key concepts", ""])
    for item in recap.get("key_concepts", []):
        times = ", ".join(elapsed_label(value) for value in item.get("evidence_timestamps_ms", []))
        evidence = f" _(lecture: {times})_" if times else ""
        lines.append(f"- **{item.get('concept', '')}** — {item.get('explanation', '')}{evidence}")
    lines.extend(["", "## Important visuals", ""])
    for item in recap.get("important_visuals", []):
        lines.append(f"- **{elapsed_label(item.get('timestamp_ms', 0))}** — {item.get('description', '')} "
                     f"(`{item.get('snapshot_id', '')}`)")
    lines.extend(["", "## Formulas and terms", ""])
    for item in recap.get("formulas_and_terms", []):
        lines.append(f"- **{item.get('item', '')}** — {item.get('explanation', '')}")
    lines.extend(["", "## Review questions", ""])
    for index, item in enumerate(recap.get("review_questions", []), 1):
        lines.extend([f"{index}. **{item.get('question', '')}**", f"   {item.get('answer', '')}"])
    lines.extend(["", "## Unresolved questions", ""])
    lines.extend(f"- {item}" for item in recap.get("unresolved_questions", []))
    lines.extend(["", "## Next steps", ""])
    lines.extend(f"- {item}" for item in recap.get("next_steps", []))
    return "\n".join(lines).strip() + "\n"


@app.post("/api/sessions/{session_id}/recap")
async def create_recap(session_id: str, body: RecapRequest):
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise HTTPException(503, "Set OPENAI_API_KEY in .env and restart the server to generate a recap")
    with conn() as db:
        session(db, session_id)
        segments = db.execute("SELECT * FROM segments WHERE session_id=? ORDER BY start_ms",
                              (session_id,)).fetchall()
        snapshots = db.execute("SELECT * FROM snapshots WHERE session_id=? ORDER BY captured_at_ms, slide_number",
                               (session_id,)).fetchall()
        messages = db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY created_at_ms",
                              (session_id,)).fetchall()
    if not segments and not snapshots and not messages:
        raise HTTPException(409, "This lecture has no context to summarize yet")

    transcript_lines = [
        f"[{row['id']} {row['start_ms']}-{row['end_ms']}ms {row['language']}] {row['text']}" +
        (f" | translation ({row['translated_language']}): {row['translated_text']}"
         if row["translated_text"] else "")
        for row in segments
    ]
    transcript_text = "\n".join(transcript_lines)
    if len(transcript_text) > 120_000:
        transcript_text = transcript_text[:60_000] + "\n...[middle transcript omitted]...\n" + transcript_text[-60_000:]
    visual_metadata = [snapshot_public(row) for row in snapshots]
    qa_history = [{"question": row["question"],
                   "answer": json.loads(row["answer_json"]).get("answer", "")}
                  for row in messages[-40:]]
    parts = [{"type": "input_text", "text": "Create a grounded study recap in the requested language from this lecture context. "
                      "Use only supplied evidence; do not invent topics, formulas, or definitions. Preserve "
                      "mathematical notation. Attach session-relative millisecond timestamps to key concepts. "
                      "Important visuals must use supplied snapshot IDs. Turn uncertainties and unanswered "
                      "student questions into unresolved_questions. Return only the requested structured JSON.\n" +
                      json.dumps({"language": body.language, "transcript": transcript_text,
                                  "visual_timeline": visual_metadata,
                                  "student_questions_and_answers": qa_history}, ensure_ascii=False)}]
    for shot in representative_snapshots(snapshots):
        label = f"Representative {shot['source']} {shot['id']} at {shot['captured_at_ms']} ms"
        if shot["slide_number"]:
            label += f", slide {shot['slide_number']}"
        parts.extend([{"type": "input_text", "text": label + ":"}, image_part(Path(shot["path"]).read_bytes())])
    payload = {"model": MODEL, "input": [{"role": "user", "content": parts}], "store": False,
               "text": {"format": {"type": "json_schema", "name": "lecture_recap",
                                   "strict": True, "schema": RECAP_RESPONSE_SCHEMA}},
               "max_output_tokens": 4096}
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post("https://api.openai.com/v1/responses",
                                         headers={"Authorization": f"Bearer {key}"}, json=payload)
        response.raise_for_status()
        recap = parse_model_json(response)
        if not isinstance(recap.get("title"), str) or not isinstance(recap.get("overview"), str):
            raise ValueError("Invalid recap")
    except httpx.HTTPStatusError as exc:
        logger.warning("Recap model HTTP status=%s", exc.response.status_code)
        raise HTTPException(502, model_error_detail(exc.response.status_code)) from exc
    except (httpx.RequestError, ValueError, KeyError, json.JSONDecodeError) as exc:
        logger.warning("Recap model response error: %s", type(exc).__name__)
        raise HTTPException(502, "Could not generate the recap. Try again or check the server log.") from exc

    valid_snapshot_ids = {row["id"] for row in snapshots}
    recap["important_visuals"] = [item for item in recap.get("important_visuals", [])
                                    if item.get("snapshot_id") in valid_snapshot_ids]
    with conn() as db:
        recap_id = new_id("recap")
        db.execute("INSERT INTO recaps VALUES (?,?,?,?,?)",
                   (recap_id, session_id, body.language, json.dumps(recap, ensure_ascii=False),
                    int(time.time() * 1000)))
        row = db.execute("SELECT * FROM recaps WHERE id=?", (recap_id,)).fetchone()
    return recap_public(row)


@app.get("/api/sessions/{session_id}/recap")
def latest_recap(session_id: str):
    with conn() as db:
        session(db, session_id)
        row = db.execute("SELECT * FROM recaps WHERE session_id=? ORDER BY created_at_ms DESC LIMIT 1",
                         (session_id,)).fetchone()
    if not row:
        raise HTTPException(404, "No recap has been generated for this lecture")
    return recap_public(row)


@app.get("/api/sessions/{session_id}/recaps")
def list_recaps(session_id: str):
    with conn() as db:
        session(db, session_id)
        rows = db.execute("SELECT * FROM recaps WHERE session_id=? ORDER BY created_at_ms DESC",
                          (session_id,)).fetchall()
    return {"session_id": session_id, "recaps": [recap_public(row) for row in rows]}


@app.get("/api/recaps/{recap_id}/markdown")
def download_recap(recap_id: str):
    with conn() as db:
        row = db.execute("SELECT * FROM recaps WHERE id=?", (recap_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Recap not found")
    recap = json.loads(row["recap_json"])
    return PlainTextResponse(recap_markdown(recap), media_type="text/markdown",
                             headers={"Content-Disposition": "attachment; filename=lecture-recap.md"})
