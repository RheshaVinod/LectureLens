import { pipeline } from "https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.3.3";

const LANGUAGE_NAMES = {
  en: "english",
  es: "spanish",
  fr: "french",
  zh: "chinese",
  hi: "hindi",
  ko: "korean",
  ja: "japanese",
  de: "german",
  pt: "portuguese",
  ar: "arabic",
};

let transcriber = null;
let transcriptionQueue = Promise.resolve();

async function loadModel() {
  if (transcriber) return;
  transcriber = await pipeline(
    "automatic-speech-recognition",
    "onnx-community/whisper-tiny",
    {
      dtype: "q4",
      progress_callback: (progress) => {
        self.postMessage({
          type: "progress",
          progress: progress.progress,
          status: progress.status,
          file: progress.file,
        });
      },
    },
  );
}

self.onmessage = (event) => {
  const message = event.data;
  if (message.type === "load") {
    loadModel()
      .then(() => self.postMessage({ type: "ready" }))
      .catch((error) => self.postMessage({ type: "error", error: error.message || String(error) }));
    return;
  }

  if (message.type === "transcribe") {
    const startedAt = new Date().toISOString();
    transcriptionQueue = transcriptionQueue
      .then(async () => {
        await loadModel();
        const output = await transcriber(message.audio, {
          language: LANGUAGE_NAMES[message.lang] || "english",
          task: "transcribe",
        });
        const text = Array.isArray(output)
          ? output.map((item) => item.text).join(" ").trim()
          : output.text.trim();
        if (text) self.postMessage({ type: "result", text, startedAt });
      })
      .catch((error) => self.postMessage({ type: "error", error: error.message || String(error) }));
  }
};
