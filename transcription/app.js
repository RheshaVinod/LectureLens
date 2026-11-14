// LectureLens — live transcription + multilingual captions
// ASR: Web Speech API, with an in-browser Whisper fallback when Chrome's
// remote recognition service is unavailable.
// Translation: MyMemory free translation API (no key needed, rate-limited — swap for
// Deepgram/DeepL/Google Translate later if you need higher volume or better accuracy)

const sourceLangSelect = document.getElementById("sourceLang");
const targetLangSelect = document.getElementById("targetLang");
const startBtn = document.getElementById("startBtn");
const stopBtn = document.getElementById("stopBtn");
const missedBtn = document.getElementById("missedBtn");
const statusEl = document.getElementById("status");
const finalCaptionsEl = document.getElementById("finalCaptions");
const interimCaptionEl = document.getElementById("interimCaption");
const translatedCaptionsEl = document.getElementById("translatedCaptions");
const missedPanel = document.getElementById("missedPanel");
const missedText = document.getElementById("missedText");
const closeMissed = document.getElementById("closeMissed");
const compatibilityNotice = document.getElementById("compatibilityNotice");

const SpeechRecognitionImpl = window.SpeechRecognition || window.webkitSpeechRecognition;
const forceLocalRecognition = new URLSearchParams(window.location.search).get("local") === "1";

// Shared transcript store. Other teammates (vision-linking, Q&A) can read from this
// array — each entry is a self-contained record with a timestamp.
// Shape: { id, text, lang, startedAt (Date), translated }
window.LectureLensTranscript = [];

let recognition = null;
let listening = false; // intentional on/off, used to decide whether to auto-restart
let entryId = 0;
let restartTimer = null;
let recognitionMode = null;
let localWorker = null;
let localStream = null;
let localAudioContext = null;
let localProcessor = null;
let localSilentGain = null;
let localAudioParts = [];
let localAudioSamples = 0;
let localModelReady = false;
let localCaptureStarted = false;

const LOCAL_CHUNK_SECONDS = 6;

const FATAL_RECOGNITION_ERRORS = new Set([
  "not-allowed",
  "service-not-allowed",
  "audio-capture",
  "language-not-supported",
]);

function showNotice(message) {
  compatibilityNotice.textContent = message;
  compatibilityNotice.classList.remove("hidden");
}

function hideNotice() {
  compatibilityNotice.classList.add("hidden");
}

function resetControls(status = "idle") {
  setStatus(status, false);
  startBtn.disabled = false;
  stopBtn.disabled = true;
}

function explainRecognitionError(error) {
  const messages = {
    "not-allowed": "Microphone access was blocked. Allow microphone access for this site in Chrome, then try again.",
    "service-not-allowed": "Speech recognition is disabled by this browser or browser policy. Try the latest Chrome or Edge.",
    "audio-capture": "No working microphone was found. Check your input device and system microphone permissions.",
    "network": "Chrome's speech service is unavailable. Switching to private, in-browser transcription…",
    "language-not-supported": "This browser does not support speech recognition for the selected language.",
    "no-speech": "No speech detected yet. Still listening…",
  };
  return messages[error] || `Speech recognition stopped (${error}). Please try again.`;
}

// SpeechRecognition is unavailable in many embedded/in-app browsers. It also
// behaves more reliably on an HTTP(S) origin than when index.html is opened as a file.
if (!SpeechRecognitionImpl && !navigator.mediaDevices?.getUserMedia) {
  showNotice("Live transcription is not supported in this browser. Open this page on localhost in the latest Chrome or Edge.");
  startBtn.disabled = true;
  setStatus("microphone unavailable", false);
} else if (!SpeechRecognitionImpl || forceLocalRecognition) {
  showNotice("Chrome speech recognition is unavailable, so LectureLens will use its in-browser transcription model.");
} else if (window.location.protocol === "file:") {
  showNotice("For microphone access, serve this folder on localhost and open it in Chrome or Edge (do not open index.html as a file).");
}

