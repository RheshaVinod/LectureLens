"""Single-process LectureLens hackathon prototype."""
import base64
import io
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

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
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
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
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "overview": {"type": "STRING"},
        "key_concepts": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "concept": {"type": "STRING"}, "explanation": {"type": "STRING"},
            "evidence_timestamps_ms": {"type": "ARRAY", "items": {"type": "INTEGER"}},
        }, "required": ["concept", "explanation", "evidence_timestamps_ms"]}},
        "important_visuals": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "snapshot_id": {"type": "STRING"}, "timestamp_ms": {"type": "INTEGER"},
            "description": {"type": "STRING"},
        }, "required": ["snapshot_id", "timestamp_ms", "description"]}},
        "formulas_and_terms": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "item": {"type": "STRING"}, "explanation": {"type": "STRING"},
        }, "required": ["item", "explanation"]}},
        "review_questions": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "question": {"type": "STRING"}, "answer": {"type": "STRING"},
        }, "required": ["question", "answer"]}},
        "unresolved_questions": {"type": "ARRAY", "items": {"type": "STRING"}},
        "next_steps": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["title", "overview", "key_concepts", "important_visuals",
                 "formulas_and_terms", "review_questions", "unresolved_questions", "next_steps"],
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
      snapshot_id TEXT NOT NULL, region_json TEXT, question TEXT NOT NULL,
      answer_json TEXT NOT NULL, created_at_ms INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS recaps (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
      language TEXT NOT NULL, recap_json TEXT NOT NULL, created_at_ms INTEGER NOT NULL);
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
          answer_json TEXT NOT NULL, created_at_ms INTEGER NOT NULL);
        INSERT INTO messages SELECT * FROM messages_with_required_snapshot;
        DROP TABLE messages_with_required_snapshot;
        """)
    session_columns = {row["name"] for row in db.execute("PRAGMA table_info(sessions)").fetchall()}
    if "join_token" not in session_columns:
        db.execute("ALTER TABLE sessions ADD COLUMN join_token TEXT")
    if "join_expires_epoch_ms" not in session_columns:
        db.execute("ALTER TABLE sessions ADD COLUMN join_expires_epoch_ms INTEGER")
    now_ms = int(time.time() * 1000)
    for row in db.execute("SELECT id FROM sessions WHERE join_token IS NULL").fetchall():
        db.execute("UPDATE sessions SET join_token=?, join_expires_epoch_ms=? WHERE id=?",
                   (secrets.token_urlsafe(18), now_ms + PHONE_JOIN_LIFETIME_MS, row["id"]))


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


class RecapRequest(BaseModel):
    language: str = Field(default="en", pattern="^(en|es|fr|zh|hi|ko|ja|de|pt|ar)$")


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
        shot = None
        if body.snapshot_id:
            shot = db.execute("SELECT * FROM snapshots WHERE id=? AND session_id=?", (body.snapshot_id, session_id)).fetchone()
            if not shot:
                raise HTTPException(404, "Snapshot not found in this lecture")
        else:
            shot = db.execute("SELECT * FROM snapshots WHERE session_id=? ORDER BY captured_at_ms DESC LIMIT 1", (session_id,)).fetchone()
        recent_shots = (db.execute("SELECT * FROM snapshots WHERE session_id=? AND captured_at_ms<=? AND id!=? ORDER BY captured_at_ms DESC LIMIT 2",
                                   (session_id, shot["captured_at_ms"], shot["id"])).fetchall()
                        if shot else [])
        previous = db.execute("SELECT question, answer_json FROM messages WHERE session_id=? ORDER BY created_at_ms DESC LIMIT 8", (session_id,)).fetchall()
        latest_segment = db.execute("SELECT end_ms FROM segments WHERE session_id=? ORDER BY end_ms DESC LIMIT 1",
                                    (session_id,)).fetchone()
        if shot:
            anchor_ms = body.asked_at_ms if shot["source"] == "slide" and body.asked_at_ms is not None else shot["captured_at_ms"]
        else:
            anchor_ms = body.asked_at_ms if body.asked_at_ms is not None else (latest_segment["end_ms"] if latest_segment else 0)
        segments = nearby_segments(db, session_id, anchor_ms)
        if not shot and not segments and latest_segment:
            anchor_ms = latest_segment["end_ms"]
            segments = nearby_segments(db, session_id, anchor_ms)
    if not shot and not segments:
        raise HTTPException(409, "No transcript or visual context is available yet")
    if body.region and not shot:
        raise HTTPException(422, "A selected region requires a snapshot")
    if body.region and (body.region.x + body.region.width > 1.000001 or body.region.y + body.region.height > 1.000001):
        raise HTTPException(422, "Region extends outside image")
    image_bytes = Path(shot["path"]).read_bytes() if shot else None
    parts = [{"text": "You are LectureLens, a classroom learning companion. Answer the student's question in the requested language. Use a supplied board image as visual evidence when one is present; otherwise answer solely from the transcript and leave visible_evidence empty. Preserve mathematical symbols exactly; if illegible or missing, say so. Distinguish what is visible from what is inferred or stated by a transcript. Never treat nearby timestamps as proof of a visual reference. Do not invent details. Return concise JSON with answer, visible_evidence (array of short strings), inferred_explanation (array of short strings), uncertainties (array of short strings), transcript_segment_ids (array of IDs actually used), and status (supported, partial, or unsupported).\n" +
             json.dumps({"question": body.question, "language": body.language,
                         "region_label": "R1" if body.region else None,
                         "region_bbox_normalized": body.region.model_dump() if body.region else None,
                         "context_mode": "visual_and_transcript" if shot else "transcript_only",
                         "snapshot_at_ms": shot["captured_at_ms"] if shot else None, "context_anchor_ms": anchor_ms,
                         "transcript": [dict(s) for s in segments],
                         "recent_conversation": [{"question": r["question"], "answer": json.loads(r["answer_json"]).get("answer", "")} for r in reversed(previous)]}, ensure_ascii=False)}]
    if image_bytes:
        parts.append(image_part(image_bytes))
    for older in reversed(recent_shots):
        parts.append({"text": f"Earlier snapshot at {older['captured_at_ms']} ms. Use for context only; describe changes cautiously:"})
        parts.append(image_part(Path(older["path"]).read_bytes()))
    if body.region and image_bytes:
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
    result = {"answer": result["answer"], "visible_evidence": result.get("visible_evidence", []) if shot else [],
              "inferred_explanation": result.get("inferred_explanation", []),
              "uncertainties": result.get("uncertainties", []),
              "transcript_segment_ids": used_segment_ids,
              "source_timestamps": source_timestamps,
              "snapshot_at_ms": shot["captured_at_ms"] if shot else None,
              "status": result.get("status", "partial") if result.get("status") in ("supported", "partial", "unsupported") else "partial"}
    with conn() as db:
        ident = new_id("msg")
        db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?)",
                   (ident, session_id, shot["id"] if shot else None, body.region.model_dump_json() if body.region else None,
                    body.question, json.dumps(result, ensure_ascii=False), int(time.time()*1000)))
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
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise HTTPException(503, "Set GEMINI_API_KEY in server secrets to generate a recap")
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
    parts = [{"text": "Create a grounded study recap in the requested language from this lecture context. "
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
        parts.extend([{"text": label + ":"}, image_part(Path(shot["path"]).read_bytes())])
    payload = {"contents": [{"role": "user", "parts": parts}],
               "generationConfig": {"responseMimeType": "application/json",
                                    "responseSchema": RECAP_RESPONSE_SCHEMA}}
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
                headers={"x-goog-api-key": key}, json=payload)
        response.raise_for_status()
        content = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        recap = json.loads(content)
        if not isinstance(recap.get("title"), str) or not isinstance(recap.get("overview"), str):
            raise ValueError("Invalid recap")
    except (httpx.HTTPError, KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(502, f"Recap generation failed: {type(exc).__name__}. Check server logs and model access")

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
