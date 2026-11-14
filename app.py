"""Single-process LectureLens hackathon prototype."""
import base64
import io
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import httpx
import pymupdf as fitz
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
MAX_UPLOAD = 8 * 1024 * 1024
MAX_SLIDE_UPLOAD = 30 * 1024 * 1024
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
      height_px INTEGER NOT NULL, path TEXT NOT NULL, deck_id TEXT, slide_number INTEGER,
      capture_reason TEXT NOT NULL DEFAULT 'timer');
    CREATE TABLE IF NOT EXISTS segments (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
      start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, speaker TEXT NOT NULL,
      language TEXT NOT NULL, text TEXT NOT NULL, translated_text TEXT,
      translated_language TEXT);
    CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
      snapshot_id TEXT NOT NULL, region_json TEXT, question TEXT NOT NULL,
      answer_json TEXT NOT NULL, created_at_ms INTEGER NOT NULL);
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


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.post("/api/sessions")
def create_session():
    sid = new_id("sess")
    started = int(time.time() * 1000)
    with conn() as db:
        db.execute("INSERT INTO sessions VALUES (?,?)", (sid, started))
    return {"session_id": sid, "started_epoch_ms": started}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    with conn() as db:
        current = session(db, session_id)
        rows = db.execute("SELECT * FROM snapshots WHERE session_id=? ORDER BY captured_at_ms DESC, slide_number", (session_id,)).fetchall()
        messages = db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY created_at_ms DESC LIMIT 30", (session_id,)).fetchall()
        segments = db.execute("SELECT * FROM segments WHERE session_id=? ORDER BY start_ms DESC LIMIT 100", (session_id,)).fetchall()
    return {"session_id": session_id, "started_epoch_ms": current["started_epoch_ms"],
            "snapshots": [snapshot_public(r) for r in rows],
            "transcripts": [dict(r) for r in reversed(segments)],
            "messages": [message_public(r) for r in reversed(messages)]}


@app.get("/api/sessions/{session_id}/context")
def get_full_context(session_id: str):
    with conn() as db:
        current = session(db, session_id)
        snapshots = db.execute("SELECT * FROM snapshots WHERE session_id=? ORDER BY captured_at_ms, slide_number", (session_id,)).fetchall()
        segments = db.execute("SELECT * FROM segments WHERE session_id=? ORDER BY start_ms", (session_id,)).fetchall()
        messages = db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY created_at_ms", (session_id,)).fetchall()
    return {"session_id": session_id, "started_epoch_ms": current["started_epoch_ms"],
            "transcripts": [dict(row) for row in segments],
            "snapshots": [snapshot_public(row) for row in snapshots],
            "messages": [message_public(row) for row in messages]}


@app.post("/api/sessions/{session_id}/snapshots")
async def add_snapshot(session_id: str, image: UploadFile = File(...), source: str = Form("camera"),
                       capture_reason: str = Form("timer")):
    if source not in ("camera", "screen"):
        raise HTTPException(422, "source must be camera or screen")
    if capture_reason not in ("start", "timer", "caption", "manual"):
        raise HTTPException(422, "invalid capture reason")
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


class Region(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)


class Question(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    language: str = Field(default="en", pattern="^(en|es|fr|zh|hi|ko|ja|de|pt|ar)$")
    snapshot_id: str | None = None
    region: Region | None = None
    asked_at_ms: int | None = Field(default=None, ge=0)


class Segment(BaseModel):
    segment_id: str | None = None
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    speaker: str = "lecturer"
    language: str = "en"
    text: str = Field(min_length=1)
    translated_text: str | None = None
    translated_language: str | None = None


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
    return {"message_id": row["id"], "snapshot_id": row["snapshot_id"],
            "region": json.loads(row["region_json"]) if row["region_json"] else None,
            "question": row["question"], "result": json.loads(row["answer_json"]),
            "created_at_ms": row["created_at_ms"]}


def image_part(data):
    return {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(data).decode()}}


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


