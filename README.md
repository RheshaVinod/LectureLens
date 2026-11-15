# LectureLens

<p align="center">
  <img src="docs/assets/lecturelens-brand.png" alt="LectureLens — Helping every student follow every lecture" width="900">
</p>

<p align="center">
  <strong>An accessible, multimodal AI companion that connects what a professor says with what they show.</strong>
</p>

<p align="center">
  <a href="https://canva.link/pteibr2lp44r19v"><strong>View the presentation</strong></a>
  &nbsp;·&nbsp;
  <a href="https://youtu.be/mw0Exo8Qb4E"><strong>Watch the demo</strong></a>
</p>

## Product

![LectureLens landing page](docs/assets/lecturelens-landing-page.png)

## Inspiration

Every student learns and processes information differently. LectureLens helps visually impaired students, hard-of-hearing students, multilingual learners, and anyone who needs more time or context to follow a lecture.

Students should be able to consume lectures in the format that works best for them: through captions, translations, audio, visual descriptions, zoomed-in images, or the ability to pause and revisit moments. By connecting what the professor says with what they show, LectureLens makes lectures more accessible and easier to understand.

## What it does

LectureLens is an AI companion for visually impaired and hard-of-hearing students and for students navigating unfamiliar accents or language barriers. With multilingual support, it works in real time by combining live captions, translations, board images, slides, course materials, and student questions on one synchronized timeline.

Students can:

- Revisit the exact moment they missed
- View the associated board or slide image
- Ask private questions by voice or text
- Receive answers grounded in the lecture
- Select image regions or compare multiple frames
- Listen to answers aloud
- Generate a structured lecture recap

Unlike a general chatbot, LectureLens already has the lecture context. Students do not need to reconstruct the class using scattered screenshots or notes.

## How we built it

Every caption, image, uploaded material, and question receives a timestamp tied to a shared lecture clock. This allows LectureLens to retrieve the most relevant context for each question.

The frontend uses HTML, CSS, and JavaScript. The backend uses Python, FastAPI, and SQLite. Speech recognition uses the browser's Web Speech API with an in-browser Whisper fallback.

OpenAI's multimodal model combines transcript segments, visuals, course materials, and recent conversation to generate grounded answers with evidence timestamps. OpenAI's speech API provides optional natural read-aloud output.

Students can capture the board with a laptop camera or scan a QR code to upload clearer images from a phone.

## Challenges we ran into

Our biggest challenge was synchronizing audio, visuals, phone uploads, slides, and questions from different sources. A shared session ID and lecture clock helped us connect these events reliably.

Browser speech recognition can stop after silence or depend on an internet connection, so we added automatic restarts, duplicate prevention, retry handling, and a Whisper fallback.

We also had to handle uncertainty. A nearby image may not always be the exact visual a professor referenced, so LectureLens treats it as supporting context rather than guaranteed proof.

## Accomplishments that we're proud of

Our prototype can:

- Connect speech and visuals on a shared timeline
- Support captions and answers in ten languages
- Use transcripts, images, and course materials together
- Accept phone-camera uploads during a live lecture
- Answer questions about selected image regions
- Compare multiple lecture frames
- Provide evidence-backed answers
- Generate structured study recaps

Most importantly, LectureLens is designed around different ways of accessing information instead of assuming every student learns the same way.

## What we learned

We learned that a strong AI model is only part of the solution. Accurate timing, organized context, accessible interaction, and clear evidence are just as important.

A shared timeline makes it possible to connect what was said with what was shown. We also learned to design around uncertainty and give students the original sources so they can evaluate an answer themselves.

## What's next for LectureLens

We want to make LectureLens even more immersive and accessible by exploring features such as:

- Meta-style smart glasses that provide real-time captions, translations, and descriptions of what is on the board
- Audio guidance that identifies where the professor is pointing
- Personalized lecture views based on each student's preferred language and format
- Wearable or spatial notifications when important visual information appears
- Real-time collaboration between phones, laptops, and classroom displays

Our goal is to make lectures easier to follow in real time, whether students are reading captions, listening to descriptions, viewing visuals, or using assistive technology.

## Technical overview

LectureLens captures microphone audio and board images on one shared session clock. It creates live captions with optional translation, stores final transcript segments in SQLite, captures laptop-camera snapshots, and accepts phone photos plus PDF/image slide uploads. The interface organizes chats by course; accepts PPT, PDF, Word, image, and text files as optional context; lets students compare up to four frames, zoom into a snapshot, select a region, and ask follow-up questions in ten languages. OpenAI receives chosen visuals, nearby timestamped transcript, selected file content, and recent conversation; answers retain their evidence timestamps.

## Run