function setStatus(text, isListening) {
  statusEl.textContent = text;
  statusEl.classList.toggle("listening", !!isListening);
}

function appendFinalLine(text, timestamp) {
  const row = document.createElement("div");
  row.className = "line";
  const ts = document.createElement("span");
  ts.className = "ts";
  ts.textContent = timestamp.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  row.appendChild(ts);
  row.appendChild(document.createTextNode(text));
  finalCaptionsEl.appendChild(row);
  finalCaptionsEl.scrollTop = finalCaptionsEl.scrollHeight;
}

function appendTranslatedLine(text, timestamp) {
  const row = document.createElement("div");
  row.className = "line";
  const ts = document.createElement("span");
  ts.className = "ts";
  ts.textContent = timestamp.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  row.appendChild(ts);
  row.appendChild(document.createTextNode(text));
  translatedCaptionsEl.appendChild(row);
  translatedCaptionsEl.scrollTop = translatedCaptionsEl.scrollHeight;
}

// MyMemory free endpoint: https://mymemory.translated.net/doc/spec.php
async function translateText(text, sourceLangShort, targetLangShort) {
  if (!targetLangShort || sourceLangShort === targetLangShort) return null;
  const url = `https://api.mymemory.translated.net/get?q=${encodeURIComponent(text)}&langpair=${sourceLangShort}|${targetLangShort}`;
  try {
    const res = await fetch(url);
    const data = await res.json();
    return data?.responseData?.translatedText || null;
  } catch (err) {
    console.error("Translation failed:", err);
    return null;
  }
}

async function commitFinalTranscript(text, lang = sourceLangSelect.value, timestamp = new Date()) {
  const cleanText = text.trim();
  if (!cleanText) return;

  const entry = {
    id: entryId++,
    text: cleanText,
    lang,
    startedAt: timestamp,
    translated: null,
  };
  window.LectureLensTranscript.push(entry);
  appendFinalLine(cleanText, timestamp);
  interimCaptionEl.textContent = "";
  window.dispatchEvent(new CustomEvent("lecturelens:transcript", { detail: entry }));

  const targetShort = targetLangSelect.value;
  if (targetShort) {
    const translated = await translateText(cleanText, shortLang(lang), targetShort);
    if (translated) {
      entry.translated = translated;
      appendTranslatedLine(translated, timestamp);
      window.dispatchEvent(new CustomEvent("lecturelens:translation", { detail: entry }));
    }
  }
}

function shortLang(bcp47) {
  // "en-US" -> "en"
  return bcp47.split("-")[0];
}

function buildRecognition() {
  const rec = new SpeechRecognitionImpl();
  rec.continuous = true;
  rec.interimResults = true;
  rec.lang = sourceLangSelect.value;

  rec.onresult = async (event) => {
    let interim = "";
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const result = event.results[i];
      const text = result[0].transcript.trim();
      if (result.isFinal) {
        await commitFinalTranscript(text);
      } else {
        interim += text + " ";
      }
    }
    if (interim) interimCaptionEl.textContent = interim;
  };

  rec.onerror = (event) => {
    console.error("Speech recognition error:", event.error);
    const message = explainRecognitionError(event.error);

    if (event.error === "no-speech") {
      setStatus("listening…", true);
      return;
    }

    if (event.error === "network") {
      listening = false;
      recognitionMode = null;
      try {
        rec.abort();
      } catch (err) {
        console.debug("Recognition was already stopped:", err);
      }
      void startLocalRecognition();
      return;
    }

    showNotice(message);
    setStatus(`error: ${event.error}`, false);

    if (FATAL_RECOGNITION_ERRORS.has(event.error)) {
      listening = false;
      resetControls(`error: ${event.error}`);
    }
  };

  rec.onend = () => {
    // The browser auto-stops after a pause; restart seamlessly if the user
    // hasn't explicitly pressed Stop.
    if (listening && recognitionMode === "web") {
      clearTimeout(restartTimer);
      restartTimer = setTimeout(() => {
        if (!listening || recognition !== rec) return;
        try {
          rec.start();
          setStatus("listening…", true);
        } catch (err) {
          console.error("Could not restart speech recognition:", err);
          listening = false;
          showNotice("Speech recognition could not restart. Press Start listening to try again.");
          resetControls("stopped");
        }
      }, 250);
    } else if (!listening) {
      setStatus("idle", false);
    }
  };

  return rec;
}

