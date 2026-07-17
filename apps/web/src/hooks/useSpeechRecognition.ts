"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { agentBaseUrl } from "@/lib/agent";

/** Minimal typings for the Browser Web Speech API (Chrome / Edge fallback). */
interface SpeechRecognitionResultLike {
  readonly isFinal: boolean;
  readonly 0: { transcript: string };
}

interface SpeechRecognitionEventLike extends Event {
  readonly resultIndex: number;
  readonly results: ArrayLike<SpeechRecognitionResultLike>;
}

interface SpeechRecognitionLike extends EventTarget {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  maxAlternatives?: number;
  start: () => void;
  stop: () => void;
  abort: () => void;
  onresult: ((ev: SpeechRecognitionEventLike) => void) | null;
  onerror: ((ev: Event & { error?: string }) => void) | null;
  onend: (() => void) | null;
}

type SpeechRecognitionCtor = new () => SpeechRecognitionLike;

function getSpeechRecognitionCtor(): SpeechRecognitionCtor | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    SpeechRecognition?: SpeechRecognitionCtor;
    webkitSpeechRecognition?: SpeechRecognitionCtor;
  };
  return w.SpeechRecognition || w.webkitSpeechRecognition || null;
}

function pickRecorderMime(): string | undefined {
  if (typeof MediaRecorder === "undefined") return undefined;
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/mp4",
    "audio/ogg",
  ];
  return candidates.find((t) => MediaRecorder.isTypeSupported(t));
}

export interface UseSpeechRecognitionOptions {
  lang?: string;
  /** Called when a final utterance is ready. */
  onFinal?: (transcript: string) => void;
}

/**
 * Voice capture: MediaRecorder → server STT (Gemini/Whisper) by default.
 * Falls back to browser Web Speech API if recording/STT is unavailable.
 */
