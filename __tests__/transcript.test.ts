import { describe, it, expect } from "vitest";
import {
  timestampValue,
  appendTranscriptChunk,
  parseChatHistory,
  sortChatMessages,
  makeTranscriptCache,
} from "../lib/transcript";

// ── timestampValue ────────────────────────────────────────────────────────────

describe("timestampValue", () => {
  it("parses a valid ISO timestamp", () => {
    expect(timestampValue("2026-06-19T10:00:00.000Z")).toBeGreaterThan(0);
  });
  it("returns fallback for null", () => {
    expect(timestampValue(null, 42)).toBe(42);
  });
  it("returns fallback for invalid string", () => {
    expect(timestampValue("not-a-date", 7)).toBe(7);
  });
  it("defaults fallback to 0", () => {
    expect(timestampValue(undefined)).toBe(0);
  });
});

// ── appendTranscriptChunk ─────────────────────────────────────────────────────

describe("appendTranscriptChunk", () => {
  const base = "### User Query - 2026-06-19T10:00:01.000Z\n\nq\n\n";

  it("appends a new section", () => {
    const delta = "### Dualith Answer - 2026-06-19T10:00:05.000Z\n\na\n\n";
    expect(appendTranscriptChunk(base, delta)).toBe(base + delta);
  });

  it("drops an exact-suffix duplicate", () => {
    expect(appendTranscriptChunk(base, base)).toBe(base);
  });

  it("drops a chunk whose header already exists (idempotent guard)", () => {
    const dup = "### User Query - 2026-06-19T10:00:01.000Z\n\nq\n\n";
    expect(appendTranscriptChunk(base, dup)).toBe(base);
  });

  it("returns current unchanged for empty chunk", () => {
    expect(appendTranscriptChunk(base, "")).toBe(base);
  });

  it("appends a genuinely new header even if body text overlaps", () => {
    // Same body text 'q' but different timestamp → different header → should append
    const different = "### User Query - 2026-06-19T11:00:00.000Z\n\nq\n\n";
    const result = appendTranscriptChunk(base, different);
    expect(result).toBe(base + different);
  });
});

// ── parseChatHistory ──────────────────────────────────────────────────────────

describe("parseChatHistory", () => {
  it("returns empty for empty string", () => {
    expect(parseChatHistory("")).toHaveLength(0);
  });

  it("parses a user query", () => {
    const raw = "### User Query - 2026-06-19T10:00:01.000Z\n\nhello\n\n";
    const [msg] = parseChatHistory(raw);
    expect(msg.role).toBe("user");
    expect(msg.kind).toBe("ask");
    expect(msg.timestamp).toBe("2026-06-19T10:00:01.000Z");
    expect(msg.body).toBe("hello");
  });

  it("parses a dualith answer", () => {
    const raw = "### Dualith Answer - 2026-06-19T10:00:05.000Z\n\nhi there\n\n";
    const [msg] = parseChatHistory(raw);
    expect(msg.role).toBe("agent");
    expect(msg.kind).toBe("answer");
  });

  it("parses a plan section", () => {
    const raw = "### Plan - 2026-06-19T10:00:10.000Z\n\n1. step\n\n";
    const [msg] = parseChatHistory(raw);
    expect(msg.role).toBe("plan");
    expect(msg.kind).toBe("plan");
  });

  it("parses plan feedback as user kickoff (order matters: 'plan feedback' before 'plan')", () => {
    const raw = "### Plan Feedback - 2026-06-19T10:00:15.000Z\n\nadd tests\n\n";
    const [msg] = parseChatHistory(raw);
    expect(msg.role).toBe("user");
    expect(msg.kind).toBe("kickoff");
  });

  it("parses team kickoff", () => {
    const raw = "### Team Kickoff - 2026-06-19T10:00:01.000Z\n\nbuild the thing\n\n";
    const [msg] = parseChatHistory(raw);
    expect(msg.role).toBe("user");
    expect(msg.kind).toBe("kickoff");
  });

  it("parses circuit breaker", () => {
    const raw = "### Circuit Breaker - 2026-06-19T10:00:20.000Z\n\nstopped\n\n";
    const [msg] = parseChatHistory(raw);
    expect(msg.role).toBe("circuit-breaker");
    expect(msg.kind).toBe("circuit-breaker");
  });

  it("classifies unknown headers as system notes (not agent bubbles)", () => {
    const raw = "### Git Operation - 2026-06-19T10:00:30.000Z\n\ncommitted\n\n";
    const [msg] = parseChatHistory(raw);
    expect(msg.role).toBe("system");
    expect(msg.kind).toBe("system");
    expect(msg.title).toBe("Git Operation");
  });

  it("classifies Scaffold as a system note", () => {
    const raw = "### Scaffold - 2026-06-19T10:00:30.000Z\n\ndone\n\n";
    const [msg] = parseChatHistory(raw);
    expect(msg.role).toBe("system");
  });

  it("extracts attachments from _Attached: ..._ suffix", () => {
    const raw = "### User Query - 2026-06-19T10:00:01.000Z\n\nhello\n\n_Attached: file1.png, file2.pdf_\n\n";
    const [msg] = parseChatHistory(raw);
    expect(msg.attachments).toEqual(["file1.png", "file2.pdf"]);
    expect(msg.body).not.toContain("_Attached:");
  });

  it("applies the optional sanitizer to the body", () => {
    const raw = "### User Query - 2026-06-19T10:00:01.000Z\n\nraw\n\n";
    const [msg] = parseChatHistory(raw, () => "sanitized");
    expect(msg.body).toBe("sanitized");
  });

  it("strips BOM prefix", () => {
    const raw = "﻿### User Query - 2026-06-19T10:00:01.000Z\n\nhello\n\n";
    const msgs = parseChatHistory(raw);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].role).toBe("user");
  });
});

