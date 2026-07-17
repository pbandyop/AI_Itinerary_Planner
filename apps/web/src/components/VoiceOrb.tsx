"use client";

import type { CSSProperties } from "react";

import styles from "./voice-orb.module.css";

export type VoiceOrbMode =
  | "idle"
  | "listening"
  | "userSpeaking"
  | "transcribing"
  | "thinking"
  | "aiSpeaking";

export interface VoiceOrbProps {
  mode: VoiceOrbMode;
  /** Mic volume 0–1 while the user is speaking. */
  audioLevel?: number;
  /** Synthetic 0–1 envelope while the AI is speaking. */
  aiLevel?: number;
  disabled?: boolean;
  pressed?: boolean;
  onClick?: () => void;
  label: string;
}

/**
 * Living voice orb — reacts to user mic level and AI TTS.
 * STT still runs in the parent; this is visual only.
 */
export default function VoiceOrb({
  mode,
  audioLevel = 0,
  aiLevel = 0,
  disabled,
  pressed,
  onClick,
  label,
}: VoiceOrbProps) {
  const live =
    mode === "userSpeaking"
      ? audioLevel
      : mode === "aiSpeaking"
        ? aiLevel
        : mode === "listening"
          ? 0.12
          : mode === "thinking" || mode === "transcribing"
            ? 0.2
            : 0;

  const scale = 1 + live * 0.42;
  const ringOpacity = 0.18 + live * 0.55;
  const ringScale = 1 + live * 0.85;

  const modeClass =
    mode === "idle"
      ? styles.mode_idle
      : mode === "listening"
        ? styles.mode_listening
        : mode === "userSpeaking"
          ? styles.mode_userSpeaking
          : mode === "transcribing"
            ? styles.mode_transcribing
            : mode === "thinking"
              ? styles.mode_thinking
              : styles.mode_aiSpeaking;

  return (
    <button
      type="button"
      className={`${styles.orb} ${modeClass}`}
      onClick={onClick}
      disabled={disabled}
      aria-pressed={pressed}
      aria-label={label}
      style={
        {
          "--orb-scale": String(scale),
          "--ring-opacity": String(ringOpacity),
          "--ring-scale": String(ringScale),
          "--level": String(live),
        } as CSSProperties
      }
    >
      <span className={`${styles.ring} ${styles.ringOuter}`} aria-hidden />
      <span className={`${styles.ring} ${styles.ringMid}`} aria-hidden />
      <span className={`${styles.ring} ${styles.ringInner}`} aria-hidden />
      <span className={styles.core} aria-hidden>
        <span className={styles.coreGlow} />
        <span className={styles.bars}>
          <span className={styles.bar} />
          <span className={styles.bar} />
          <span className={styles.bar} />
          <span className={styles.bar} />
          <span className={styles.bar} />
        </span>
      </span>
    </button>
  );
}