export function useSpeechRecognition(options: UseSpeechRecognitionOptions = {}) {
  const { lang = "en-US", onFinal } = options;
  const [supported, setSupported] = useState(false);
  const [listening, setListening] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [interim, setInterim] = useState("");
  const [finalTranscript, setFinalTranscript] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<"server" | "browser" | "none">("none");

  const onFinalRef = useRef(onFinal);
  onFinalRef.current = onFinal;

  const mediaStreamRef = useRef<MediaStream | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const wantListenRef = useRef(false);

  useEffect(() => {
    const hasMedia =
      typeof window !== "undefined" &&
      !!navigator.mediaDevices?.getUserMedia &&
      typeof MediaRecorder !== "undefined";
    const hasBrowser = Boolean(getSpeechRecognitionCtor());
    setSupported(hasMedia || hasBrowser);
    setMode(hasMedia ? "server" : hasBrowser ? "browser" : "none");
  }, []);

  const stopMediaTracks = useCallback(() => {
    mediaStreamRef.current?.getTracks().forEach((t) => t.stop());
    mediaStreamRef.current = null;
    mediaRecorderRef.current = null;
  }, []);

  const applyTranscript = useCallback((text: string) => {
    const clean = text.replace(/\s+/g, " ").trim();
    if (!clean) {
      setError("No speech detected. Tap the mic and try speaking again.");
      return;
    }
    setFinalTranscript(clean);
    setInterim("");
    setError(null);
    onFinalRef.current?.(clean);
  }, []);

  const transcribeBlob = useCallback(
    async (blob: Blob) => {
      setTranscribing(true);
      setError(null);
      try {
        const form = new FormData();
        const ext = blob.type.includes("mp4")
          ? "m4a"
          : blob.type.includes("ogg")
            ? "ogg"
            : "webm";
        form.append("audio", blob, `speech.${ext}`);
        const res = await fetch(`${agentBaseUrl()}/stt`, {
          method: "POST",
          body: form,
        });
        if (!res.ok) {
          const detail = await res.text().catch(() => "");
          throw new Error(
            detail.slice(0, 180) || `STT failed (${res.status})`
          );
        }
        const data = (await res.json()) as { transcript?: string };
        applyTranscript(data.transcript || "");
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        setError(
          `Voice transcription failed (${msg}). Tap the mic to try again — speech input is required.`
        );
      } finally {
        setTranscribing(false);
        setListening(false);
        wantListenRef.current = false;
      }
    },
    [applyTranscript]
  );

  const startBrowserSpeech = useCallback(() => {
    const Ctor = getSpeechRecognitionCtor();
    if (!Ctor) {
      setError(
        "Speech recognition is not supported in this browser. Use Chrome or Edge with microphone access — voice input is required."
      );
      return;
    }
    try {
      recognitionRef.current?.abort();
    } catch {
      /* ignore */
    }
    const recognition = new Ctor();
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.lang = lang;
    if (typeof recognition.maxAlternatives === "number") {
      recognition.maxAlternatives = 1;
    }
    recognition.onresult = (event) => {
      let interimBuf = "";
      let finalBuf = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const piece = event.results[i][0]?.transcript ?? "";
        if (event.results[i].isFinal) finalBuf += piece;
        else interimBuf += piece;
      }
      if (finalBuf) applyTranscript(`${finalTranscript} ${finalBuf}`.trim());
      setInterim(interimBuf);
    };
    recognition.onerror = (ev) => {
      const code = ev.error || "unknown";
      if (code === "aborted" || code === "no-speech") return;
      setError(
        code === "network"
          ? "Browser speech network error. Retry the mic (server STT)."
          : `Speech unavailable (${code}). Voice input is required — retry the mic.`
      );
      wantListenRef.current = false;
      setListening(false);
    };
    recognition.onend = () => {
      setListening(false);
      setInterim("");
      wantListenRef.current = false;
    };
    recognitionRef.current = recognition;
    try {
      recognition.start();
      setListening(true);
      setMode("browser");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start microphone");
      wantListenRef.current = false;
      setListening(false);
    }
  }, [applyTranscript, finalTranscript, lang]);

  const startServerRecording = useCallback(async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
        },
      });
      mediaStreamRef.current = stream;
      chunksRef.current = [];
      const mime = pickRecorderMime();
      const recorder = mime
        ? new MediaRecorder(stream, { mimeType: mime })
        : new MediaRecorder(stream);
      mediaRecorderRef.current = recorder;

      recorder.ondataavailable = (ev) => {
        if (ev.data.size > 0) chunksRef.current.push(ev.data);
      };
      recorder.onerror = () => {
        setError("Microphone recording failed. Tap the mic to try again.");
        wantListenRef.current = false;
        setListening(false);
        stopMediaTracks();
      };
      recorder.onstop = () => {
        const type = recorder.mimeType || mime || "audio/webm";
        const blob = new Blob(chunksRef.current, { type });
        stopMediaTracks();
        if (blob.size < 256) {
          setError("Recording too short — hold the mic a bit longer, then stop.");
          setListening(false);
          wantListenRef.current = false;
          return;
        }
        void transcribeBlob(blob);
      };

      recorder.start(250);
      setListening(true);
      setMode("server");
      setInterim("Recording… click mic again when finished speaking");
    } catch (err) {
      const name = err instanceof Error ? err.name : "";
      if (name === "NotAllowedError" || name === "PermissionDeniedError") {
        setError(
          "Microphone permission denied. Allow mic access — voice input is required."
        );
      } else if (name === "NotFoundError") {
        setError("No microphone found. Plug one in — voice input is required.");
      } else {
        // Fall back to browser Web Speech if MediaRecorder path fails.
        startBrowserSpeech();
        return;
      }
      wantListenRef.current = false;
      setListening(false);
    }
  }, [startBrowserSpeech, stopMediaTracks, transcribeBlob]);

  const stop = useCallback(() => {
    wantListenRef.current = false;
    const recorder = mediaRecorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      try {
        recorder.stop();
      } catch {
        stopMediaTracks();
        setListening(false);
      }
      return;
    }
    try {
      recognitionRef.current?.stop();
    } catch {
      /* ignore */
    }
    stopMediaTracks();
    setListening(false);
    setInterim("");
  }, [stopMediaTracks]);

  const start = useCallback(() => {
    setError(null);
    setInterim("");
    wantListenRef.current = true;
    if (
      typeof navigator !== "undefined" &&
      typeof navigator.mediaDevices?.getUserMedia === "function" &&
      typeof MediaRecorder !== "undefined"
    ) {
      void startServerRecording();
    } else {
      startBrowserSpeech();
    }
  }, [startBrowserSpeech, startServerRecording]);

  const resetTranscript = useCallback(() => {
    setFinalTranscript("");
    setInterim("");
  }, []);

  const setTranscript = useCallback((text: string) => {
    // Intentionally does not unlock Send — Capstone requires real STT via onFinal.
    setFinalTranscript(text);
    setInterim("");
  }, []);

  const clearError = useCallback(() => setError(null), []);

  useEffect(() => {
    return () => {
      wantListenRef.current = false;
      try {
        recognitionRef.current?.abort();
      } catch {
        /* ignore */
      }
      stopMediaTracks();
    };
  }, [stopMediaTracks]);

  return {
    supported,
    listening,
    transcribing,
    interim,
    finalTranscript,
    transcript: `${finalTranscript}${interim ? ` ${interim}` : ""}`.trim(),
    error,
    mode,
    start,
    stop,
    resetTranscript,
    setTranscript,
    clearError,
  };
}

/** Optional short TTS for confirmations / explanations. */
export function speakText(text: string, enabled: boolean): void {
  if (!enabled || typeof window === "undefined" || !window.speechSynthesis) return;
  // Never read raw URLs or markdown emphasis aloud.
  const clean = text
    .replace(/\*\*/g, "")
    .replace(/https?:\/\/\S+/gi, "")
    .replace(/\s*\(Source:\s*[^)]*\)/gi, "")
    .replace(/\s{2,}/g, " ")
    .trim();
  if (!clean) return;
  window.speechSynthesis.cancel();
  const utter = new SpeechSynthesisUtterance(clean.slice(0, 600));
  utter.lang = "en-IN";
  utter.rate = 1.02;
  window.speechSynthesis.speak(utter);
}

export function stopSpeaking(): void {
  if (typeof window !== "undefined") {
    window.speechSynthesis?.cancel();
  }
}
