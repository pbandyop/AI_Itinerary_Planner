"use client";

import { FormEvent, useState } from "react";
import type { Itinerary, Source } from "@/types/itinerary";
import styles from "./email-itinerary.module.css";

type Status = "idle" | "sending" | "success" | "error";

export default function EmailItineraryForm({
  itinerary,
  sources,
}: {
  itinerary: Itinerary;
  sources?: Source[];
}) {
  const [email, setEmail] = useState("");
  const [status, setStatus] = useState<Status>("idle");
  const [message, setMessage] = useState<string | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setStatus("sending");
    setMessage(null);
    try {
      const res = await fetch("/api/email-itinerary", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email,
          itinerary,
          sources: sources?.length ? sources : itinerary.sources || [],
          summary: itinerary.summary ?? null,
        }),
      });
      const data = (await res.json()) as {
        ok?: boolean;
        error?: string;
        message?: string;
      };
      if (!res.ok || !data.ok) {
        setStatus("error");
        setMessage(data.error || "Could not send the itinerary.");
        return;
      }
      setStatus("success");
      setMessage(data.message || "Sent — check your inbox.");
    } catch (err) {
      setStatus("error");
      setMessage(
        err instanceof Error ? err.message : "Network error while sending.",
      );
    }
  }

  return (
    <aside className={styles.wrap} aria-label="Email itinerary PDF">
      <h3>Email this plan</h3>
      <p className={styles.hint}>
        We’ll send your day-wise itinerary to n8n, which generates a PDF and
        emails it to you.
      </p>
      <form className={styles.form} onSubmit={onSubmit}>
        <label className={styles.label} htmlFor="itinerary-email">
          Email
        </label>
        <div className={styles.row}>
          <input
            id="itinerary-email"
            type="email"
            name="email"
            autoComplete="email"
            required
            placeholder="you@example.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            disabled={status === "sending"}
            className={styles.input}
          />
          <button
            type="submit"
            className={styles.button}
            disabled={status === "sending" || !email.trim()}
          >
            {status === "sending" ? "Sending…" : "Send PDF"}
          </button>
        </div>
      </form>
      {message ? (
        <p
          className={
            status === "success"
              ? styles.success
              : status === "error"
                ? styles.error
                : styles.hint
          }
          role="status"
        >
          {message}
        </p>
      ) : null}
    </aside>
  );
}
