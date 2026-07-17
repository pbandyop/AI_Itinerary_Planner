"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { invokeAgent, type ConversationTurn } from "@/lib/agent";
import {
  speakText,
  stopSpeaking,
  useSpeechRecognition,
} from "@/hooks/useSpeechRecognition";
import type { Itinerary, Source, TripConstraints } from "@/types/itinerary";
import type { TravelTimeResult, WeatherResult } from "@/types/mcp";
import type { PipelineLogStep } from "@/lib/agent";
import PipelineTrace from "./PipelineTrace";
import ItineraryView from "./ItineraryView";
import SourcesPanel, { collectSources } from "./SourcesPanel";
import styles from "./voice-planner.module.css";

const SAMPLE_PROMPTS = [
  "Plan a trip to Jaipur.",
  "3-day relaxed Jaipur trip — food, temples, and shopping.",
  "Plan 3 days in Jaipur for heritage, museums, and food.",
  "Make Day 2 more relaxed.",
  "Swap the Day 1 evening plan to something indoors.",
  "Add a bazaar or shopping stop.",
  "Why did you pick this place?",
  "What if it rains?",
];

function newSessionId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `sess-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

function speakableReply(full: string): string {
  const trimmed = full.trim();
  if (!trimmed) return "";
  if (
    /^From the .+ travel guide/i.test(trimmed) ||
    /^Here's the .+ forecast/i.test(trimmed)
  ) {
    const firstLine =
      trimmed.split("\n").find((l) => l.trim().startsWith("•")) ||
      trimmed.split("\n")[0];
    const plain = firstLine
      .replace(/^•\s*/, "")
      .replace(/\s*\(Source:.*$/i, "")
      .trim();
    if (plain.length <= 280) {
      return `${plain} Details and sources are on screen.`;
    }
    return `${plain.slice(0, 260).trim()}… Details and sources are on screen.`;
  }
  const match = trimmed.match(/^(.+?)\.\s*\n/);
  if (match) return match[1].trim() + ". Details and sources are on screen.";
  const intro = trimmed.split("\n")[0] || trimmed;
  if (intro.length <= 420) return intro;
  return `${intro.slice(0, 400).trim()}… Full details and sources are on screen.`;
}

export default function VoicePlanner() {
  const [draft, setDraft] = useState("");
  const [reply, setReply] = useState("");
  const [intent, setIntent] = useState<string | null>(null);
  const [safety, setSafety] = useState<string | null>(null);
  const [itinerary, setItinerary] = useState<Itinerary | null>(null);
  const [travelTimes, setTravelTimes] = useState<TravelTimeResult | null>(null);
  const [weather, setWeather] = useState<WeatherResult | null>(null);
  const [pendingTrip, setPendingTrip] = useState<TripConstraints | null>(null);
  const [sources, setSources] = useState<Source[]>([]);
  const [pipelineLog, setPipelineLog] = useState<PipelineLogStep[]>([]);
  const [conversation, setConversation] = useState<ConversationTurn[]>([]);
  const [sessionId, setSessionId] = useState(newSessionId);
  const [error, setError] = useState<string | null>(null);
  const [tts, setTts] = useState(true);
  const [autoSend, setAutoSend] = useState(true);
  const [pending, setPending] = useState(false);
  const [samplesOpen, setSamplesOpen] = useState(false);
  /** Capstone: user turns must be exact STT output (no typed bypass). */
  const [voiceUnlocked, setVoiceUnlocked] = useState(false);
  const [speakHint, setSpeakHint] = useState<string | null>(null);
  const sttTextRef = useRef("");
  const abortRef = useRef<AbortController | null>(null);
  const itineraryRef = useRef<Itinerary | null>(null);
  const pendingTripRef = useRef<TripConstraints | null>(null);
  const conversationRef = useRef<ConversationTurn[]>([]);
  const sessionIdRef = useRef(sessionId);
  const lastAutoSentRef = useRef<string>("");
  const chatEndRef = useRef<HTMLDivElement | null>(null);
  const submitRef = useRef<(message: string) => Promise<void>>(async () => {});
  itineraryRef.current = itinerary;
  pendingTripRef.current = pendingTrip;
  conversationRef.current = conversation;
  sessionIdRef.current = sessionId;

  const speech = useSpeechRecognition({
    lang: "en-US",
    onFinal: (text) => {
      const clean = text.trim();
      sttTextRef.current = clean;
      setDraft(clean);
      setVoiceUnlocked(Boolean(clean));
      setSpeakHint(null);
      setError(null);
    },
  });

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [conversation, pending]);

  const resetSession = useCallback(() => {
    setDraft("");
    speech.resetTranscript();
    sttTextRef.current = "";
    setError(null);
    setVoiceUnlocked(false);
    setSpeakHint(null);
    setPipelineLog([]);
    setReply("");
    setIntent(null);
    setSafety(null);
    setItinerary(null);
    setTravelTimes(null);
    setWeather(null);
    setPendingTrip(null);
    setSources([]);
    setConversation([]);
    setSessionId(newSessionId());
  }, [speech]);

  const submit = useCallback(
    async (message: string) => {
      const text = message.trim();
      if (!text || pending) return;
      // Capstone: strictly STT — message must match the latest transcript.
      if (!voiceUnlocked || text !== sttTextRef.current.trim()) {
        setError(
          "Voice input is required — tap the mic and speak. Typed messages are not accepted."
        );
        return;
      }

      speech.stop();
      stopSpeaking();
      setError(null);
      setReply("");
      setIntent(null);
      setSafety(null);
      setPipelineLog([]);
      setSources([]);
      setPending(true);
      lastAutoSentRef.current = text;
      setDraft("");
      speech.resetTranscript();
      sttTextRef.current = "";
      setVoiceUnlocked(false);
      setSpeakHint(null);
      const prior = conversationRef.current;
      setConversation((c) => [...c, { role: "user", content: text }]);

      abortRef.current?.abort();
      const ac = new AbortController();
      abortRef.current = ac;

      try {
        const prev = itineraryRef.current;
        const pendingTripState = pendingTripRef.current;
        const tripSeed =
          pendingTripState && pendingTripState.confirmed !== true
            ? pendingTripState
            : null;
        const result = await invokeAgent(
          {
            user_message: text,
            session_id: sessionIdRef.current,
            conversation: prior,
            previous_itinerary: prev,
            merged_itinerary: prev,
            trip_constraints: tripSeed,
          },
          ac.signal
        );
        setReply(result.user_reply || "");
        setIntent(result.intent);
        setSafety(result.safety_status);
        setPipelineLog(result.pipeline_log || []);
        setSources(result.sources || []);
        if (result.trip_constraints) {
          setPendingTrip(result.trip_constraints);
        }
        if (result.merged_itinerary) {
          setItinerary(result.merged_itinerary);
          setPendingTrip(result.merged_itinerary.trip);
          if (
            !result.sources?.length &&
            result.merged_itinerary.sources?.length
          ) {
            setSources(result.merged_itinerary.sources);
          }
        }
        if (result.travel_time_results) {
          setTravelTimes(result.travel_time_results);
        }
        if (result.weather_results) {
          setWeather(result.weather_results);
        }
        setConversation((c) => [
          ...c,
          { role: "assistant", content: result.user_reply || "" },
        ]);
        speakText(speakableReply(result.user_reply || ""), tts);
      } catch (err) {
        if ((err as Error).name === "AbortError") return;
        setError(
          err instanceof Error
            ? err.message
            : "Could not reach the agent. Is it running on port 8000?"
        );
        setConversation((c) =>
          c.length && c[c.length - 1]?.role === "user" ? c.slice(0, -1) : c
        );
      } finally {
        setPending(false);
      }
    },
    [pending, speech, tts, voiceUnlocked]
  );
  submitRef.current = submit;

  useEffect(() => {
    if (!autoSend || pending || speech.listening || speech.transcribing) return;
    const text = speech.finalTranscript.trim();
    if (!text || text === lastAutoSentRef.current) return;
    if (!voiceUnlocked) return;
    const t = window.setTimeout(() => {
      void submitRef.current(text);
    }, 350);
    return () => window.clearTimeout(t);
  }, [
    autoSend,
    pending,
    speech.listening,
    speech.transcribing,
    speech.finalTranscript,
    voiceUnlocked,
  ]);

  const toggleMic = () => {
    setError(null);
    if (speech.listening) {
      speech.stop();
      return;
    }
    speech.resetTranscript();
    setDraft("");
    sttTextRef.current = "";
    setVoiceUnlocked(false);
    speech.start();
  };

  const canSendVoice = Boolean(
    draft.trim() &&
      voiceUnlocked &&
      draft.trim() === sttTextRef.current.trim() &&
      !pending
  );

  const slotsReady = Boolean(
    pendingTrip?.days_known &&
      pendingTrip.num_days &&
      pendingTrip?.pace_known &&
      pendingTrip.pace &&
      pendingTrip?.interests_known &&
      (pendingTrip.interests?.length ?? 0) > 0
  );

  const awaitingConfirm =
    safety === "needs_clarify" &&
    (intent === "confirm" || Boolean(pendingTrip && !pendingTrip.confirmed));

  const clarifying =
    safety === "needs_clarify" &&
    Boolean(pendingTrip && !pendingTrip.confirmed && !slotsReady);

  void reply;

  return (
    <div className={styles.shell}>
      <header className={styles.topNav}>
        <div className={styles.topNavInner}>
          <a className={styles.brandLink} href="/">
            <span className={styles.brandMark} aria-hidden>
              ✈
            </span>
            VocalVoyage
          </a>
          <ul className={styles.navLinks}>
            <li>
              <button
                type="button"
                className={styles.navLink}
                onClick={resetSession}
              >
                New Trip
              </button>
            </li>
            <li>
              <span className={styles.navLinkActive}>Voice Input</span>
            </li>
          </ul>
          <div className={styles.navActions}>
            <button
              type="button"
              className={styles.ghostNav}
              onClick={resetSession}
            >
              Reset
            </button>
            <button
              type="button"
              className={styles.ctaNav}
              disabled={!canSendVoice}
              onClick={() => void submit(draft)}
            >
              {pending ? "Working…" : "Send"}
            </button>
          </div>
        </div>
      </header>

      <div className={styles.main}>
        <section className={styles.chatColumn} aria-label="Voice conversation">
          <div className={styles.chatHeader}>
            <div>
              <h2>Travel AI</h2>
              <p className={styles.chatSub}>
                {itinerary
                  ? `${itinerary.trip.num_days ?? "?"}‑day ${
                      itinerary.trip.pace === "moderate"
                        ? "balanced"
                        : itinerary.trip.pace ?? ""
                    } plan · ${itinerary.trip.city}`
                  : clarifying
                    ? "Clarifying your trip…"
                    : awaitingConfirm
                      ? "Say “yes” or “confirm” into the mic"
                      : "Jaipur · 2–4 days · STT required"}
              </p>
            </div>
            {clarifying ? (
              <span className={styles.statusPill}>clarifying</span>
            ) : awaitingConfirm ? (
              <span className={styles.statusPill}>confirm</span>
            ) : itinerary ? (
              <span className={styles.statusPill}>editable</span>
            ) : null}
          </div>

          <div className={styles.chatLog} aria-live="polite">
            {conversation.length === 0 && (
              <p className={styles.chatEmpty}>
                Hi — I’m VocalVoyage. Tap the mic and say “Plan a trip to
                Jaipur.” Voice input is required — I’ll ask for days, pace, and
                interests before building anything.
              </p>
            )}
            {conversation.map((turn, i) => (
              <div
                key={`${turn.role}-${i}`}
                className={`${styles.bubbleRow} ${
                  turn.role === "user" ? styles.bubbleRowUser : ""
                }`}
              >
                <div
                  className={`${styles.avatar} ${
                    turn.role === "user" ? styles.avatarUser : styles.avatarAi
                  }`}
                  aria-hidden
                >
                  {turn.role === "user" ? "You" : "AI"}
                </div>
                <div
                  className={
                    turn.role === "user" ? styles.bubbleUser : styles.bubbleAi
                  }
                >
                  {turn.content}
                </div>
              </div>
            ))}
            {pending && (
              <div className={styles.bubbleRow}>
                <div
                  className={`${styles.avatar} ${styles.avatarAi}`}
                  aria-hidden
                >
                  AI
                </div>
                <div className={styles.bubbleAi}>
                  <span className={styles.typing} aria-label="Working">
                    <span className={styles.typingDot} />
                    <span className={styles.typingDot} />
                    <span className={styles.typingDot} />
                  </span>
                </div>
              </div>
            )}
            <div ref={chatEndRef} />
          </div>

          <div className={styles.composer}>
            <div className={styles.composerRow}>
              <textarea
                className={styles.transcript}
                value={
                  speech.listening && speech.interim && !draft
                    ? speech.interim
                    : draft
                }
                readOnly
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    if (canSendVoice) void submit(draft);
                  }
                }}
                placeholder={
                  speech.listening && speech.interim
                    ? speech.interim
                    : speakHint
                      ? `Try saying: “${speakHint}”`
                      : awaitingConfirm && slotsReady
                        ? "Tap the mic and say “yes, confirm”"
                        : voiceUnlocked
                          ? "Speech transcript ready — Send or Auto-send"
                          : "Tap the mic and speak — typed input is disabled"
                }
                rows={2}
                aria-label="Speech transcript (read-only — voice input required)"
              />
              <button
                type="button"
                className={`${styles.mic} ${speech.listening ? styles.micLive : ""}`}
                onClick={toggleMic}
                disabled={speech.transcribing || pending || !speech.supported}
                aria-pressed={speech.listening}
                aria-label={
                  speech.listening ? "Stop listening" : "Start microphone"
                }
              >
                <span className={styles.micPulse} aria-hidden />
                <span className={styles.micIcon} aria-hidden>
                  {speech.listening ? "●" : "🎤"}
                </span>
              </button>
            </div>

            <div className={styles.composerMeta}>
              <p className={styles.hint}>
                {!speech.supported
                  ? "Microphone required — use Chrome/Edge with mic access"
                  : speech.transcribing
                    ? "Transcribing…"
                    : speech.listening
                      ? "Listening… tap mic to finish"
                      : voiceUnlocked
                        ? "STT ready · Send or Auto-send (typing disabled)"
                        : awaitingConfirm && slotsReady
                          ? "Say “yes” or “confirm” — voice only"
                          : "STT required · tap mic to speak"}
              </p>
              <div className={styles.toggles}>
                <label className={styles.ttsToggle}>
                  <input
                    type="checkbox"
                    checked={tts}
                    onChange={(e) => setTts(e.target.checked)}
                  />
                  Read aloud
                </label>
                <label className={styles.ttsToggle}>
                  <input
                    type="checkbox"
                    checked={autoSend}
                    onChange={(e) => setAutoSend(e.target.checked)}
                  />
                  Auto-send
                </label>
              </div>
            </div>

            {(speech.error || error) && (
              <p className={styles.warn}>{speech.error || error}</p>
            )}

            <div className={styles.actions}>
              <button
                type="button"
                className={styles.primary}
                disabled={!canSendVoice}
                onClick={() => void submit(draft)}
              >
                {pending ? "Working…" : "Send"}
              </button>
              {awaitingConfirm && slotsReady && (
                <button
                  type="button"
                  className={styles.ghost}
                  disabled={pending || speech.listening || speech.transcribing}
                  onClick={() => {
                    setSpeakHint("yes, confirm");
                    setError(null);
                    speech.resetTranscript();
                    setDraft("");
                    sttTextRef.current = "";
                    setVoiceUnlocked(false);
                    speech.start();
                  }}
                >
                  Mic: say yes
                </button>
              )}
              <button
                type="button"
                className={styles.ghost}
                disabled={pending || (!draft && !conversation.length)}
                onClick={resetSession}
              >
                New conversation
              </button>
            </div>

            <div className={styles.samplesWrap}>
              <button
                type="button"
                className={styles.samplesToggle}
                onClick={() => setSamplesOpen((v) => !v)}
                aria-expanded={samplesOpen}
              >
                <span>Try saying</span>
                <span className={styles.samplesHint}>
                  {samplesOpen
                    ? "Hide"
                    : `${SAMPLE_PROMPTS.length} prompts · show`}
                </span>
                <span className={styles.samplesChev} aria-hidden>
                  {samplesOpen ? "▾" : "▸"}
                </span>
              </button>
              {samplesOpen ? (
                <div className={styles.samples}>
                  {SAMPLE_PROMPTS.map((p) => (
                    <button
                      key={p}
                      type="button"
                      className={styles.chip}
                      onClick={() => {
                        setSpeakHint(p);
                        setError(null);
                        if (!speech.listening && !speech.transcribing) {
                          speech.resetTranscript();
                          setDraft("");
                          setVoiceUnlocked(false);
                          speech.start();
                        }
                      }}
                    >
                      {p}
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
          </div>
        </section>

        <section className={styles.planColumn} aria-label="Itinerary and sources">
          {itinerary ? (
            <ItineraryView
              itinerary={itinerary}
              travel={travelTimes}
              weather={weather}
              sources={collectSources(sources, itinerary)}
            />
          ) : (
            <div className={styles.planEmpty}>
              <h2>Your itinerary appears here</h2>
              <p>
                Confirm days, pace, and interests by voice — then say “yes”
                to generate a grounded Jaipur plan with travel times and sources.
              </p>
            </div>
          )}

          <PipelineTrace
            steps={pipelineLog}
            pending={pending}
            userMessage={
              conversation.filter((t) => t.role === "user").at(-1)?.content
            }
          />

          <SourcesPanel sources={collectSources(sources, itinerary)} />
        </section>
      </div>
    </div>
  );
}