function mergeAudio(parts, totalLength) {
  const merged = new Float32Array(totalLength);
  let offset = 0;
  for (const part of parts) {
    merged.set(part, offset);
    offset += part.length;
  }
  return merged;
}

function downsampleAudio(audio, inputRate, outputRate = 16000) {
  if (inputRate === outputRate) return audio;
  const ratio = inputRate / outputRate;
  const output = new Float32Array(Math.round(audio.length / ratio));
  for (let i = 0; i < output.length; i++) {
    const start = Math.floor(i * ratio);
    const end = Math.min(Math.floor((i + 1) * ratio), audio.length);
    let sum = 0;
    for (let j = start; j < end; j++) sum += audio[j];
    output[i] = sum / Math.max(1, end - start);
  }
  return output;
}

function beginLocalAudioCapture() {
  if (localCaptureStarted || !localStream || !localModelReady) return;
  localCaptureStarted = true;
  localAudioContext = new (window.AudioContext || window.webkitAudioContext)();
  const source = localAudioContext.createMediaStreamSource(localStream);
  localProcessor = localAudioContext.createScriptProcessor(4096, 1, 1);
  localSilentGain = localAudioContext.createGain();
  localSilentGain.gain.value = 0;
  localAudioParts = [];
  localAudioSamples = 0;

  localProcessor.onaudioprocess = (event) => {
    if (!listening || recognitionMode !== "local" || !localWorker) return;
    const part = new Float32Array(event.inputBuffer.getChannelData(0));
    localAudioParts.push(part);
    localAudioSamples += part.length;

    if (localAudioSamples >= localAudioContext.sampleRate * LOCAL_CHUNK_SECONDS) {
      const audio = downsampleAudio(
        mergeAudio(localAudioParts, localAudioSamples),
        localAudioContext.sampleRate,
      );
      localAudioParts = [];
      localAudioSamples = 0;
      localWorker.postMessage(
        { type: "transcribe", audio, lang: shortLang(sourceLangSelect.value) },
        [audio.buffer],
      );
      setStatus("transcribing locally…", true);
    }
  };

  source.connect(localProcessor);
  localProcessor.connect(localSilentGain);
  localSilentGain.connect(localAudioContext.destination);
  setStatus("listening locally…", true);
  showNotice("Using private in-browser transcription. The first caption may take a little longer while the model warms up.");
}

function stopLocalRecognition() {
  if (localProcessor) {
    localProcessor.disconnect();
    localProcessor.onaudioprocess = null;
  }
  if (localSilentGain) localSilentGain.disconnect();
  if (localAudioContext) void localAudioContext.close();
  if (localStream) localStream.getTracks().forEach((track) => track.stop());
  if (localWorker) localWorker.terminate();
  localProcessor = null;
  localSilentGain = null;
  localAudioContext = null;
  localStream = null;
  localWorker = null;
  localAudioParts = [];
  localAudioSamples = 0;
  localModelReady = false;
  localCaptureStarted = false;
}

