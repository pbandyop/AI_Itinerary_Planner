"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { invokeAgent, type ConversationTurn } from "@/lib/agent";
import {
  appendLiveEvalRow,
  dayPacesFromItinerary,
  inferSourceChannel,
  shouldLogItineraryJson,
  sourcesForEvalLog,
  sourcesToRetrievalContext,
} from "@/lib/evalCsv";
import { normalizeSttMessage } from "@/lib/sttNormalize";
import {
  speakText,
  stopSpeaking,
  useSpeechRecognition,
} from "@/hooks/useSpeechRecognition";
import type { Itinerary, Source, TripConstraints } from "@/types/itinerary";
import type { TravelTimeResult, WeatherResult } from "@/types/mcp";
import type { PipelineLogStep } from "@/lib/agent";
import AssistantReply, { speakableReply } from "./AssistantReply";
import EvalPanel from "./EvalPanel";
import PipelineTrace from "./PipelineTrace";
import ItineraryView from "./ItineraryView";
import SourcesPanel, { collectSources } from "./SourcesPanel";
import VoiceOrb, { type VoiceOrbMode } from "./VoiceOrb";
import styles from "./voice-planner.module.css";

const SAMPLE_PROMPTS = [
  "Plan a trip to Jaipur.",
  "3-day relaxed Jaipur trip — food, temples, and shopping.",
  "Plan 3 days in Jaipur for heritage, museums, and food.",
  "Make Day 2 relaxed.",
  "Swap the Day 1 evening plan to something indoors.",
  "Add a bazaar or shopping stop.",
  "Why did you pick this place?",
  "What if it rains?",
];

const WELCOME_SPEECH =
  "Hi — I’m VocalVoyage. Tap the mic and say “Plan a trip to Jaipur.” Voice input is required — I’ll ask for days, pace, and interests before building anything.";

const CHAT_HISTORY_KEY = "vocalvoyage.chatHistory.v1";

type SavedChat = {
  id: string;
  title: string;
  updatedAt: number;
  sessionId: string;
  conversation: ConversationTurn[];
  itinerary: Itinerary | null;
  travelTimes: TravelTimeResult | null;
  weather: WeatherResult | null;
  pendingTrip: TripConstraints | null;
  sources: Source[];
  reply: string;
};

function truncateCaption(text: string, max = 140): string {
  const clean = text.replace(/\s+/g, " ").trim();
  if (clean.length <= max) return clean;
  return `${clean.slice(0, max - 1).trimEnd()}…`;
}

function loadChatHistory(): SavedChat[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(CHAT_HISTORY_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as SavedChat[];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function persistChatHistory(items: SavedChat[]) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(CHAT_HISTORY_KEY, JSON.stringify(items.slice(0, 40)));
  } catch {
    /* ignore quota */
  }
}

function chatTitleFrom(conversation: ConversationTurn[]): string {
  const firstUser = conversation.find((t) => t.role === "user")?.content?.trim();
  if (firstUser) return truncateCaption(firstUser, 42);
  return "New conversation";
}

