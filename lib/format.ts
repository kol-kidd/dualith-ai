// Pure formatting utilities: numbers, durations, timestamps, labels.

export function compactNumber(value: number | null | undefined) {
  if (!value) return "-";
  return Intl.NumberFormat("en-US", {
    notation: value >= 10000 ? "compact" : "standard",
    maximumFractionDigits: value >= 10000 ? 1 : 0,
  }).format(value);
}

export function timestampLabel(value: string | null) {
  if (!value) return "--:--:--";
  return new Date(value).toLocaleTimeString("en-US", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function timestampValue(value: string | null | undefined, fallback = 0) {
  if (!value) return fallback;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function durationLabel(ms: number | null | undefined) {
  if (!ms) return "-";
  const seconds = Math.max(1, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}

export function formatElapsed(seconds: number) {
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

export function isRecent(value: string | null) {
  if (!value) return false;
  return Date.now() - new Date(value).getTime() < 2500;
}

export function priorityLabel(priority: string) {
  return priority && priority !== "other" ? priority.toUpperCase() : "Note";
}

export function priorityTone(priority: string): "green" | "amber" | "red" | "cyan" | "muted" {
  if (priority === "p0" || priority === "p1") return "red";
  if (priority === "p2" || priority === "p3") return "amber";
  return "muted";
}