1. Create a Python environment and install `requirements.txt`.
2. Create an [OpenAI API key](https://platform.openai.com/api-keys). For local development, add `OPENAI_API_KEY=your_key_here` to `.env` next to `app.py`. Restart the server after saving it. In Replit, add `OPENAI_API_KEY` as a server-side secret instead. Do not put it in frontend code or commit it.
3. Run `uvicorn app:app --host 0.0.0.0 --port 8000` and open the HTTPS Replit URL (or `http://localhost:8000`).
4. Allow microphone and camera access, choose any detected built-in or external webcam from the **Camera** menu, and start the lecture. You can also upload slides or choose **Connect phone** and scan the QR code to take board photos with the phone's rear camera.

## Deploy from GitHub

The repository includes a `render.yaml` blueprint for deploying the FastAPI app from the `main` branch on Render. In Render, create a new Blueprint, connect this GitHub repository, and provide `OPENAI_API_KEY` when prompted. Every later commit to `main` deploys automatically.

The prototype stores sessions, uploads, and snapshots on the web service's local filesystem. That data is ephemeral on Render unless the service is upgraded and configured with persistent storage. Do not share session URLs publicly because the prototype has no user authentication.

To ask by voice in Chrome, tap the microphone in the question box, speak, then tap it again to finish. Review or edit the recognized text and press **Ask**. The current frame is pinned when dictation starts. If live captions are running, they pause during the question and resume afterward so the student's words are not stored as lecturer transcript. Voice dictation uses Chrome speech recognition and needs microphone permission on localhost or HTTPS.

Each answer has a **Listen** button for optional natural read-aloud output. Tap **Cancel** while audio is being prepared or **Stop** during playback. The server sends the saved answer text (without citation labels or math markup) to OpenAI speech using the same server-side `OPENAI_API_KEY`, then returns WAV audio to the browser. Live captions pause during playback and resume afterward. The default voice model is `gpt-4o-mini-tts` with the `marin` voice; set `OPENAI_TTS_MODEL` or `OPENAI_TTS_VOICE` to override. The Speech API model is deprecated and scheduled to shut down in January 2027, so replace this integration for a longer-lived deployment. Playback audio is cached only in the current browser tab.

Use **Add files** for optional answer context: `.ppt`, `.pptx`, `.pdf`, `.doc`, `.docx`, `.png`, `.jpg`, `.jpeg`, `.webp`, `.txt`, and `.md` (20 MB per file). Select files under **File context** and use **Use files** to include or exclude them from a question. The app ranks pages against each question, sends up to ten relevant pages of extracted text and up to four relevant visuals, and links cited pages, board frames, and caption excerpts directly below the answer. PDF pages beyond the first four can be previewed on demand. PPT slides contribute text and their first embedded raster image when present; PowerPoint shapes/charts are not rendered, and Word embedded images are not extracted. Legacy `.ppt` and `.doc` need LibreOffice on the server. **Add slides** separately adds PDF or image pages to the selectable snapshot timeline.

The answer and recap model ID defaults to `gpt-5.4-mini`; override with server-side `OPENAI_MODEL` if needed. Each browser session has its own URL and SQLite records. This prototype has no user authentication; do not share session links publicly. The camera runs only while this page is open and capture is on. Replit's local `data/` storage may not survive redeployment.

## Shared lecture context

`POST /api/sessions/{session_id}/transcript` with JSON such as:

```json
{"segment_id":"seg_demo_1","start_ms":12000,"end_ms":17000,"speaker":"lecturer","language":"en","text":"This term approaches zero."}
```

All times are integer milliseconds since session creation. The audio pipeline automatically posts final segments, retries when offline, and updates translated text using the same segment ID. Changing caption translation updates the answer-language default; the answer-language selector controls each subsequent question. Camera snapshots and uploaded slide pages share the session context. Region coordinates are normalized to the image, with top-left origin: `{x,y,width,height}` in `[0,1]`.

- `POST /api/sessions/{session_id}/transcript` — idempotent transcript segment upload.
- `POST /api/sessions/{session_id}/snapshots` — timestamped camera/screen image.
- `GET /api/sessions/{session_id}/phone-join` — create a six-hour tokenized phone-camera link and QR URL.
- `POST /api/sessions/{session_id}/slides` — PDF or image slide deck upload.
- `GET /api/sessions/{session_id}/context` — full transcript, snapshot/slide, and Q&A history.
- `POST /api/sessions/{session_id}/ask` — audio-only or multimodal Q&A with `{question,language,snapshot_id,snapshot_ids,region,asked_at_ms,chat_id,selected_deck_ids}`.
- `POST /api/sessions/{session_id}/recap` — generate and persist a grounded study recap with `{language}`.
- `GET /api/sessions/{session_id}/recap` — retrieve the latest generated recap.
- `GET /api/sessions/{session_id}/recaps` — list every saved recap for the lecture.
- `GET /api/recaps/{recap_id}/markdown` — download a recap as a Markdown study guide.

For camera captures, questions retrieve transcript around the snapshot timestamp. For uploaded slides, the question time becomes the transcript anchor. If no snapshot exists, questions still work from nearby audio transcript segments alone and the response is explicitly marked as transcript-only evidence. Nearby timestamps are candidate context, not proof that spoken words refer to a particular visual region.

Answers may include clickable `F` (board frame), `A` (audio caption), and `M` (uploaded material) citations. The server validates model-returned labels against the sources actually supplied to that question. An audio citation opens the transcript excerpt and timestamp; raw audio is not stored or played back.

Study recaps combine the session transcript and translations, visual timeline, up to six representative snapshots/slides, and prior student Q&A. The structured result contains key concepts with lecture timestamps, important visuals, formulas and terms, review questions with answers, unresolved questions, and suggested next steps.

The phone camera joins the same session clock and uploads snapshots with `source: "phone"`. A phone cannot open the laptop's `127.0.0.1` URL; use a deployed HTTPS URL or a shared HTTPS development tunnel for cross-device capture. Phone upload links use a rotating token that expires after six hours.
