# LectureLens multimodal lecture companion

LectureLens captures microphone audio and a board camera on one shared session clock. It creates live captions with optional translation, stores final transcript segments in SQLite, captures a board snapshot every four seconds, and accepts PDF or image slide uploads. Students can hold any snapshot or slide, select a region, and ask follow-up questions in ten languages. Gemini receives the selected visual, nearby timestamped transcript, earlier snapshots, and recent conversation; answers retain their evidence timestamps.

## Run

1. Create a Python environment and install `requirements.txt`.
2. Create a Gemini API key in [Google AI Studio](https://aistudio.google.com/app/apikey). For local development, create a `.env` file next to `app.py` containing `GEMINI_API_KEY=your_key_here`. The app loads it at startup. In Replit, add `GEMINI_API_KEY` as a server-side secret instead. Do not put it in frontend code or commit it.
3. Run `uvicorn app:app --host 0.0.0.0 --port 8000` and open the HTTPS Replit URL (or `http://localhost:8000`).
4. Allow camera and microphone access. Start captions and camera capture, or upload the lecture's PDF/slides, then select a region and ask a question. The first camera snapshot is captured immediately.

The model ID defaults to `gemini-3.6-flash`; override with server-side `GEMINI_MODEL` if needed. Each browser session has its own URL and SQLite records. This prototype has no user authentication; do not share session links publicly. The camera runs only while this page is open and capture is on. Replit's local `data/` storage may not survive redeployment.

## Shared lecture context

`POST /api/sessions/{session_id}/transcript` with JSON such as:

```json
{"segment_id":"seg_demo_1","start_ms":12000,"end_ms":17000,"speaker":"lecturer","language":"en","text":"This term approaches zero."}
```

All times are integer milliseconds since session creation. The audio pipeline automatically posts final segments, retries when offline, and updates translated text using the same segment ID. Camera snapshots and uploaded slide pages share the session context. Region coordinates are normalized to the image, with top-left origin: `{x,y,width,height}` in `[0,1]`.

- `POST /api/sessions/{session_id}/transcript` — idempotent transcript segment upload.
- `POST /api/sessions/{session_id}/snapshots` — timestamped camera/screen image.
- `POST /api/sessions/{session_id}/slides` — PDF or image slide deck upload.
- `GET /api/sessions/{session_id}/context` — full transcript, snapshot/slide, and Q&A history.
- `POST /api/sessions/{session_id}/ask` — `{question,language,snapshot_id,region,asked_at_ms}`.
- `POST /api/sessions/{session_id}/recap` — generate and persist a grounded study recap with `{language}`.
- `GET /api/sessions/{session_id}/recap` — retrieve the latest generated recap.
- `GET /api/sessions/{session_id}/recaps` — list every saved recap for the lecture.
- `GET /api/recaps/{recap_id}/markdown` — download a recap as a Markdown study guide.

For camera captures, questions retrieve transcript around the snapshot timestamp. For uploaded slides, the question time becomes the transcript anchor. If no snapshot exists, questions still work from nearby audio transcript segments alone and the response is explicitly marked as transcript-only evidence. Nearby timestamps are candidate context, not proof that spoken words refer to a particular visual region.

Study recaps combine the session transcript and translations, visual timeline, up to six representative snapshots/slides, and prior student Q&A. The structured result contains key concepts with lecture timestamps, important visuals, formulas and terms, review questions with answers, unresolved questions, and suggested next steps.