// ── sortChatMessages ──────────────────────────────────────────────────────────

describe("sortChatMessages", () => {
  it("re-sorts a flipped transcript to chronological order", () => {
    const raw =
      "### Dualith Answer - 2026-06-19T10:00:05.000Z\n\nthe answer\n\n" +
      "### User Query - 2026-06-19T10:00:01.000Z\n\nthe question\n\n";
    const sorted = sortChatMessages(parseChatHistory(raw));
    expect(sorted[0].role).toBe("user");
    expect(sorted[1].role).toBe("agent");
  });

  it("keeps insertion order for same-second messages (stable tiebreak)", () => {
    const raw =
      "### User Query - 2026-06-19T10:00:01.000Z\n\nq\n\n" +
      "### Dualith Answer - 2026-06-19T10:00:01.000Z\n\na\n\n";
    const sorted = sortChatMessages(parseChatHistory(raw));
    expect(sorted[0].role).toBe("user");
    expect(sorted[1].role).toBe("agent");
  });

  it("is a no-op on already-sorted input", () => {
    const raw =
      "### User Query - 2026-06-19T10:00:01.000Z\n\nq\n\n" +
      "### Dualith Answer - 2026-06-19T10:00:05.000Z\n\na\n\n";
    const sorted = sortChatMessages(parseChatHistory(raw));
    expect(sorted.map((m) => m.role)).toEqual(["user", "agent"]);
  });

  it("handles multiple rounds in the right order", () => {
    const raw =
      "### User Query - 2026-06-19T10:00:01.000Z\n\nq1\n\n" +
      "### Dualith Answer - 2026-06-19T10:00:05.000Z\n\na1\n\n" +
      "### Dualith Answer - 2026-06-19T10:01:00.000Z\n\na2 (out of order)\n\n" +
      "### User Query - 2026-06-19T10:00:50.000Z\n\nq2\n\n";
    const sorted = sortChatMessages(parseChatHistory(raw));
    expect(sorted.map((m) => m.body)).toEqual(["q1", "a1", "q2", "a2 (out of order)"]);
  });
});

// ── makeTranscriptCache ───────────────────────────────────────────────────────

describe("makeTranscriptCache (incremental parse)", () => {
  it("returns same reference for identical input", () => {
    const cache = makeTranscriptCache(parseChatHistory);
    const raw = "### User Query - 2026-06-19T10:00:01.000Z\n\nq\n\n";
    const r1 = cache(raw);
    const r2 = cache(raw);
    expect(r1).toBe(r2);
  });

  it("appends new messages without re-parsing the full string", () => {
    const cache = makeTranscriptCache(parseChatHistory);
    const base = "### User Query - 2026-06-19T10:00:01.000Z\n\nq\n\n";
    const full = base + "### Dualith Answer - 2026-06-19T10:00:05.000Z\n\na\n\n";
    const r1 = cache(base);
    expect(r1).toHaveLength(1);
    const r2 = cache(full);
    expect(r2).toHaveLength(2);
  });

  it("full re-parses on snapshot reconcile (non-append change)", () => {
    const cache = makeTranscriptCache(parseChatHistory);
    cache("### User Query - 2026-06-19T10:00:01.000Z\n\nq\n\n");
    // A cleared transcript (snapshot after clear) is a non-append
    const r = cache("");
    expect(r).toHaveLength(0);
  });
});
