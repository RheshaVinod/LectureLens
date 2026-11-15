(() => {
  const byId = id => document.getElementById(id);
  const ui = {
    source: byId("audioLanguage"),
    target: byId("captionLanguage"),
    microphone: byId("microphone"),
    start: byId("startAudio"),
    stop: byId("stopAudio"),
    status: byId("audioStatus"),
    sync: byId("audioSync"),
    level: byId("levelFill"),
    levelTrack: document.querySelector(".level-track"),
    transcript: byId("liveTranscript"),
    interim: byId("interimTranscript"),
  };

  const SpeechRecognitionImpl = window.SpeechRecognition || window.webkitSpeechRecognition;
  const forceLocal = new URLSearchParams(location.search).get("local") === "1";
  const QUEUE_KEY = "lecturelens:transcript-upload-queue:v1";
  const CHUNK_SECONDS = 6;
  const MIN_FLUSH_SECONDS = 0.35;

  const state = {
    session: null,
    listening: false,
    mode: null,
    recognition: null,
    restartTimer: null,
    stream: null,
    meterContext: null,
    analyser: null,
    meterFrame: null,
    localWorker: null,
    localContext: null,
    localProcessor: null,
    localGain: null,
    localParts: [],
    localSamples: 0,
    localChunkStartMs: 0,
    localReady: false,
    localCaptureStarted: false,
    drainResolve: null,
    sequence: 0,
    lastFinalEndMs: 0,
    recentFingerprints: [],
    syncing: false,
    retryTimer: null,
  };

  window.LectureLensTranscript = window.LectureLensTranscript || [];

  function setStatus(message, kind = "") {
    ui.status.textContent = message;
    ui.status.className = `audio-status${kind ? ` ${kind}` : ""}`;
  }

  function sessionMs() {
    if (!state.session) return 0;
    return Math.max(0, Date.now() - state.session.startedEpochMs);
  }

  function formatElapsed(milliseconds) {
    const seconds = Math.max(0, Math.floor(milliseconds / 1000));
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  }

  function shortLanguage(value) {
    return value.split("-")[0];
  }

  function normalizeText(text) {
    return text.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
  }

  function isRecentDuplicate(text, endMs) {
    const normalized = normalizeText(text);
    const duplicate = state.recentFingerprints.some(item =>
      item.text === normalized && Math.abs(item.endMs - endMs) < 8000
    );
    if (!duplicate) {
      state.recentFingerprints.push({ text: normalized, endMs });
      state.recentFingerprints = state.recentFingerprints.slice(-40);
    }
    return duplicate;
  }

  function readQueue() {
    try {
      const value = JSON.parse(localStorage.getItem(QUEUE_KEY) || "[]");
      return Array.isArray(value) ? value : [];
    } catch {
      return [];
    }
  }

  function writeQueue(queue) {
    localStorage.setItem(QUEUE_KEY, JSON.stringify(queue));
    updateSyncStatus(queue.length);
  }

  function updateSyncStatus(count = readQueue().length) {
    ui.sync.className = `audio-sync ${count ? "pending" : "synced"}`;
    ui.sync.textContent = count ? `${count} segment${count === 1 ? "" : "s"} pending` : "Context synced";
  }

  function enqueueTranscript(entry) {
    if (!state.session) return;
    const key = `${state.session.id}:${entry.id}`;
    const body = {
      segment_id: entry.id,
      start_ms: entry.startMs,
      end_ms: entry.endMs,
      speaker: "lecturer",
      language: shortLanguage(entry.lang),
      text: entry.text,
      translated_text: entry.translated,
      translated_language: entry.translatedLanguage,
    };
    const queue = readQueue();
    const existing = queue.find(item => item.key === key);
    const version = `${Date.now()}-${Math.random()}`;
    if (existing) Object.assign(existing, { body, version, attempts: 0, nextAttemptAt: 0 });
    else queue.push({
      key,
      version,
      path: `/api/sessions/${encodeURIComponent(state.session.id)}/transcript`,
      body,
      attempts: 0,
      nextAttemptAt: 0,
    });
    writeQueue(queue);
    void processQueue();
  }

  async function processQueue() {
    if (state.syncing || !navigator.onLine) return;
    clearTimeout(state.retryTimer);
    state.syncing = true;
    try {
      while (navigator.onLine) {
        const queue = readQueue();
        const item = queue[0];
        if (!item) break;
        const wait = item.nextAttemptAt - Date.now();
        if (wait > 0) {
          state.retryTimer = setTimeout(() => void processQueue(), wait);
          break;
        }
        const sentVersion = item.version;
        try {
          const response = await fetch(item.path, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(item.body),
          });
          if (!response.ok) throw new Error(`Context sync failed (${response.status})`);
          const latest = readQueue();
          const current = latest.find(candidate => candidate.key === item.key);
          if (current?.version === sentVersion) writeQueue(latest.filter(candidate => candidate.key !== item.key));
        } catch (error) {
          const latest = readQueue();
          const current = latest.find(candidate => candidate.key === item.key);
          if (current) {
            current.attempts = (current.attempts || 0) + 1;
            const delay = Math.min(30000, 1000 * (2 ** Math.min(current.attempts, 5)));
            current.nextAttemptAt = Date.now() + delay;
            writeQueue(latest);
            state.retryTimer = setTimeout(() => void processQueue(), delay);
          }
          console.warn("Transcript queued for retry:", error);
          break;
        }
      }
    } finally {
      state.syncing = false;
    }
  }

  function renderTranscript(entry) {
    ui.transcript.querySelector(".transcript-empty")?.remove();
    let line = ui.transcript.querySelector(`[data-segment-id="${CSS.escape(entry.id)}"]`);
    if (!line) {
      line = document.createElement("p");
      line.className = "transcript-line";
      line.dataset.segmentId = entry.id;
      line.innerHTML = `<time></time><span class="original"></span><span class="translation"></span>`;
      ui.transcript.append(line);
    }
    line.querySelector("time").textContent = formatElapsed(entry.startMs);
    line.querySelector(".original").textContent = entry.text;
    const translation = line.querySelector(".translation");
    translation.textContent = entry.translated ? `→ ${entry.translated}` : "";
    ui.transcript.scrollTop = ui.transcript.scrollHeight;
  }

  async function translateText(text, source, target) {
    if (!target || source === target) return null;
    try {
      const url = `https://api.mymemory.translated.net/get?q=${encodeURIComponent(text)}&langpair=${source}|${target}`;
      const response = await fetch(url);
      if (!response.ok) throw new Error(`Translation failed (${response.status})`);
      const data = await response.json();
      return data?.responseData?.translatedText || null;
    } catch (error) {
      console.warn("Caption translation unavailable:", error);
      return null;
    }
  }

  async function commitFinalTranscript(text, timing = {}) {
    if (!state.session) return;
    const cleanText = text.trim();
    if (!cleanText) return;
    const endMs = Math.max(0, Math.round(timing.endMs ?? sessionMs()));
    const estimatedDuration = Math.min(8000, Math.max(700, cleanText.split(/\s+/).length * 380));
    const startMs = Math.max(
      state.lastFinalEndMs,
      Math.round(timing.startMs ?? Math.max(0, endMs - estimatedDuration)),
    );
    if (isRecentDuplicate(cleanText, endMs)) return;
    state.lastFinalEndMs = Math.max(state.lastFinalEndMs, endMs);
    state.sequence += 1;
    sessionStorage.setItem(`lecturelens:sequence:${state.session.id}`, String(state.sequence));
    const entry = {
      id: `seg_${state.session.id}_${state.sequence}`,
      sessionId: state.session.id,
      sequence: state.sequence,
      text: cleanText,
      lang: ui.source.value,
      startedAt: new Date(state.session.startedEpochMs + startMs),
      startMs,
      endMs: Math.max(startMs, endMs),
      finalizedAt: new Date(),
      timingAccuracy: timing.accuracy || "estimated",
      translated: null,
      translatedLanguage: null,
    };
    window.LectureLensTranscript.push(entry);
    renderTranscript(entry);
    ui.interim.textContent = "";
    window.dispatchEvent(new CustomEvent("lecturelens:transcript", { detail: entry }));
    enqueueTranscript(entry);

    const target = ui.target.value;
    const translated = await translateText(cleanText, shortLanguage(entry.lang), target);
    if (translated) {
      entry.translated = translated;
      entry.translatedLanguage = target;
      renderTranscript(entry);
      enqueueTranscript(entry);
      window.dispatchEvent(new CustomEvent("lecturelens:translation", { detail: entry }));
    }
  }

  async function populateMicrophones() {
    if (!navigator.mediaDevices?.enumerateDevices) return;
    const selected = ui.microphone.value;
    const devices = (await navigator.mediaDevices.enumerateDevices()).filter(device => device.kind === "audioinput");
    ui.microphone.replaceChildren(new Option("Default", ""));
    devices.forEach((device, index) => ui.microphone.add(new Option(device.label || `Microphone ${index + 1}`, device.deviceId)));
    if ([...ui.microphone.options].some(option => option.value === selected)) ui.microphone.value = selected;
  }

  function audioConstraints() {
    return {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      ...(ui.microphone.value ? { deviceId: { exact: ui.microphone.value } } : {}),
    };
  }

  function startMeter(stream) {
    state.meterContext = new (window.AudioContext || window.webkitAudioContext)();
    const source = state.meterContext.createMediaStreamSource(stream);
    state.analyser = state.meterContext.createAnalyser();
    state.analyser.fftSize = 256;
    source.connect(state.analyser);
    const samples = new Uint8Array(state.analyser.fftSize);
    const draw = () => {
      if (!state.analyser) return;
      state.analyser.getByteTimeDomainData(samples);
      let sum = 0;
      for (const value of samples) {
        const normalized = (value - 128) / 128;
        sum += normalized * normalized;
      }
      const level = Math.min(100, Math.round(Math.sqrt(sum / samples.length) * 260));
      ui.level.style.width = `${level}%`;
      ui.levelTrack.setAttribute("aria-valuenow", String(level));
      state.meterFrame = requestAnimationFrame(draw);
    };
    draw();
  }

  async function acquireMicrophone() {
    if (state.stream) return state.stream;
    state.stream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraints() });
    startMeter(state.stream);
    await populateMicrophones();
    return state.stream;
  }

  async function releaseMicrophone() {
    cancelAnimationFrame(state.meterFrame);
    state.meterFrame = null;
    state.analyser = null;
    if (state.meterContext) await state.meterContext.close().catch(() => {});
    state.meterContext = null;
    state.stream?.getTracks().forEach(track => track.stop());
    state.stream = null;
    ui.level.style.width = "0";
    ui.levelTrack.setAttribute("aria-valuenow", "0");
  }

  function buildWebRecognition() {
    const recognition = new SpeechRecognitionImpl();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = ui.source.value;
    recognition.onresult = event => {
      let interim = "";
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const result = event.results[index];
        const text = result[0].transcript.trim();
        if (result.isFinal) void commitFinalTranscript(text, { endMs: sessionMs(), accuracy: "estimated" });
        else interim += `${text} `;
      }
      ui.interim.textContent = interim.trim();
    };
    recognition.onerror = event => {
      console.warn("Browser speech recognition error:", event.error);
      if (event.error === "no-speech") return;
      if (event.error === "network" && state.listening) {
        state.mode = "switching";
        try { recognition.abort(); } catch {}
        void startLocalRecognition(true);
        return;
      }
      if (["not-allowed", "service-not-allowed", "audio-capture", "language-not-supported"].includes(event.error)) {
        setStatus(event.error.replaceAll("-", " "), "error");
        void stopAudio();
      }
    };
    recognition.onend = () => {
      if (!state.listening || state.mode !== "web") return;
      clearTimeout(state.restartTimer);
      state.restartTimer = setTimeout(() => {
        if (!state.listening || state.mode !== "web") return;
        try { recognition.start(); } catch (error) { console.warn("Recognition restart failed:", error); }
      }, 300);
    };
    return recognition;
  }

  function mergeAudio(parts, length) {
    const merged = new Float32Array(length);
    let offset = 0;
    parts.forEach(part => { merged.set(part, offset); offset += part.length; });
    return merged;
  }

  function downsample(audio, inputRate, outputRate = 16000) {
    if (inputRate === outputRate) return audio;
    const ratio = inputRate / outputRate;
    const output = new Float32Array(Math.round(audio.length / ratio));
    for (let index = 0; index < output.length; index += 1) {
      const start = Math.floor(index * ratio);
      const end = Math.min(audio.length, Math.floor((index + 1) * ratio));
      let sum = 0;
      for (let source = start; source < end; source += 1) sum += audio[source];
      output[index] = sum / Math.max(1, end - start);
    }
    return output;
  }

  function sendLocalChunk(force = false) {
    if (!state.localWorker || !state.localContext || !state.localSamples) return false;
    const durationSeconds = state.localSamples / state.localContext.sampleRate;
    if (!force && durationSeconds < CHUNK_SECONDS) return false;
    if (force && durationSeconds < MIN_FLUSH_SECONDS) return false;
    const startMs = state.localChunkStartMs;
    const endMs = Math.round(startMs + durationSeconds * 1000);
    const audio = downsample(mergeAudio(state.localParts, state.localSamples), state.localContext.sampleRate);
    state.localParts = [];
    state.localSamples = 0;
    state.localChunkStartMs = endMs;
    state.localWorker.postMessage({
      type: "transcribe",
      audio,
      lang: shortLanguage(ui.source.value),
      startMs,
      endMs,
    }, [audio.buffer]);
    return true;
  }

  function beginLocalCapture() {
    if (state.localCaptureStarted || !state.localWorker || !state.stream) return;
    state.localCaptureStarted = true;
    state.localContext = new (window.AudioContext || window.webkitAudioContext)();
    const source = state.localContext.createMediaStreamSource(state.stream);
    state.localProcessor = state.localContext.createScriptProcessor(4096, 1, 1);
    state.localGain = state.localContext.createGain();
    state.localGain.gain.value = 0;
    state.localParts = [];
    state.localSamples = 0;
    state.localChunkStartMs = sessionMs();
    state.localProcessor.onaudioprocess = event => {
      if (!state.listening || state.mode !== "local") return;
      const part = new Float32Array(event.inputBuffer.getChannelData(0));
      state.localParts.push(part);
      state.localSamples += part.length;
      if (state.localSamples >= state.localContext.sampleRate * CHUNK_SECONDS) sendLocalChunk();
    };
    source.connect(state.localProcessor);
    state.localProcessor.connect(state.localGain);
    state.localGain.connect(state.localContext.destination);
    setStatus("On-device Whisper", "live");
  }

  async function startLocalRecognition(fromFallback = false) {
    state.mode = "local";
    setStatus(fromFallback ? "Loading fallback" : "Loading Whisper");
    await acquireMicrophone();
    if (state.localWorker) return;
    state.localWorker = new Worker("/assets/whisper-worker.js", { type: "module" });
    beginLocalCapture();
    state.localWorker.onmessage = event => {
      const message = event.data;
      if (message.type === "progress") {
        const percent = Number.isFinite(message.progress) ? ` ${Math.round(message.progress)}%` : "";
        setStatus(`Loading${percent}`);
      } else if (message.type === "ready") {
        state.localReady = true;
        beginLocalCapture();
      } else if (message.type === "result") {
        void commitFinalTranscript(message.text, { startMs: message.startMs, endMs: message.endMs, accuracy: "sample-clock" });
      } else if (message.type === "drained") {
        state.drainResolve?.();
        state.drainResolve = null;
      } else if (message.type === "error") {
        console.error("Whisper error:", message.error);
        setStatus("Whisper error", "error");
      }
    };
    state.localWorker.onerror = event => {
      console.error("Whisper worker failed:", event.message);
      setStatus("Model unavailable", "error");
    };
    state.localWorker.postMessage({ type: "load" });
  }

  async function stopLocalRecognition({ flush = true } = {}) {
    if (state.localProcessor) {
      state.localProcessor.onaudioprocess = null;
      state.localProcessor.disconnect();
    }
    if (flush) sendLocalChunk(true);
    if (state.localWorker) {
      await new Promise(resolve => {
        const timeout = setTimeout(resolve, 15000);
        state.drainResolve = () => { clearTimeout(timeout); resolve(); };
        state.localWorker.postMessage({ type: "drain" });
      });
      state.localWorker.terminate();
    }
    state.localGain?.disconnect();
    if (state.localContext) await state.localContext.close().catch(() => {});
    state.localWorker = null;
    state.localContext = null;
    state.localProcessor = null;
    state.localGain = null;
    state.localParts = [];
    state.localSamples = 0;
    state.localReady = false;
    state.localCaptureStarted = false;
  }

  async function startAudio() {
    if (!state.session) return false;
    if (state.listening) return true;
    if (!navigator.mediaDevices?.getUserMedia) {
      setStatus("Microphone unavailable", "error");
      return false;
    }
    ui.start.disabled = true;
    ui.stop.disabled = false;
    ui.microphone.disabled = true;
    setStatus("Requesting mic");
    try {
      await acquireMicrophone();
      state.listening = true;
      if (!SpeechRecognitionImpl || forceLocal || ui.microphone.value) {
        await startLocalRecognition();
      } else {
        state.mode = "web";
        state.recognition = buildWebRecognition();
        state.recognition.start();
        setStatus("Browser captions", "live");
      }
      return true;
    } catch (error) {
      console.error("Could not start microphone:", error);
      setStatus(error.name === "NotAllowedError" ? "Mic blocked" : "Mic unavailable", "error");
      state.listening = false;
      ui.start.disabled = false;
      ui.stop.disabled = true;
      ui.microphone.disabled = false;
      await releaseMicrophone();
      return false;
    }
  }

  async function stopAudio() {
    if (!state.listening && !state.stream) return;
    const previousMode = state.mode;
    state.listening = false;
    state.mode = "stopping";
    clearTimeout(state.restartTimer);
    if (previousMode === "web" && state.recognition) {
      try { state.recognition.stop(); } catch {}
    }
    if (previousMode === "local") await stopLocalRecognition({ flush: true });
    state.recognition = null;
    state.mode = null;
    await releaseMicrophone();
    ui.start.disabled = false;
    ui.stop.disabled = true;
    ui.microphone.disabled = false;
    ui.interim.textContent = "";
    setStatus("Stopped");
    void processQueue();
    return true;
  }

  async function connectSession(session) {
    if (!session?.id || state.session?.id === session.id) return;
    state.session = session;
    state.sequence = Number(sessionStorage.getItem(`lecturelens:sequence:${session.id}`) || 0);
    try {
      const response = await fetch(`/api/sessions/${encodeURIComponent(session.id)}`);
      if (response.ok) {
        const data = await response.json();
        for (const segment of data.transcripts || []) {
          state.sequence = Math.max(state.sequence, Number(segment.id.split("_").at(-1)) || 0);
          const entry = {
            id: segment.id,
            sessionId: session.id,
            text: segment.text,
            lang: segment.language,
            startMs: segment.start_ms,
            endMs: segment.end_ms,
            translated: segment.translated_text,
            translatedLanguage: segment.translated_language,
          };
          if (!window.LectureLensTranscript.some(existing => existing.id === entry.id)) window.LectureLensTranscript.push(entry);
          renderTranscript(entry);
        }
      }
    } catch (error) {
      console.warn("Could not restore transcripts:", error);
    }
    updateSyncStatus();
    void processQueue();
  }

  ui.start.addEventListener("click", () => void startAudio());
  ui.stop.addEventListener("click", () => void stopAudio());
  ui.source.addEventListener("change", () => {
    if (state.mode === "web" && state.recognition) {
      state.recognition.lang = ui.source.value;
      try { state.recognition.stop(); } catch {}
    }
  });
  ui.target.addEventListener("change", () => {
    window.dispatchEvent(new CustomEvent("lecturelens:translation-language", {
      detail: { language: ui.target.value },
    }));
  });
  window.addEventListener("online", () => void processQueue());
  window.addEventListener("offline", () => updateSyncStatus());
  window.addEventListener("lecturelens:session-ready", event => void connectSession(event.detail));
  navigator.mediaDevices?.addEventListener?.("devicechange", () => void populateMicrophones());
  void populateMicrophones();
  updateSyncStatus();
  if (window.LectureLensSession) void connectSession(window.LectureLensSession);
  window.LectureLensAudio = {
    start: startAudio,
    stop: stopAudio,
    isListening: () => state.listening,
  };
})();
