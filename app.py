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
    """)


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


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.post("/api/sessions")
def create_session():
    sid = new_id("sess")
    with conn() as db:
        db.execute("INSERT INTO sessions VALUES (?,?)", (sid, int(time.time() * 1000)))
    return {"session_id": sid}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    with conn() as db:
        session(db, session_id)
        rows = db.execute("SELECT * FROM snapshots WHERE session_id=? ORDER BY captured_at_ms DESC LIMIT 12", (session_id,)).fetchall()
        messages = db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY created_at_ms DESC LIMIT 30", (session_id,)).fetchall()
    return {"session_id": session_id, "snapshots": [snapshot_public(r) for r in rows],
            "messages": [message_public(r) for r in reversed(messages)]}


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
    return {"message_id": row["id"], "snapshot_id": row["snapshot_id"],
            "region": json.loads(row["region_json"]) if row["region_json"] else None,
            "question": row["question"], "result": json.loads(row["answer_json"]),
            "created_at_ms": row["created_at_ms"]}


def image_part(data):
    return {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(data).decode()}}


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
        previous = db.execute("SELECT question, answer_json FROM messages WHERE session_id=? ORDER BY created_at_ms DESC LIMIT 4", (session_id,)).fetchall()
        segments = db.execute("SELECT * FROM segments WHERE session_id=? AND start_ms<=? AND end_ms>=? ORDER BY start_ms LIMIT 8",
                              (session_id, shot["captured_at_ms"] + 15000, max(0, shot["captured_at_ms"] - 30000))).fetchall()
    if body.region and (body.region.x + body.region.width > 1.000001 or body.region.y + body.region.height > 1.000001):
        raise HTTPException(422, "Region extends outside image")
    image_bytes = Path(shot["path"]).read_bytes()
    parts = [{"text": "You are LectureLens, a classroom visual companion. Answer the student's question in the requested language. Use the supplied board image as visual evidence. Preserve mathematical symbols exactly; if illegible, say so. Distinguish what is visible from what is inferred or stated by a transcript. Never treat nearby timestamps as proof of a visual reference. Do not invent details. Return concise JSON with answer, visible_evidence (array of short strings), inferred_explanation (array of short strings), uncertainties (array of short strings), transcript_segment_ids (array of IDs actually used), and status (supported, partial, or unsupported).\n" +
             json.dumps({"question": body.question, "language": body.language,
                         "region_label": "R1" if body.region else None,
                         "region_bbox_normalized": body.region.model_dump() if body.region else None,
                         "snapshot_at_ms": shot["captured_at_ms"],
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
               "generationConfig": {"responseMimeType": "application/json"}}
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
    result = {"answer": result["answer"], "visible_evidence": result.get("visible_evidence", []),
              "inferred_explanation": result.get("inferred_explanation", []),
              "uncertainties": result.get("uncertainties", []),
              "transcript_segment_ids": [s for s in result.get("transcript_segment_ids", []) if s in {r["id"] for r in segments}],
              "status": result.get("status", "partial") if result.get("status") in ("supported", "partial", "unsupported") else "partial"}
    with conn() as db:
        ident = new_id("msg")
        db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?)",
                   (ident, session_id, shot["id"], body.region.model_dump_json() if body.region else None,
                    body.question, json.dumps(result, ensure_ascii=False), int(time.time()*1000)))
        row = db.execute("SELECT * FROM messages WHERE id=?", (ident,)).fetchone()
    return message_public(row)
