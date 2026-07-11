export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <head>
        <title>AI Itinerary Planner</title>
        <meta
          name="description"
          content="Voice-first AI travel planner for Jaipur (capstone Phase 0 stub)."
        />
      </head>
      <body
        style={{
          margin: 0,
          fontFamily:
            "Georgia, 'Times New Roman', Cambria, serif",
          background:
            "linear-gradient(160deg, #f7f1e8 0%, #e8f0f2 45%, #dfe8e4 100%)",
          color: "#1c2a2e",
          minHeight: "100vh",
        }}
      >
        {children}
      </body>
    </html>
  );
}
