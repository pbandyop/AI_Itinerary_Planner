import type { CSSProperties, ReactNode } from "react";
import type { Metadata } from "next";
import { Plus_Jakarta_Sans } from "next/font/google";

import "./globals.css";

const jakarta = Plus_Jakarta_Sans({
  subsets: ["latin"],
  variable: "--font-jakarta",
  display: "swap",
  weight: ["400", "500", "600", "700"],
});

export const metadata: Metadata = {
  title: "VocalVoyage — Voice Travel Planner",
  description:
    "Voice-first AI travel companion for Jaipur. Speak to plan, confirm, edit days, and get cited tips.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: ReactNode;
}>) {
  const fontVars = {
    ["--font-display"]: "var(--font-jakarta), 'Segoe UI', sans-serif",
    ["--font-body"]: "var(--font-jakarta), 'Segoe UI', sans-serif",
  } as CSSProperties;

  return (
    <html lang="en" className={jakarta.variable}>
      <body style={fontVars}>{children}</body>
    </html>
  );
}
