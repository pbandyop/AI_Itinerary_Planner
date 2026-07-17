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

export interface SpeakHandlers {
  onStart?: () => void;
  onEnd?: () => void;
}

/**
 * Voice capture: MediaRecorder → server STT (Gemini/Whisper) by default.
 * Falls back to browser Web Speech API if recording/STT is unavailable.
 * Exposes ``audioLevel`` (0–1) from a live AnalyserNode while the mic is open.
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
  const [audioLevel, setAudioLevel] = useState(0);

  const onFinalRef = useRef(onFinal);
  onFinalRef.current = onFinal;

  const mediaStreamRef = useRef<MediaStream | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const wantListenRef = useRef(false);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const analyserRafRef = useRef(0);
  const levelSmoothRef = useRef(0);

  const stopAnalyser = useCallback(() => {
    if (analyserRafRef.current) {
      cancelAnimationFrame(analyserRafRef.current);
      analyserRafRef.current = 0;
    }
    const ctx = audioCtxRef.current;
    audioCtxRef.current = null;
    if (ctx) {
      void ctx.close().catch(() => undefined);
    }
    levelSmoothRef.current = 0;
    setAudioLevel(0);
  }, []);

  const startAnalyser = useCallback(
    (stream: MediaStream) => {
      stopAnalyser();
      try {
        const AC =
          window.AudioContext ||
          (window as unknown as { webkitAudioContext?: typeof AudioContext })
            .webkitAudioContext;
        if (!AC) return;
        const ctx = new AC();
        audioCtxRef.current = ctx;
        const source = ctx.createMediaStreamSource(stream);
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 512;
        analyser.smoothingTimeConstant = 0.75;
        source.connect(analyser);
        const data = new Uint8Array(analyser.fftSize);

        const tick = () => {
          analyser.getByteTimeDomainData(data);
          let sum = 0;
          for (let i = 0; i < data.length; i++) {
            const v = (data[i]! - 128) / 128;
            sum += v * v;
          }
          const rms = Math.sqrt(sum / data.length);
          // Gate ambient noise; boost speech peaks into a visible 0–1 range.
          const gated = Math.max(0, rms - 0.02);
          const boosted = Math.min(1, gated * 6.5);
          levelSmoothRef.current =
            levelSmoothRef.current * 0.55 + boosted * 0.45;
          setAudioLevel(levelSmoothRef.current);
          analyserRafRef.current = requestAnimationFrame(tick);
        };
        if (ctx.state === "suspended") {
          void ctx.resume();
        }
        tick();
      } catch {
        /* analyser is decorative — never block STT */
      }
    },
    [stopAnalyser]
  );

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
    stopAnalyser();
    mediaStreamRef.current?.getTracks().forEach((t) => t.stop());
    mediaStreamRef.current = null;
    mediaRecorderRef.current = null;
  }, [stopAnalyser]);

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

  const startBrowserSpeech = useCallback(async () => {
    const Ctor = getSpeechRecognitionCtor();
    if (!Ctor) {
      setError(
        "Speech recognition is not supported in this browser. Use Chrome or Edge with microphone access — voice input is required."
      );
      return;
    }
    // Optional analyser stream so the orb can vibrate in browser-STT mode too.
    try {
      if (navigator.mediaDevices?.getUserMedia) {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true },
        });
        mediaStreamRef.current = stream;
        startAnalyser(stream);
      }
    } catch {
      /* recognition can still work without visual levels */
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
      stopMediaTracks();
    };
    recognition.onend = () => {
      setListening(false);
      setInterim("");
      wantListenRef.current = false;
      stopMediaTracks();
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
      stopMediaTracks();
    }
  }, [
    applyTranscript,
    finalTranscript,
    lang,
    startAnalyser,
    stopMediaTracks,
  ]);

  const startServerRecording = useCallback(async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
        },
      });
      mediaStreamRef.current = stream;
      startAnalyser(stream);
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
      setInterim("Listening… tap the orb when you’re done");
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
        void startBrowserSpeech();
        return;
      }
      wantListenRef.current = false;
      setListening(false);
    }
  }, [startAnalyser, startBrowserSpeech, stopMediaTracks, transcribeBlob]);

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
      void startBrowserSpeech();
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
    audioLevel,
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
export function speakText(
  text: string,
  enabled: boolean,
  handlers?: SpeakHandlers
): void {
  if (!enabled || typeof window === "undefined" || !window.speechSynthesis) {
    handlers?.onEnd?.();
    return;
  }
  // Never read raw URLs or markdown emphasis aloud.
  const clean = text
    .replace(/\*\*/g, "")
    .replace(/https?:\/\/\S+/gi, "")
    .replace(/\s*\(Source:\s*[^)]*\)/gi, "")
    .replace(/\s{2,}/g, " ")
    .trim();
  if (!clean) {
    handlers?.onEnd?.();
    return;
  }
  window.speechSynthesis.cancel();
  const utter = new SpeechSynthesisUtterance(clean.slice(0, 600));
  utter.lang = "en-IN";
  utter.rate = 1.02;
  utter.onstart = () => handlers?.onStart?.();
  utter.onend = () => handlers?.onEnd?.();
  utter.onerror = () => handlers?.onEnd?.();
  window.speechSynthesis.speak(utter);
}

export function stopSpeaking(): void {
  if (typeof window !== "undefined") {
    window.speechSynthesis?.cancel();
  }
}