async function startLocalRecognition() {
  if (!navigator.mediaDevices?.getUserMedia) {
    showNotice("This browser cannot capture microphone audio. Open LectureLens on localhost in Chrome or Edge.");
    listening = false;
    resetControls("microphone unavailable");
    return;
  }

  recognitionMode = "local";
  listening = true;
  startBtn.disabled = true;
  stopBtn.disabled = false;
  missedBtn.disabled = false;
  setStatus("requesting microphone…", false);

  try {
    setStatus("loading local speech model…", false);
    showNotice("Chrome's speech service could not connect. Loading the in-browser transcription fallback…");

    localWorker = new Worker("whisper-worker.js", { type: "module" });
    localWorker.onmessage = (event) => {
      const message = event.data;
      if (message.type === "progress") {
        const percent = Number.isFinite(message.progress) ? ` ${Math.round(message.progress)}%` : "";
        setStatus(`loading speech model${percent}…`, false);
      } else if (message.type === "ready") {
        localModelReady = true;
        beginLocalAudioCapture();
      } else if (message.type === "result") {
        setStatus("listening locally…", true);
        void commitFinalTranscript(message.text, sourceLangSelect.value, new Date(message.startedAt));
      } else if (message.type === "error") {
        console.error("Local transcription error:", message.error);
        showNotice(`Local transcription failed: ${message.error}`);
        listening = false;
        stopLocalRecognition();
        resetControls("local model error");
      }
    };
    localWorker.onerror = (event) => {
      console.error("Could not load local transcription worker:", event.message);
      showNotice("The in-browser speech model could not load. Check your connection once so the model can be downloaded and cached.");
      listening = false;
      stopLocalRecognition();
      resetControls("model unavailable");
    };
    localWorker.postMessage({ type: "load" });

    localStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
    if (!listening || recognitionMode !== "local") {
      localStream.getTracks().forEach((track) => track.stop());
      localStream = null;
      return;
    }
    beginLocalAudioCapture();
  } catch (err) {
    console.error("Microphone access failed:", err);
    listening = false;
    stopLocalRecognition();
    const denied = err?.name === "NotAllowedError";
    showNotice(denied
      ? "Microphone access was blocked. Allow it in Chrome's site settings, then try again."
      : "The microphone could not start. Check your input device and system microphone permissions.");
    resetControls(denied ? "microphone blocked" : "could not start");
  }
}

startBtn.addEventListener("click", () => {
  if (!SpeechRecognitionImpl || forceLocalRecognition) {
    void startLocalRecognition();
    return;
  }
  hideNotice();
  try {
    recognition = buildRecognition();
    recognitionMode = "web";
    listening = true;
    recognition.start();
    setStatus("listening…", true);
    startBtn.disabled = true;
    stopBtn.disabled = false;
    missedBtn.disabled = false;
  } catch (err) {
    console.error("Could not start speech recognition:", err);
    listening = false;
    showNotice("The microphone could not start. Check browser and system microphone permissions, then try again.");
    resetControls("could not start");
  }
});

stopBtn.addEventListener("click", () => {
  listening = false;
  recognitionMode = null;
  clearTimeout(restartTimer);
  if (recognition) {
    try {
      recognition.stop();
    } catch (err) {
      console.debug("Recognition was already stopped:", err);
    }
  }
  stopLocalRecognition();
  setStatus("stopped", false);
  startBtn.disabled = false;
  stopBtn.disabled = true;
});

// If the source language changes mid-session, restart recognition with the new lang.
sourceLangSelect.addEventListener("change", () => {
  if (listening && recognitionMode === "web" && recognition) {
    listening = false;
    recognition.stop();
    setTimeout(() => {
      recognition = buildRecognition();
      listening = true;
      recognition.start();
      setStatus("listening…", true);
    }, 300);
  }
});

// "I missed that" — surface the most recent final transcript entry (and its
// translation, if present). This is the hook point for also showing the matching
// board/slide snapshot once the vision side of the project exists.
missedBtn.addEventListener("click", () => {
  const last = window.LectureLensTranscript[window.LectureLensTranscript.length - 1];
  if (!last) {
    missedText.textContent = "Nothing captured yet.";
  } else {
    const when = last.startedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    missedText.textContent = last.translated
      ? `[${when}] ${last.text}\n\n→ ${last.translated}`
      : `[${when}] ${last.text}`;
  }
  missedPanel.classList.remove("hidden");
});

closeMissed.addEventListener("click", () => {
  missedPanel.classList.add("hidden");
});
