// Pure transcript-processing utilities shared between the UI (app/_helpers.tsx)
// and the test suite. No React imports — safe to run in a plain Node environment.

import type { ChatMessage } from "../app/_types";

export function timestampValue(value: string | null | undefined, fallback = 0): number {
  if (!value) return fallback;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function appendTranscriptChunk(current: string, chunk: string): string {
  if (!chunk) return current;
  if (current.endsWith(chunk)) return current;
  // Idempotency guard: a delta can overlap a snapshot that already carried the
  // same section. Each section opens with a unique `### <header> - <timestamp>`
  // line; if that exact header already exists in the transcript, drop the chunk
  // so the parser never emits a duplicate bubble (and never re-orders).
  const headerMatch = chunk.match(/^[\s﻿]*(###\s+.+?)\n/m) ?? chunk.match(/(###\s+.+?)(?:\n|$)/);
  if (headerMatch && current.includes(headerMatch[1].trim())) return current;
  return `${current}${chunk}`;
}

// parseChatHistory accepts an optional body sanitizer so it can be tested
// without pulling in the full sanitizeRunnerOutput chain from _helpers.tsx.
export function parseChatHistory(
  raw: string,
  sanitize: (body: string) => string = (s) => s,
): ChatMessage[] {
  const text = raw.replace(/^﻿/, "").trim();
  if (!text) return [];
  const messages: ChatMessage[] = [];
  const sections = text.split(/^###\s+/m).filter((s) => s.trim());
  for (const section of sections) {
    const newline = section.indexOf("\n");
    const header = (newline === -1 ? section : section.slice(0, newline)).trim();
    const rawBody = (newline === -1 ? "" : section.slice(newline + 1)).trim();
    const lower = header.toLowerCase();
    const [, timestamp = ""] = header.split(/\s+-\s+/);
    const attachMatch = rawBody.match(/_Attached:\s*([^_]+)_\s*$/);
    const attachments = attachMatch
      ? attachMatch[1].split(",").map((s) => s.trim()).filter(Boolean)
      : [];
    const body = sanitize(attachMatch ? rawBody.slice(0, attachMatch.index).trim() : rawBody);
    if (lower.startsWith("user query")) {
      messages.push({ role: "user", title: "You", timestamp, body, attachments, kind: "ask" });
    } else if (lower.startsWith("team kickoff")) {
      messages.push({ role: "user", title: "Team kickoff", timestamp, body, attachments, kind: "kickoff" });
    } else if (lower.startsWith("pipeline kickoff")) {
      messages.push({ role: "user", title: "Pipeline kickoff", timestamp, body, attachments, kind: "kickoff" });
    } else if (lower.startsWith("dualith answer")) {
      const questionMatch = body.match(/\nQUESTION:\s*(.+)$/);
      const cleanBody = questionMatch ? body.slice(0, questionMatch.index!).trim() : body;
      const question = questionMatch ? questionMatch[1].trim() : undefined;
      const kind = questionMatch ? "question" : "answer";
      messages.push({ role: "agent", title: "Dualith", timestamp, body: cleanBody, question, attachments: [], kind });
    } else if (lower.startsWith("plan feedback")) {
      messages.push({ role: "user", title: "Plan feedback", timestamp, body, attachments: [], kind: "kickoff" });
    } else if (lower.startsWith("plan")) {
      messages.push({ role: "plan", title: "Plan", timestamp, body, attachments: [], kind: "plan" });
    } else if (lower.startsWith("circuit breaker")) {
      messages.push({ role: "circuit-breaker", title: "Circuit Breaker", timestamp, body, attachments: [], kind: "circuit-breaker" });
    } else if (lower.startsWith("git operation") || lower.startsWith("scaffold")) {
      // Structural operations — render as system notes (collapsed, not agent bubbles)
      messages.push({ role: "system", title: header.split(/\s+-\s+/)[0] || "System", timestamp, body, attachments: [], kind: "system" });
    } else {
      messages.push({
        role: "system",
        title: header.split(/\s+-\s+/)[0] || "System",
        timestamp,
        body,
        attachments: [],
        kind: "system",
      });
    }
  }
  return messages;
}

// Stable sort by (header timestamp, parse index). Makes Chat tab render order
// chronological regardless of how the underlying string was assembled.
export function sortChatMessages(messages: ChatMessage[]): ChatMessage[] {
  return messages
    .map((message, index) => ({ message, index, ts: timestampValue(message.timestamp, index) }))
    .sort((a, b) => (a.ts === b.ts ? a.index - b.index : a.ts - b.ts))
    .map((entry) => entry.message);
}

// Incremental parse cache: re-parses only the tail appended since the last
// call, so large transcripts don't get re-split on every delta.
export function makeTranscriptCache<T>(parse: (raw: string) => T[]): (raw: string) => T[] {
  let cachedRaw = "";
  let cachedResult: T[] = [];
  return (raw: string): T[] => {
    if (raw === cachedRaw) return cachedResult;
    if (raw.startsWith(cachedRaw) && cachedRaw.length > 0) {
      const lastBoundary = cachedRaw.lastIndexOf("\n### ");
      const safeBase = lastBoundary >= 0 ? cachedRaw.slice(0, lastBoundary) : "";
      const safeMessages = safeBase ? parse(safeBase) : [];
      const tail = raw.slice(safeBase.length);
      const tailMessages = parse(tail);
      cachedRaw = raw;
      cachedResult = [...safeMessages, ...tailMessages];
      return cachedResult;
    }
    cachedRaw = raw;
    cachedResult = parse(raw);
    return cachedResult;
  };
}
