// Runner output sanitization: strip JSON payloads, format error messages, parse SSE.

import type { RunnerId, SseMessage, AgentResult } from "../app/_types";
import { runnerLabels } from "../app/_constants";

// ── Internal helpers ─────────────────────────────────────────────────────────

function decodeJsonStringLiteral(value: string) {
  try {
    return JSON.parse(`"${value.replace(/\r?\n/g, "\\n")}"`) as string;
  } catch {
    return value.replace(/\\"/g, '"').replace(/\\n/g, "\n").replace(/\\t/g, "\t");
  }
}

function runnerLabelForMessage(runner?: RunnerId | "") {
  return runner && runner !== "auto" ? runnerLabels[runner] : "The runner";
}

function runnerResetHint(text: string) {
  const match = text.match(/resets?\s+(?:in\s+)?([^."\n}]+)/i);
  if (!match) return "";
  const hint = match[1].trim().replace(/\s+/g, " ").replace(/[.,;:]+$/, "");
  return hint ? `resets ${hint}` : "";
}

function looksLikeRunnerResultPayload(text: string) {
  const sample = text.slice(0, 1000);
  return (
    /^\s*\{/.test(text) &&
    /"type"\s*:\s*"result"|"api_error_status"|"is_error"|"stop_reason"|"session_id"|"total_cost_usd"/.test(
      sample
    )
  );
}

function resultTextFromRunnerPayload(text: string) {
  const trimmed = text.trim();
  if (!trimmed) return "";
  if (trimmed.startsWith("{")) {
    try {
      const parsed = JSON.parse(trimmed) as unknown;
      if (parsed && typeof parsed === "object") {
        const record = parsed as Record<string, unknown>;
        const isRunnerResult =
          record.type === "result" ||
          "api_error_status" in record ||
          "is_error" in record ||
          "stop_reason" in record ||
          "session_id" in record;
        if (isRunnerResult) {
          for (const key of ["result", "message", "error", "detail"]) {
            const value = record[key];
            if (typeof value === "string" && value.trim()) return value.trim();
          }
          return "";
        }
      }
    } catch {
      // Fall through to regex path for truncated JSON written by a killed process.
    }
  }
  const resultMatch = trimmed.match(/"result"\s*:\s*"((?:\\.|[^"\\])*)"/);
  if (resultMatch) return decodeJsonStringLiteral(resultMatch[1]).trim();
  const messageMatch = trimmed.match(/"(?:message|error|detail)"\s*:\s*"((?:\\.|[^"\\])*)"/);
  return messageMatch ? decodeJsonStringLiteral(messageMatch[1]).trim() : "";
}

function friendlyRunnerPayloadMessage(value: string, runner?: RunnerId | "") {
  const text = value.trim();
  if (!text || !looksLikeRunnerResultPayload(text)) return "";
  const core = resultTextFromRunnerPayload(text);
  const source = `${core} ${text}`.toLowerCase();
  const label = runnerLabelForMessage(runner);
  const reset = runnerResetHint(core || text);
  const suffix = reset ? ` (${reset})` : "";
  if (source.includes("session limit")) return `${label} hit its session limit${suffix}.`;
  if (
    source.includes("rate limit") ||
    (source.includes("api_error_status") && source.includes("429"))
  )
    return `${label} hit its rate limit${suffix}.`;
  if (source.includes("quota") || source.includes("usage limit"))
    return `${label} is out of quota headroom${suffix}.`;
  if (core) return `${label} failed: ${core}`;
  return `${label} failed without a readable error.`;
}

// ── Public exports ───────────────────────────────────────────────────────────

export function sanitizeRunnerOutput(value: string, runner?: RunnerId | "") {
  const text = value.trim();
  if (!text) return "";
  const wholeMessage = friendlyRunnerPayloadMessage(text, runner);
  if (wholeMessage) return wholeMessage;

  const output: string[] = [];
  for (const line of value.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) {
      if (output.length && output[output.length - 1] !== "") output.push("");
      continue;
    }
    const message = friendlyRunnerPayloadMessage(trimmed, runner);
    if (message) {
      if (!output.includes(message)) output.push(message);
      continue;
    }
    if (looksLikeRunnerResultPayload(trimmed)) continue;
    output.push(line);
  }
  return output.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

export function safeResultBody(result: AgentResult) {
  if (result.status === "stopped") return "";
  if (result.status === "error") {
    return (
      sanitizeRunnerOutput(result.error || "", result.runner) ||
      "The run hit a problem. Check the Log panel for details."
    );
  }
  return sanitizeRunnerOutput(result.content?.trim() || "", result.runner);
}

export async function readErrorMessage(response: Response) {
  const body = await response.text();
  if (!body) return `HTTP ${response.status}`;
  try {
    const parsed = JSON.parse(body) as { detail?: unknown };
    if (typeof parsed.detail === "string") return parsed.detail;
  } catch {
    return body;
  }
  return body;
}

export async function readSseResponse(
  response: Response,
  onMessage: (message: SseMessage) => void
) {
  if (!response.body) throw new Error("Streaming response was empty.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split("\n\n");
    buffer = events.pop() ?? "";
    for (const event of events) {
      for (const line of event.split("\n")) {
        if (!line.startsWith("data: ")) continue;
        let parsed: SseMessage;
        try {
          parsed = JSON.parse(line.slice(6)) as SseMessage;
        } catch {
          continue;
        }
        onMessage(parsed);
      }
    }
  }
}
