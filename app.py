"""Single-process LectureLens hackathon prototype."""
import asyncio
import base64
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
DB = DATA / "lecturelens.sqlite3"
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
logger = logging.getLogger("lecturelens")
MAX_UPLOAD = 8 * 1024 * 1024
MAX_DECK_UPLOAD = 20 * 1024 * 1024
MAX_DECK_SLIDES = 100
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
    CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, started_epoch_ms INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS snapshots (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
      captured_at_ms INTEGER NOT NULL, source TEXT NOT NULL, width_px INTEGER NOT NULL,
      height_px INTEGER NOT NULL, path TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS segments (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
      start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, speaker TEXT NOT NULL,
      language TEXT NOT NULL, text TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
      snapshot_id TEXT NOT NULL, region_json TEXT, question TEXT NOT NULL,
      answer_json TEXT NOT NULL, created_at_ms INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
      title TEXT NOT NULL, created_at_ms INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS classes (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
      name TEXT NOT NULL, created_at_ms INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS decks (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
      class_id TEXT NOT NULL, filename TEXT NOT NULL, slide_count INTEGER NOT NULL,
      created_at_ms INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS deck_slides (deck_id TEXT NOT NULL, slide_number INTEGER NOT NULL,
      title TEXT NOT NULL, text TEXT NOT NULL, PRIMARY KEY(deck_id, slide_number));
    """)
    if "chat_id" not in {column[1] for column in db.execute("PRAGMA table_info(messages)")}:
        db.execute("ALTER TABLE messages ADD COLUMN chat_id TEXT")
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


def snapshot_public(row):
    return {"snapshot_id": row["id"], "session_id": row["session_id"],
            "captured_at_ms": row["captured_at_ms"], "source": row["source"],
            "width_px": row["width_px"], "height_px": row["height_px"],
            "image_url": f"/api/snapshots/{row['id']}/image"}


def deck_public(row):
    return {"deck_id": row["id"], "class_id": row["class_id"],
            "filename": row["filename"], "slide_count": row["slide_count"],
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
                slides.append({"slide_number": number, "title": lines[0][:120] if lines else f"Slide {number}", "text": text})
            if not any(slide["text"] for slide in slides):
                raise HTTPException(422, "No selectable text found in the slides. This version needs a deck with slide text.")
            return slides
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError, RuntimeError):
        raise HTTPException(422, "This is not a readable PowerPoint deck")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.post("/api/sessions")
def create_session():
    sid = new_id("sess")
    with conn() as db:
        db.execute("INSERT INTO sessions VALUES (?,?)", (sid, int(time.time() * 1000)))
        for name in ("Analysis of Algorithms", "Deep Learning"):
            db.execute("INSERT INTO classes VALUES (?,?,?,?)", (new_id("class"), sid, name, int(time.time() * 1000)))
    return {"session_id": sid}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    with conn() as db:
        session(db, session_id)
        rows = db.execute("SELECT * FROM snapshots WHERE session_id=? ORDER BY captured_at_ms DESC LIMIT 12", (session_id,)).fetchall()
        messages = db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY created_at_ms, rowid", (session_id,)).fetchall()
        chats = db.execute("SELECT id, title, class_id, created_at_ms FROM chats WHERE session_id=? ORDER BY created_at_ms DESC, rowid DESC", (session_id,)).fetchall()
        classes = db.execute("SELECT id, name FROM classes WHERE session_id=? ORDER BY created_at_ms, rowid", (session_id,)).fetchall()
        decks = db.execute("SELECT * FROM decks WHERE session_id=? ORDER BY created_at_ms DESC, rowid DESC", (session_id,)).fetchall()
    return {"session_id": session_id, "snapshots": [snapshot_public(r) for r in rows],
            "messages": [message_public(r) for r in messages],
            "chats": [{"chat_id": r["id"], "title": r["title"], "class_id": r["class_id"], "created_at_ms": r["created_at_ms"]} for r in chats],
            "classes": [{"class_id": r["id"], "name": r["name"]} for r in classes],
            "decks": [deck_public(r) for r in decks]}


@app.post("/api/sessions/{session_id}/decks")
async def upload_deck(session_id: str, file: UploadFile = File(...), class_id: str = Form(...)):
    filename = Path(file.filename or "").name[:120]
    if not filename.lower().endswith((".ppt", ".pptx")):
        raise HTTPException(422, "Choose a .ppt or .pptx PowerPoint file")
    with conn() as db:
        session(db, session_id)
        if not db.execute("SELECT 1 FROM classes WHERE id=? AND session_id=?", (class_id, session_id)).fetchone():
            raise HTTPException(404, "Class not found in this session")
    raw = await file.read(MAX_DECK_UPLOAD + 1)
    if len(raw) > MAX_DECK_UPLOAD:
        raise HTTPException(413, "PowerPoint file exceeds 20 MB")
    converted = await asyncio.to_thread(pptx_bytes, raw, filename)
    slides = await asyncio.to_thread(extract_deck_slides, converted)
    with conn() as db:
        deck_id = new_id("deck")
        db.execute("INSERT INTO decks VALUES (?,?,?,?,?,?)",
                   (deck_id, session_id, class_id, filename, len(slides), int(time.time() * 1000)))
        db.executemany("INSERT INTO deck_slides VALUES (?,?,?,?)",
                       [(deck_id, slide["slide_number"], slide["title"], slide["text"]) for slide in slides])
        deck = db.execute("SELECT * FROM decks WHERE id=?", (deck_id,)).fetchone()
    return deck_public(deck)


class ClassInput(BaseModel):
    name: str = Field(min_length=2, max_length=60)


@app.post("/api/sessions/{session_id}/classes")
def create_class(session_id: str, body: ClassInput):
    name = body.name.strip()
    if len(name) < 2:
        raise HTTPException(422, "Class name is too short")
    with conn() as db:
        session(db, session_id)
        if db.execute("SELECT 1 FROM classes WHERE session_id=? AND lower(name)=lower(?)", (session_id, name)).fetchone():
            raise HTTPException(409, "That class already exists")
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


@app.post("/api/sessions/{session_id}/snapshots")
async def add_snapshot(session_id: str, image: UploadFile = File(...), source: str = Form("camera")):
    if source not in ("camera", "screen"):
        raise HTTPException(422, "source must be camera or screen")
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
    picture.thumbnail((1920, 1080))
    sid = new_id("snap")
    path = DATA / f"{sid}.jpg"
    with conn() as db:
        started = session(db, session_id)["started_epoch_ms"]
        timestamp = max(0, int(time.time() * 1000) - started)
        picture.save(path, "JPEG", quality=88)
        db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?)",
                   (sid, session_id, timestamp, source, picture.width, picture.height, str(path)))
        row = db.execute("SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone()
    return snapshot_public(row)


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
    language: str = Field(default="en", pattern="^(en|hi)$")
    snapshot_id: str | None = None
    region: Region | None = None
    chat_id: str | None = None
    selected_deck_ids: list[str] | None = None


class Segment(BaseModel):
    segment_id: str | None = None
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    speaker: str = "lecturer"
    language: str = "en"
    text: str = Field(min_length=1)


@app.post("/api/sessions/{session_id}/transcript")
def add_segment(session_id: str, segment: Segment):
    if segment.end_ms < segment.start_ms:
        raise HTTPException(422, "end_ms precedes start_ms")
    with conn() as db:
        session(db, session_id)
        ident = segment.segment_id or new_id("seg")
        db.execute("INSERT OR REPLACE INTO segments VALUES (?,?,?,?,?,?,?)",
                   (ident, session_id, segment.start_ms, segment.end_ms,
                    segment.speaker, segment.language, segment.text))
    return {"segment_id": ident, "session_id": session_id}


def message_public(row):
    return {"message_id": row["id"], "chat_id": row["chat_id"], "snapshot_id": row["snapshot_id"],
            "region": json.loads(row["region_json"]) if row["region_json"] else None,
            "question": row["question"], "result": json.loads(row["answer_json"]),
            "created_at_ms": row["created_at_ms"]}


def image_part(data):
    return {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(data).decode()}}


def deck_context(decks, slides_by_deck):
    if not decks:
        return None, {}
    count = sum(len(slides_by_deck[deck["id"]]) for deck in decks)
    per_slide = max(60, min(1200, 16000 // max(1, count) - 90))
    references = {}
    entries = []
    for deck in decks:
        slides = []
        for slide in slides_by_deck[deck["id"]]:
            slide_id = f"{deck['id']}:{slide['slide_number']}"
            references[slide_id] = f"{deck['filename']} · slide {slide['slide_number']}"
            slides.append({"slide_id": slide_id, "slide_number": slide["slide_number"],
                           "title": slide["title"], "extracted_text": slide["text"][:per_slide]})
        entries.append({"filename": deck["filename"], "slides": slides})
    return {"decks": entries, "limitation": "Only selectable slide text was extracted; embedded images, charts, and handwritten content are not included."}, references


ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "visible_evidence": {"type": "array", "items": {"type": "string"}},
        "inferred_explanation": {"type": "array", "items": {"type": "string"}},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
        "transcript_segment_ids": {"type": "array", "items": {"type": "string"}},
        "slide_references": {"type": "array", "items": {"type": "string"}},
        "status": {"type": "string", "enum": ["supported", "partial", "unsupported"]},
    },
    "required": ["answer", "visible_evidence", "inferred_explanation", "uncertainties", "transcript_segment_ids", "slide_references", "status"],
    "additionalProperties": False,
}


def parse_model_answer(response):
    """Use the final text part; Gemini may include non-answer thought parts."""
    data = response.json()
    candidate = data["candidates"][0]
    texts = [part["text"] for part in candidate["content"]["parts"]
             if isinstance(part.get("text"), str) and not part.get("thought")]
    for content in reversed(texts):
        try:
            answer = json.loads(content)
        except json.JSONDecodeError:
            continue
        if isinstance(answer, dict) and isinstance(answer.get("answer"), str):
            return answer
    logger.warning("Unparseable model answer: finish_reason=%s text_parts=%s lengths=%s",
                   candidate.get("finishReason"), len(texts), [len(t) for t in texts])
    raise ValueError("Unparseable answer")


def model_error_detail(status_code):
    return {
        400: "The model rejected this request. Try a different capture; check the server log if it repeats.",
        401: "The Gemini API key was rejected. Check GEMINI_API_KEY in .env and restart the server.",
        403: "This API key cannot access the model. Check the key's project and model access.",
        413: "The captured images are too large for the model request. Try again with a different capture.",
        429: "Gemini rate limit or quota reached. Wait and retry, or check your project's quota.",
        500: "Gemini had a temporary server error. Retry this question.",
        503: "Gemini is temporarily unavailable. Retry this question shortly.",
    }.get(status_code, f"Gemini returned HTTP {status_code}. Retry this question or check the server log.")


@app.post("/api/sessions/{session_id}/ask")
async def ask(session_id: str, body: Question):
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise HTTPException(503, "Set GEMINI_API_KEY in server secrets to enable answers")
    with conn() as db:
        session(db, session_id)
        if body.chat_id:
            chat = db.execute("SELECT id, class_id FROM chats WHERE id=? AND session_id=?", (body.chat_id, session_id)).fetchone()
            if not chat:
                raise HTTPException(404, "Chat not found in this session")
            chat_id = chat["id"]
        else:
            chat = db.execute("SELECT id, class_id FROM chats WHERE session_id=? ORDER BY created_at_ms LIMIT 1", (session_id,)).fetchone()
            chat_id = chat["id"] if chat else None
        class_id = chat["class_id"] if chat else db.execute(
            "SELECT id FROM classes WHERE session_id=? ORDER BY created_at_ms LIMIT 1", (session_id,)).fetchone()["id"]
        available_decks = db.execute("SELECT * FROM decks WHERE session_id=? AND class_id=? ORDER BY created_at_ms DESC, rowid DESC",
                                     (session_id, class_id)).fetchall()
        selected_ids = body.selected_deck_ids
        if selected_ids is None:
            selected_ids = [available_decks[0]["id"]] if available_decks else []
        if len(selected_ids) > 10 or len(selected_ids) != len(set(selected_ids)):
            raise HTTPException(422, "Select up to 10 distinct PowerPoint decks")
        available_by_id = {row["id"]: row for row in available_decks}
        if any(deck_id not in available_by_id for deck_id in selected_ids):
            raise HTTPException(422, "A selected PowerPoint deck does not belong to this class")
        decks = [available_by_id[deck_id] for deck_id in selected_ids]
        slides_by_deck = {deck["id"]: db.execute("SELECT * FROM deck_slides WHERE deck_id=? ORDER BY slide_number",
                                                 (deck["id"],)).fetchall() for deck in decks}
        if body.snapshot_id:
            shot = db.execute("SELECT * FROM snapshots WHERE id=? AND session_id=?", (body.snapshot_id, session_id)).fetchone()
        else:
            shot = db.execute("SELECT * FROM snapshots WHERE session_id=? ORDER BY captured_at_ms DESC LIMIT 1", (session_id,)).fetchone()
        if not shot:
            raise HTTPException(409, "No snapshots yet. Start camera capture first")
        recent_shots = db.execute("SELECT * FROM snapshots WHERE session_id=? AND captured_at_ms<=? AND id!=? ORDER BY captured_at_ms DESC LIMIT 2",
                                  (session_id, shot["captured_at_ms"], shot["id"])).fetchall()
        previous = db.execute("SELECT question, answer_json FROM messages WHERE session_id=? AND chat_id=? ORDER BY created_at_ms DESC LIMIT 8", (session_id, chat_id)).fetchall()
        segments = db.execute("SELECT * FROM segments WHERE session_id=? AND start_ms<=? AND end_ms>=? ORDER BY start_ms LIMIT 8",
                              (session_id, shot["captured_at_ms"] + 15000, max(0, shot["captured_at_ms"] - 30000))).fetchall()
    if body.region and (body.region.x + body.region.width > 1.000001 or body.region.y + body.region.height > 1.000001):
        raise HTTPException(422, "Region extends outside image")
    image_bytes = Path(shot["path"]).read_bytes()
    slides_context, valid_slide_ids = deck_context(decks, slides_by_deck)
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
    parts = [{"text": "You are LectureLens, a classroom visual companion. Answer the student's question in the requested language. Use the supplied board image as visual evidence. Preserve mathematical symbols exactly; if illegible, say so. Distinguish what is visible from what is inferred or stated by a transcript or lecture deck. Uploaded deck text is background context, not proof of what is visible in the camera image. Deck extraction omits images and diagrams; do not claim to see them. Use slide text only when relevant, and cite its exact slide_id in slide_references; otherwise return an empty list. Never treat nearby timestamps or slide similarity as proof of a visual reference. Do not invent details. Within JSON string fields, use Markdown for lists and emphasis, $...$ for inline LaTeX equations, $$...$$ for display equations, and backticks for literal code or pseudocode. Do not wrap mathematical notation in code backticks. Return concise JSON with answer, visible_evidence (array of short strings), inferred_explanation (array of short strings), uncertainties (array of short strings), transcript_segment_ids (array of IDs actually used), slide_references (array of exact slide IDs actually used), and status (supported, partial, or unsupported).\n" +
             json.dumps({"question": body.question, "language": body.language,
                         "region_label": "R1" if body.region else None,
                         "region_bbox_normalized": body.region.model_dump() if body.region else None,
                         "snapshot_at_ms": shot["captured_at_ms"],
                         "transcript": [dict(s) for s in segments],
                         "lecture_deck": slides_context,
                         "recent_conversation": recent_conversation}, ensure_ascii=False)},
             image_part(image_bytes)]
    for older in reversed(recent_shots):
        parts.append({"text": f"Earlier snapshot at {older['captured_at_ms']} ms. Use for context only; describe changes cautiously:"})
        parts.append(image_part(Path(older["path"]).read_bytes()))
    if body.region:
        im = Image.open(io.BytesIO(image_bytes))
        r = body.region
        box = (round(r.x * im.width), round(r.y * im.height), round((r.x+r.width)*im.width), round((r.y+r.height)*im.height))
        crop = io.BytesIO()
        im.crop(box).save(crop, "JPEG", quality=90)
        parts.append({"text": "Selected region R1, enlarged:"})
        parts.append(image_part(crop.getvalue()))
    payload = {"contents": [{"role": "user", "parts": parts}],
               "generationConfig": {"responseMimeType": "application/json",
                                    "responseJsonSchema": ANSWER_SCHEMA,
                                    "maxOutputTokens": 4096}}
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            res = await client.post(f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
                                    headers={"x-goog-api-key": key}, json=payload)
        res.raise_for_status()
        result = parse_model_answer(res)
    except httpx.HTTPStatusError as exc:
        logger.warning("Model HTTP status=%s", exc.response.status_code)
        raise HTTPException(502, model_error_detail(exc.response.status_code))
    except httpx.TimeoutException:
        logger.warning("Model request timed out")
        raise HTTPException(504, "The model took too long to answer. Retry this question.")
    except (httpx.RequestError, KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Model response error: %s", type(exc).__name__)
        raise HTTPException(502, "Could not read the model answer. Please try again")
    result = {"answer": result["answer"], "visible_evidence": result.get("visible_evidence", []),
              "inferred_explanation": result.get("inferred_explanation", []),
              "uncertainties": result.get("uncertainties", []),
              "transcript_segment_ids": [s for s in result.get("transcript_segment_ids", []) if s in {r["id"] for r in segments}],
              "slide_references": [valid_slide_ids[slide_id] for slide_id in (result.get("slide_references") or [])
                                   if isinstance(slide_id, str) and slide_id in valid_slide_ids],
              "used_lecture_deck": bool(decks),
              "used_lecture_deck_ids": selected_ids,
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
        db.execute("INSERT INTO messages (id,session_id,snapshot_id,region_json,question,answer_json,created_at_ms,chat_id) VALUES (?,?,?,?,?,?,?,?)",
                   (ident, session_id, shot["id"], body.region.model_dump_json() if body.region else None,
                    body.question, json.dumps(result, ensure_ascii=False), int(time.time()*1000), chat_id))
        row = db.execute("SELECT * FROM messages WHERE id=?", (ident,)).fetchone()
    return message_public(row)
