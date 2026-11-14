# LectureLens visual prototype

Each student points their camera at the board or lecture screen. The browser captures a snapshot every four seconds. A question uses the selected or latest snapshot; students can draw a box to focus on one region. Answers retain the evidence snapshot for follow-ups. Transcript segments can be posted later by the audio pipeline.

## Run

1. Create a Python environment and install `requirements.txt`.
2. Create a Gemini API key in [Google AI Studio](https://aistudio.google.com/app/apikey). For local development, create a `.env` file next to `app.py` containing `GEMINI_API_KEY=your_key_here`. The app loads it at startup. In Replit, add `GEMINI_API_KEY` as a server-side secret instead. Do not put it in frontend code or commit it.
3. Run `uvicorn app:app --host 0.0.0.0 --port 8000` and open the HTTPS Replit URL (or `http://localhost:8000`).
4. Allow camera access, point at the board, and ask a question. The first snapshot is captured immediately.

The model ID defaults to `gemini-3.6-flash`; override with server-side `GEMINI_MODEL` if needed. Each browser session has its own URL and SQLite records. This prototype has no user authentication; do not share session links publicly. The camera runs only while this page is open and capture is on. Replit's local `data/` storage may not survive redeployment.

## Transcript interface

`POST /api/sessions/{session_id}/transcript` with JSON such as:

```json
{"segment_id":"seg_demo_1","start_ms":12000,"end_ms":17000,"speaker":"lecturer","language":"en","text":"This term approaches zero."}
```

All times are integer milliseconds since session creation. Snapshot IDs are returned by the capture endpoint. Region coordinates are normalized to the image, with top-left origin: `{x,y,width,height}` in `[0,1]`. The question endpoint is `POST /api/sessions/{session_id}/ask`, with `{question,language,snapshot_id,region}`. Transcript segments near the selected snapshot are passed as candidate context, not proof of a visual reference.
