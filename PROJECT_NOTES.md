# LectureLens — Project Notes

Shared context log for humans and AI agents working on this repo. Read this first; append updates as decisions are made.

## Overview
- **Project:** LectureLens (HackSC hackathon project)
- **Tagline:** Understand what's said, what's shown, and what you missed—in your language.
- **Repo:** https://github.com/RheshaVinod/LectureLens
- **Local path:** `~/Documents/Hackathon/HackSC/LectureLens`

## Problem
Captions capture a professor's words but miss meaning tied to visuals. E.g. a professor points at an equation and says "this term goes to zero"; the transcript doesn't say *which* term. Accents and language barriers make it harder.

## Solution
A live, multilingual lecture companion that combines classroom audio with a camera view of the whiteboard/slides, links spoken explanations to visual content, and lets students ask private questions in their preferred language.

**Target users:** deaf and hard-of-hearing students, students following lectures in a second language, anyone who struggles to process speech and visuals simultaneously.

**Positioning:** a companion to existing accessibility services (not a replacement). Potential pilot in one USC class.

## Hackathon MVP Features
1. **Live captions with speaker labels** in English + one additional language.
2. **Visual context linking:** highlights the equation, term, or diagram region being discussed.
3. **"I missed that" button:** brings back the recent explanation alongside its board snapshot.
4. **Multilingual Q&A:** students type questions in their preferred language; answers are grounded in the lecture transcript + board images, with links to the relevant moments.
5. **Accessible laptop UI:** adjustable text size, synchronized captions and visuals.

## How AI Is Used
- Speech recognition → lecture transcript.
- Computer vision → reads the board/slides.
- Multimodal model → connects speech and visuals, resolves references like "this equation", answers follow-ups across languages.
- **Must flag uncertainty** when a visual connection is unclear or the lecture lacks enough info.

## Demo Script
1. A teammate writes an equation, points to a term, and says "We can ignore this because it approaches zero."
2. App captions it and highlights the relevant term.
3. A student asks "Why does that term become zero?" in their preferred language.
4. App answers in that language, showing the supporting board snapshot and lecture excerpt.

## Scope
- **In scope:** laptop only, controlled lecture setup, typed questions, English + one additional language.
- **Out of scope / future:** AR glasses, voice questions, ASL translation.

## Current Status
- Repo cloned; it was empty (no commits) as of 2026-11-14.
- Product concept defined (see above). No code yet.

## Decisions
- 2026-11-14: Product concept, MVP features, demo, and scope set as above.

## Open Questions
- Tech stack (frontend, backend, ASR, vision/multimodal model) not yet chosen.
- Which second language to support.
- How to detect pointing / which board region is referenced (gesture tracking vs. model inference from frame + speech).
- Speaker diarization approach.
- Team roles / task split.

## Update Log
- **2026-11-14** — Cloned empty repo. Created this notes file.
- **2026-11-14** — Added the product idea: problem, solution, MVP features, AI approach, demo script, scope.
