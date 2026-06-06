import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Dualith Command Center",
  description: "Dense local command center for AI agent workspace orchestration."
};

export default function RootLayout({
  children
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
