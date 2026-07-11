export default function HomePage() {
  return (
    <main
      style={{
        maxWidth: 720,
        margin: "0 auto",
        padding: "4rem 1.5rem",
      }}
    >
      <p
        style={{
          letterSpacing: "0.08em",
          textTransform: "uppercase",
          fontSize: "0.75rem",
          color: "#4a6b63",
          marginBottom: "0.75rem",
        }}
      >
        Capstone · Phase 0
      </p>
      <h1
        style={{
          fontSize: "clamp(2rem, 5vw, 3rem)",
          lineHeight: 1.15,
          margin: "0 0 1rem",
          fontWeight: 600,
        }}
      >
        AI Itinerary Planner
      </h1>
      <p style={{ fontSize: "1.125rem", lineHeight: 1.6, maxWidth: "36ch" }}>
        Voice-first Jaipur travel planning. Companion UI stub — mic, day-wise
        itinerary, and sources land in later phases.
      </p>
      <ul
        style={{
          marginTop: "2rem",
          paddingLeft: "1.25rem",
          lineHeight: 1.8,
          color: "#334448",
        }}
      >
        <li>Scope: Jaipur only · 2–4 day trips</li>
        <li>Runtime: LangGraph (Python) + LangChain tools/RAG</li>
        <li>Agent stub: START → orchestrator → END</li>
      </ul>
    </main>
  );
}