@app.post("/api/sessions/{session_id}/ask")
async def ask(session_id: str, body: Question):
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise HTTPException(503, "Set GEMINI_API_KEY in server secrets to enable answers")
    with conn() as db:
        session(db, session_id)
        if body.snapshot_id:
            shot = db.execute("SELECT * FROM snapshots WHERE id=? AND session_id=?", (body.snapshot_id, session_id)).fetchone()
        else:
            shot = db.execute("SELECT * FROM snapshots WHERE session_id=? ORDER BY captured_at_ms DESC LIMIT 1", (session_id,)).fetchone()
        if not shot:
            raise HTTPException(409, "No snapshots yet. Start camera capture first")
        recent_shots = db.execute("SELECT * FROM snapshots WHERE session_id=? AND captured_at_ms<=? AND id!=? ORDER BY captured_at_ms DESC LIMIT 2",
                                  (session_id, shot["captured_at_ms"], shot["id"])).fetchall()
        previous = db.execute("SELECT question, answer_json FROM messages WHERE session_id=? ORDER BY created_at_ms DESC LIMIT 8", (session_id,)).fetchall()
        anchor_ms = body.asked_at_ms if shot["source"] == "slide" and body.asked_at_ms is not None else shot["captured_at_ms"]
        segments = nearby_segments(db, session_id, anchor_ms)
    if body.region and (body.region.x + body.region.width > 1.000001 or body.region.y + body.region.height > 1.000001):
        raise HTTPException(422, "Region extends outside image")
    image_bytes = Path(shot["path"]).read_bytes()
    parts = [{"text": "You are LectureLens, a classroom visual companion. Answer the student's question in the requested language. Use the supplied board image as visual evidence. Preserve mathematical symbols exactly; if illegible, say so. Distinguish what is visible from what is inferred or stated by a transcript. Never treat nearby timestamps as proof of a visual reference. Do not invent details. Return concise JSON with answer, visible_evidence (array of short strings), inferred_explanation (array of short strings), uncertainties (array of short strings), transcript_segment_ids (array of IDs actually used), and status (supported, partial, or unsupported).\n" +
             json.dumps({"question": body.question, "language": body.language,
                         "region_label": "R1" if body.region else None,
                         "region_bbox_normalized": body.region.model_dump() if body.region else None,
                         "snapshot_at_ms": shot["captured_at_ms"], "context_anchor_ms": anchor_ms,
                         "transcript": [dict(s) for s in segments],
                         "recent_conversation": [{"question": r["question"], "answer": json.loads(r["answer_json"]).get("answer", "")} for r in reversed(previous)]}, ensure_ascii=False)},
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
                                    "responseSchema": ANSWER_RESPONSE_SCHEMA}}
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            res = await client.post(f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
                                    headers={"x-goog-api-key": key}, json=payload)
        res.raise_for_status()
        content = res.json()["candidates"][0]["content"]["parts"][0]["text"]
        result = json.loads(content)
        if not isinstance(result.get("answer"), str):
            raise ValueError("Missing answer")
    except (httpx.HTTPError, KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(502, f"Model request failed: {type(exc).__name__}. Check server logs and model access")
    used_segment_ids = [s for s in result.get("transcript_segment_ids", []) if s in {r["id"] for r in segments}]
    source_timestamps = [{"segment_id": row["id"], "start_ms": row["start_ms"], "end_ms": row["end_ms"]}
                         for row in segments if row["id"] in used_segment_ids]
    result = {"answer": result["answer"], "visible_evidence": result.get("visible_evidence", []),
              "inferred_explanation": result.get("inferred_explanation", []),
              "uncertainties": result.get("uncertainties", []),
              "transcript_segment_ids": used_segment_ids,
              "source_timestamps": source_timestamps,
              "snapshot_at_ms": shot["captured_at_ms"],
              "status": result.get("status", "partial") if result.get("status") in ("supported", "partial", "unsupported") else "partial"}
    with conn() as db:
        ident = new_id("msg")
        db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?)",
                   (ident, session_id, shot["id"], body.region.model_dump_json() if body.region else None,
                    body.question, json.dumps(result, ensure_ascii=False), int(time.time()*1000)))
        row = db.execute("SELECT * FROM messages WHERE id=?", (ident,)).fetchone()
    return message_public(row)