function newSessionId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `sess-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
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
  const [aiSpeaking, setAiSpeaking] = useState(false);
  const [aiLevel, setAiLevel] = useState(0);
  const [navNotice, setNavNotice] = useState<string | null>(null);
  const [chatHistory, setChatHistory] = useState<SavedChat[]>([]);
  const [activeChatId, setActiveChatId] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [menuChatId, setMenuChatId] = useState<string | null>(null);
  const [activeView, setActiveView] = useState<"planner" | "evals">("planner");
  const sttTextRef = useRef("");
  const abortRef = useRef<AbortController | null>(null);
  const itineraryRef = useRef<Itinerary | null>(null);
  const pendingTripRef = useRef<TripConstraints | null>(null);
  const conversationRef = useRef<ConversationTurn[]>([]);
  const sessionIdRef = useRef(sessionId);
  const lastAutoSentRef = useRef<string>("");
  const chatEndRef = useRef<HTMLDivElement | null>(null);
  const submitRef = useRef<(message: string) => Promise<void>>(async () => {});
  const aiLevelRafRef = useRef(0);
  const navNoticeTimerRef = useRef<number | null>(null);
  const sidebarMenuRef = useRef<HTMLDivElement | null>(null);
  const travelTimesRef = useRef<TravelTimeResult | null>(null);
  const weatherRef = useRef<WeatherResult | null>(null);
  const sourcesRef = useRef<Source[]>([]);
  const replyRef = useRef("");
  itineraryRef.current = itinerary;
  pendingTripRef.current = pendingTrip;
  conversationRef.current = conversation;
  sessionIdRef.current = sessionId;
  travelTimesRef.current = travelTimes;
  weatherRef.current = weather;
  sourcesRef.current = sources;
  replyRef.current = reply;

  useEffect(() => {
    setChatHistory(loadChatHistory());
  }, []);

  const speech = useSpeechRecognition({
    lang: "en-US",
    onFinal: (text) => {
      const clean = normalizeSttMessage(text.trim());
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

  // Greet on landing. Retry on first gesture if the browser blocks autoplay TTS.
  useEffect(() => {
    if (!tts) return;
    let spoken = false;
    const speakWelcome = () => {
      if (spoken || conversationRef.current.length > 0) return;
      speakText(WELCOME_SPEECH, true, {
        onStart: () => {
          spoken = true;
          setAiSpeaking(true);
        },
        onEnd: () => setAiSpeaking(false),
      });
    };
    speakWelcome();
    const unlock = () => speakWelcome();
    window.addEventListener("pointerdown", unlock);
    window.addEventListener("keydown", unlock);
    return () => {
      window.removeEventListener("pointerdown", unlock);
      window.removeEventListener("keydown", unlock);
      stopSpeaking();
      setAiSpeaking(false);
    };
  }, [tts]);

  useEffect(() => {
    if (!aiSpeaking) {
      setAiLevel(0);
      if (aiLevelRafRef.current) {
        cancelAnimationFrame(aiLevelRafRef.current);
        aiLevelRafRef.current = 0;
      }
      return;
    }
    let t0 = performance.now();
    const tick = (now: number) => {
      const t = (now - t0) / 1000;
      // Soft synthetic envelope — browser TTS has no audio analyser.
      const wave =
        0.35 +
        0.35 * Math.abs(Math.sin(t * 5.2)) +
        0.2 * Math.abs(Math.sin(t * 11.7));
      setAiLevel(Math.min(1, wave));
      aiLevelRafRef.current = requestAnimationFrame(tick);
    };
    aiLevelRafRef.current = requestAnimationFrame(tick);
    return () => {
      if (aiLevelRafRef.current) cancelAnimationFrame(aiLevelRafRef.current);
    };
  }, [aiSpeaking]);

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
    setActiveChatId(null);
    // New Trip gets a fresh Session_Id; eval CSV is never cleared (append-only).
    const nextSessionId = newSessionId();
    sessionIdRef.current = nextSessionId;
    setSessionId(nextSessionId);
    stopSpeaking();
    setAiSpeaking(false);
    if (tts) {
      window.setTimeout(() => {
        speakText(WELCOME_SPEECH, true, {
          onStart: () => setAiSpeaking(true),
          onEnd: () => setAiSpeaking(false),
        });
      }, 120);
    }
  }, [speech, tts]);

  const archiveCurrentChat = useCallback(() => {
    const turns = conversationRef.current;
    if (!turns.length) return;
    const snapshot: SavedChat = {
      id: activeChatId || sessionIdRef.current || newSessionId(),
      title: chatTitleFrom(turns),
      updatedAt: Date.now(),
      sessionId: sessionIdRef.current,
      conversation: turns,
      itinerary: itineraryRef.current,
      travelTimes: travelTimesRef.current,
      weather: weatherRef.current,
      pendingTrip: pendingTripRef.current,
      sources: sourcesRef.current,
      reply: replyRef.current,
    };
    setChatHistory((prev) => {
      const without = prev.filter((c) => c.id !== snapshot.id);
      const next = [snapshot, ...without].slice(0, 40);
      persistChatHistory(next);
      return next;
    });
  }, [activeChatId]);

  const startNewConversation = useCallback(() => {
    archiveCurrentChat();
    setSidebarOpen(true);
    resetSession();
  }, [archiveCurrentChat, resetSession]);

  const openSavedChat = useCallback(
    (chat: SavedChat) => {
      if (conversationRef.current.length && activeChatId !== chat.id) {
        archiveCurrentChat();
      }
      stopSpeaking();
      setAiSpeaking(false);
      setMenuChatId(null);
      setActiveChatId(chat.id);
      const restoredSession = chat.sessionId || chat.id;
      sessionIdRef.current = restoredSession;
      setSessionId(restoredSession);
      setConversation(chat.conversation || []);
      setItinerary(chat.itinerary);
      setTravelTimes(chat.travelTimes);
      setWeather(chat.weather);
      setPendingTrip(chat.pendingTrip);
      setSources(chat.sources || []);
      setReply(chat.reply || "");
      setPipelineLog([]);
      setIntent(null);
      setSafety(null);
      setDraft("");
      setError(null);
      setVoiceUnlocked(false);
      setSpeakHint(null);
      sttTextRef.current = "";
      speech.resetTranscript();
      setSidebarOpen(true);
    },
    [activeChatId, archiveCurrentChat, speech]
  );

  const deleteSavedChat = useCallback(
    (chatId: string) => {
      const target = chatHistory.find((c) => c.id === chatId);
      const label = target?.title || "this conversation";
      if (
        typeof window !== "undefined" &&
        !window.confirm(`Delete “${label}”? This cannot be undone.`)
      ) {
        setMenuChatId(null);
        return;
      }
      setChatHistory((prev) => {
        const next = prev.filter((c) => c.id !== chatId);
        persistChatHistory(next);
        return next;
      });
      setMenuChatId(null);
      const deletingActive =
        activeChatId === chatId ||
        sessionIdRef.current === chatId ||
        (target != null && sessionIdRef.current === target.sessionId);
      if (deletingActive) {
        resetSession();
      }
    },
    [activeChatId, chatHistory, resetSession]
  );

  useEffect(() => {
    if (!menuChatId) return;
    const onPointerDown = (event: MouseEvent | PointerEvent) => {
      const root = sidebarMenuRef.current;
      if (root && !root.contains(event.target as Node)) {
        setMenuChatId(null);
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuChatId(null);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuChatId]);

  const submit = useCallback(
    async (message: string) => {
      const text = normalizeSttMessage(message.trim());
      if (!text || pending) return;
      // Capstone: strictly STT — message must match the latest transcript.
      if (!voiceUnlocked || text !== normalizeSttMessage(sttTextRef.current.trim())) {
        setError(
          "Voice input is required — tap the orb and speak. Typed messages are not accepted."
        );
        return;
      }
      sttTextRef.current = text;

      speech.stop();
      stopSpeaking();
      setAiSpeaking(false);
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
      const timestampUq = new Date().toISOString();

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
        const timestampR = new Date().toISOString();
        const replyText = result.user_reply || "";
        const replySources = sourcesForEvalLog({
          intent: result.intent,
          sources: result.sources,
          itinerarySources: result.merged_itinerary?.sources,
          agentTrace: result.agent_trace as
            | Array<Record<string, unknown>>
            | undefined,
        });
        const knowledgeTurn =
          result.intent === "explain" ||
          (result.agent_trace || []).some((e) =>
            String(e.action || e.tool || "")
              .toLowerCase()
              .includes("knowledge_qa")
          );
        setReply(replyText);
        setIntent(result.intent);
        setSafety(result.safety_status);
        setPipelineLog(result.pipeline_log || []);
        // Tip turns: show turn citations (RAG). Plan turns: allow itinerary refs.
        if (result.intent === "explain") {
          setSources(replySources);
        } else {
          setSources(result.sources || []);
        }
        if (result.trip_constraints) {
          setPendingTrip(result.trip_constraints);
        }
        if (result.merged_itinerary) {
          setItinerary(result.merged_itinerary);
          setPendingTrip(result.merged_itinerary.trip);
          if (
            result.intent !== "explain" &&
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
          { role: "assistant", content: replyText },
        ]);
        const logItinerary = shouldLogItineraryJson(result.intent);
        appendLiveEvalRow({
          sessionId: sessionIdRef.current,
          timestampUq,
          timestampR,
          question: text,
          retrievalContext: sourcesToRetrievalContext(replySources, {
            knowledgeTurn,
          }),
          sourceChannel: inferSourceChannel(
            replySources,
            result.agent_trace as Array<Record<string, unknown>> | undefined
          ),
          actualOutput: replyText,
          itineraryJson:
            logItinerary && result.merged_itinerary
              ? JSON.stringify(result.merged_itinerary)
              : "",
          dayPacesJson:
            logItinerary && result.merged_itinerary
              ? dayPacesFromItinerary(result.merged_itinerary)
              : "",
        });
        speakText(speakableReply(replyText), tts, {
          onStart: () => setAiSpeaking(true),
          onEnd: () => setAiSpeaking(false),
        });
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
    stopSpeaking();
    setAiSpeaking(false);
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

  const userSpeaking = speech.listening && speech.audioLevel > 0.08;

  const orbMode: VoiceOrbMode = pending
    ? "thinking"
    : speech.transcribing
      ? "transcribing"
      : aiSpeaking
        ? "aiSpeaking"
        : userSpeaking
          ? "userSpeaking"
          : speech.listening
            ? "listening"
            : "idle";

  const statusLabel = !speech.supported
    ? "Microphone required"
    : pending
      ? "Planning…"
      : speech.transcribing
        ? "Transcribing…"
        : aiSpeaking
          ? "Speaking…"
          : speech.listening
            ? userSpeaking
              ? "Hearing you…"
              : "Listening…"
            : voiceUnlocked
              ? "Ready to send"
              : awaitingConfirm && slotsReady
                ? "Say yes to confirm"
                : clarifying
                  ? "Clarifying your trip"
                  : "Tap to speak";

  const liveCaption = useMemo(() => {
    if (speech.listening && speech.interim) {
      return truncateCaption(speech.interim);
    }
    if (draft.trim()) return truncateCaption(draft);
    if (speakHint) return `Try saying: “${speakHint}”`;
    if (aiSpeaking && reply) return truncateCaption(speakableReply(reply));
    const lastAssistant = [...conversation]
      .reverse()
      .find((t) => t.role === "assistant");
    if (lastAssistant?.content) {
      return truncateCaption(speakableReply(lastAssistant.content));
    }
    return "";
  }, [
    speech.listening,
    speech.interim,
    draft,
    speakHint,
    aiSpeaking,
    reply,
    conversation,
  ]);

  const showCitiesComingSoon = useCallback(() => {
    setNavNotice("Additional cities are in progress. Come back soon!");
    if (navNoticeTimerRef.current) {
      window.clearTimeout(navNoticeTimerRef.current);
    }
    navNoticeTimerRef.current = window.setTimeout(() => {
      setNavNotice(null);
      navNoticeTimerRef.current = null;
    }, 4500);
  }, []);

  const showEvals = useCallback(() => {
    setActiveView("evals");
    setNavNotice(null);
  }, []);

  useEffect(() => {
    return () => {
      if (navNoticeTimerRef.current) {
        window.clearTimeout(navNoticeTimerRef.current);
      }
    };
  }, []);

  void reply;

  if (activeView === "evals") {
    return (
      <div className={styles.shell}>
        <header className={styles.topNav}>
          <div className={styles.topNavInner}>
            <a className={styles.brandLink} href="/">
              <span className={styles.brandMark} aria-hidden>
                ✈
              </span>
              <span className={styles.brandText}>
                <span className={styles.brandName}>VocalVoyage</span>
                <span className={styles.brandSub}>Jaipur · 2–4 days</span>
              </span>
            </a>
            <ul className={styles.navLinks}>
              <li>
                <button
                  type="button"
                  className={styles.navLink}
                  onClick={() => setActiveView("planner")}
                >
                  Planner
                </button>
              </li>
              <li>
                <button
                  type="button"
                  className={`${styles.navLink} ${styles.navLinkActive}`}
                  onClick={showEvals}
                >
                  Eval
                </button>
              </li>
            </ul>
          </div>
        </header>
        <EvalPanel onBack={() => setActiveView("planner")} />
      </div>
    );
  }

  return (
    <div className={styles.shell}>
      <header className={styles.topNav}>
        <div className={styles.topNavInner}>
          <a className={styles.brandLink} href="/">
            <span className={styles.brandMark} aria-hidden>
              ✈
            </span>
            <span className={styles.brandText}>
              <span className={styles.brandName}>VocalVoyage</span>
              <span className={styles.brandSub}>Jaipur · 2–4 days</span>
            </span>
          </a>
          <ul className={styles.navLinks}>
            <li>
              <button
                type="button"
                className={styles.navLink}
                onClick={showCitiesComingSoon}
              >
                New Trip
              </button>
            </li>
            <li>
              <button
                type="button"
                className={styles.navLink}
                onClick={showEvals}
              >
                Eval
              </button>
            </li>
          </ul>
          <div className={styles.navActions}>
            <span
              className={styles.ctaNavDisabled}
              aria-disabled="true"
              title="Coming soon"
            >
              Log in
            </span>
          </div>
        </div>
        {navNotice ? (
          <p className={styles.navNotice} role="status" aria-live="polite">
            {navNotice}
          </p>
        ) : null}
      </header>

      <div className={styles.bodyRow}>
        {sidebarOpen ? (
          <aside className={styles.chatSidebar} aria-label="Conversation history">
            <div className={styles.sidebarHead}>
              <h2>Conversations</h2>
              <button
                type="button"
                className={styles.sidebarClose}
                onClick={() => setSidebarOpen(false)}
                aria-label="Close conversation list"
              >
                ✕
              </button>
            </div>
            <button
              type="button"
              className={styles.sidebarNew}
              onClick={startNewConversation}
            >
              + New conversation
            </button>
            <ul className={styles.sidebarList}>
              {chatHistory.length === 0 ? (
                <li className={styles.sidebarEmpty}>
                  Past chats appear here when you start a new conversation.
                </li>
              ) : (
                chatHistory.map((chat) => (
                  <li key={chat.id} className={styles.sidebarRow}>
                    <button
                      type="button"
                      className={`${styles.sidebarItem} ${
                        activeChatId === chat.id ? styles.sidebarItemActive : ""
                      }`}
                      onClick={() => openSavedChat(chat)}
                    >
                      <span className={styles.sidebarItemTitle}>{chat.title}</span>
                      <span className={styles.sidebarItemMeta}>
                        {new Date(chat.updatedAt).toLocaleString(undefined, {
                          month: "short",
                          day: "numeric",
                          hour: "numeric",
                          minute: "2-digit",
                        })}
                      </span>
                    </button>
                    <div
                      className={styles.sidebarItemMenuWrap}
                      ref={menuChatId === chat.id ? sidebarMenuRef : undefined}
                    >
                      <button
                        type="button"
                        className={styles.sidebarItemMenuBtn}
                        aria-label={`Conversation options for ${chat.title}`}
                        aria-haspopup="menu"
                        aria-expanded={menuChatId === chat.id}
                        onClick={(e) => {
                          e.stopPropagation();
                          setMenuChatId((id) =>
                            id === chat.id ? null : chat.id
                          );
                        }}
                      >
                        ⋮
                      </button>
                      {menuChatId === chat.id ? (
                        <div
                          className={styles.sidebarItemMenu}
                          role="menu"
                          aria-label="Conversation actions"
                        >
                          <button
                            type="button"
                            role="menuitem"
                            className={styles.sidebarItemMenuDelete}
                            onClick={(e) => {
                              e.stopPropagation();
                              deleteSavedChat(chat.id);
                            }}
                          >
                            Delete
                          </button>
                        </div>
                      ) : null}
                    </div>
                  </li>
                ))
              )}
            </ul>
          </aside>
        ) : null}

      <div className={styles.main}>
        <section className={styles.chatColumn} aria-label="Voice conversation">
          <div className={styles.chatLog} aria-live="polite">
            {conversation.length === 0 && (
              <p className={styles.chatEmpty}>
                {WELCOME_SPEECH}
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
                  {turn.role === "assistant" ? (
                    <AssistantReply text={turn.content} />
                  ) : (
                    turn.content
                  )}
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
              <span className={styles.srOnly} aria-live="polite">
                {statusLabel}
                {liveCaption ? `. ${liveCaption}` : ""}
              </span>
              <VoiceOrb
                mode={orbMode}
                audioLevel={speech.audioLevel}
                aiLevel={aiLevel}
                disabled={speech.transcribing || pending || !speech.supported}
                pressed={speech.listening}
                onClick={toggleMic}
                label={
                  speech.listening ? "Stop listening" : "Start microphone"
                }
              />
            </div>

            <div className={styles.composerMeta}>
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
              {!autoSend && (
                <button
                  type="button"
                  className={styles.primary}
                  disabled={!canSendVoice}
                  onClick={() => void submit(draft)}
                >
                  {pending ? "Working…" : "Send"}
                </button>
              )}
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
                disabled={pending}
                onClick={startNewConversation}
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

          <SourcesPanel
            sources={
              intent === "explain"
                ? sources
                : collectSources(sources, itinerary)
            }
          />
        </section>
      </div>
      </div>
    </div>
  );
}
