# LectureLens - Devpost Submission

## Submission details

- **Tagline:** Understand what is said, what is shown, and what you missed - in your language.
- **Prototype:** https://labored-bauble-steadier.ngrok-free.dev
- **GitHub:** https://github.com/RheshaVinod/LectureLens
- **Demo video:** [Add the final public or unlisted video URL]
- **Pitch deck:** `deliverables/LectureLens-Hackathon-Pitch.pdf`
- **Project image:** `deliverables/LectureLens-Product-Screenshot.jpg`
- **Team members:** [Add every teammate's Devpost account]

> Submission deadline: Sunday, November 15 at 11:00 AM PT.

## Short description

LectureLens is a multilingual lecture companion that synchronizes live speech, translations, board images, uploaded slides, and student questions on one session timeline. Students can recover a missed explanation, inspect the visual shown at that moment, and ask grounded follow-up questions in their preferred language.

## Inspiration and problem

Traditional captions preserve a professor's words but often lose the visual context that makes those words meaningful. When an instructor points to an equation and says, "this term goes to zero," a transcript records the sentence but not which term they meant. Students who are deaf or hard of hearing, learning in a second language, or processing speech and visuals at different speeds must mentally reconnect the audio, translation, board work, and timing while the lecture continues.

LectureLens was built to preserve that missing context. It is designed as a companion to classroom accessibility services, not a replacement for them.

## What it does

One button begins synchronized audio and visual capture. LectureLens produces live captions, optionally translates them into one of ten languages, and stores final transcript segments with session-relative timestamps. The laptop camera captures the board periodically and when a caption finalizes. A student can also scan a QR code and use a phone's rear camera to add a clearer board photo to the same lecture session.

Every transcript segment, translation, camera snapshot, uploaded slide, question, answer, and evidence timestamp becomes part of a shared lecture context. Students can:

- Follow live captions in their preferred language.
- Return to the transcript and board image associated with a missed moment.
- Upload PDF, PowerPoint, or image-based lecture material.
- Compare multiple captured frames or select a specific image region.
- Ask follow-up questions even when only audio context is available.
- Receive answers in the selected translation language with supporting timestamps.
- Generate a structured study recap from the complete lecture session.

## How we built it

The frontend is built with HTML, CSS, and JavaScript. It uses the browser Web Speech API for low-latency live recognition and an in-browser quantized Whisper Tiny model through Hugging Face Transformers as a fallback. MyMemory provides demo-volume caption translation.

The backend uses Python, FastAPI, and SQLite. Transcript segments and images share a server-created session clock, with `startMs` and `endMs` values used to retrieve the nearest relevant context. Transcript uploads are idempotent, and the audio client includes duplicate prevention and an offline retry queue.

Visual context comes from the laptop camera, tokenized phone-camera uploads, and uploaded PDF or image slides. PyMuPDF renders PDF pages, Pillow handles image processing, and QR codes provide a quick phone-join flow. Gemini receives the selected visuals, nearby transcript segments, uploaded deck text, and recent conversation. Structured response schemas keep Q&A and recap responses predictable, while evidence timestamps remain attached to the result.

## Tech stack

- **Frontend:** HTML, CSS, JavaScript
- **Live speech:** Web Speech API / `SpeechRecognition`
- **Fallback speech recognition:** Whisper Tiny, ONNX, Hugging Face Transformers.js
- **Translation:** MyMemory Translation API
- **Backend:** Python, FastAPI, Uvicorn
- **Storage:** SQLite and local image storage
- **Multimodal reasoning:** Google Gemini API with structured JSON responses
- **Visual and document processing:** Pillow and PyMuPDF
- **Phone capture:** QR code, tokenized HTTPS join link, rear-camera web capture
- **Deployment/demo:** HTTPS ngrok tunnel

## Challenges we faced

### Keeping audio and visuals on one timeline

Audio recognition, camera capture, phone uploads, and slide uploads arrive through different paths. We created a server-owned lecture session and expressed every event in milliseconds relative to that session, allowing questions to retrieve the closest transcript segments around a selected visual moment.

### Making browser speech recognition resilient

Browser speech recognition depends on an online service and may stop after silence. We added automatic restart behavior, a local Whisper fallback, final-chunk flushing, microphone selection, an audio-level indicator, duplicate prevention, and an offline upload queue.

### Grounding answers without overstating certainty

Nearby timestamps are useful evidence, but they do not prove that a spoken phrase refers to a specific visual region. The retrieval pipeline treats them as candidate context, includes source timestamps, and supports transcript-only answers when no image is available.

### Connecting a phone securely during a live demo

Phone cameras require HTTPS and cannot access a laptop's localhost address. We built a QR-based phone companion with a rotating six-hour token so the phone can upload board images into the correct session without exposing the main interface.

### Combining multiple independent prototypes

The audio, visual, Q&A, and final interface began as separate pipelines. We unified them around explicit API contracts and a shared session context rather than coupling features through browser-only global state.

## Accomplishments we are proud of

- A complete capture-to-question workflow rather than a disconnected transcription demo.
- Live captions and answer generation across ten language choices.
- Event-triggered snapshots when captions finalize, in addition to timed capture.
- Audio-only explanations, multimodal region questions, and multi-frame comparison.
- Phone camera capture that joins the same timestamped lecture session.
- A grounded recap that turns live session data into a reusable study artifact.

## What we learned

Multimodal systems depend as much on data contracts and timing as they do on model capability. A strong model cannot recover classroom context if transcript segments, snapshots, and questions do not share stable identifiers and timestamps. We also learned to design fallbacks around browser and network limitations instead of assuming one capture method will always work.

## What's next

Next, we would move session storage from a local SQLite prototype to persistent cloud storage, add authenticated class spaces and instructor controls, and stream updates between clients through a realtime service. We would also evaluate timestamp retrieval over longer lectures, improve accessibility testing with students and campus disability services, add privacy and retention controls, and package the phone companion as an optional Expo/React Native application.

## Three-minute demo script

### 0:00-0:20 - The problem

"Captions tell students what a professor said, but not what they were pointing at. For multilingual and accessibility-focused learners, losing that visual moment can make the next several minutes difficult to follow. LectureLens keeps speech and visuals together."

### 0:20-0:45 - Start the lecture

- Open a fresh LectureLens session.
- Choose the spoken language and a different translation language.
- Click **Start lecture** once to begin synchronized captions and camera capture.
- Point out the single, uncluttered control and live audio indicator.

### 0:45-1:15 - Prove synchronized context

- Show an equation or diagram to the camera.
- Say: "We can ignore this term because it approaches zero."
- Wait for the final caption and show that a caption-linked snapshot appears.
- Briefly point to the transcript and snapshot timestamps.

### 1:15-1:45 - Ask a grounded question

- Select the relevant snapshot or image region.
- Ask: "Why does this term become zero?"
- Show the answer in the selected translation language.
- Point out the supporting transcript excerpt and evidence timestamp.

### 1:45-2:10 - Connect the phone

- Click **Use phone** and scan the QR code.
- Take a clearer board photo with the phone.
- Show that the phone image appears in the same session's recent moments.

### 2:10-2:35 - Show broader lecture context

- Upload a lecture slide or PDF.
- Demonstrate either multi-frame comparison or an audio-only explanation.
- Emphasize that the answer can combine transcript, visual, and uploaded slide context.

### 2:35-2:55 - Generate the study artifact

- Generate the lecture recap.
- Show concepts, timestamps, important visuals, review questions, and unresolved questions.

### 2:55-3:00 - Close

"LectureLens helps every student understand what was said, what was shown, and what they missed - in their language."

## Final submission checklist

- [ ] Add every teammate to the Devpost project.
- [x] Draft the complete project description.
- [x] Verify the working prototype responds over HTTPS.
- [ ] Keep the prototype server and ngrok tunnel running through judging.
- [ ] Make the GitHub repository public and verify it in a signed-out browser.
- [ ] Push the two local commits currently ahead of `origin/main`.
- [ ] Review the repository for secrets, `.env` files, API keys, and generated session data before making it public.
- [ ] Record and trim the demo video to 3:00 or less.
- [ ] Upload the demo video publicly or as an unlisted link and add the URL above.
- [x] Create the short pitch deck in PDF and editable PowerPoint formats.
- [x] Capture a current screenshot of the live project.
- [ ] Upload the screenshot to Devpost.
- [ ] Submit before Sunday, November 15 at 11:00 AM PT.
