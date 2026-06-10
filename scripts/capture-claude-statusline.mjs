import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(scriptDir, "..");
const cachePath = process.env.DUALITH_CLAUDE_RATE_LIMIT_CACHE || join(repoRoot, ".dualith", "claude-rate-limits.json");

function readStdin() {
  try {
    return readFileSync(0, "utf8");
  } catch {
    return "";
  }
}

function normalizeLabel(value) {
  return String(value || "")
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
}

function windowFromLabel(...labels) {
  const label = normalizeLabel(labels.filter(Boolean).join(" "));
  if (!label) return "";
  if (/\b5\s*h\b/.test(label) || /\b5\s*hour/.test(label) || /\bfive\s*hour/.test(label)) return "five_hour";
  if (/\b7\s*d\b/.test(label) || /\b7\s*day/.test(label) || /\bweek/.test(label) || /\bweekly\b/.test(label)) return "weekly";
  return "";
}

function coerceNumber(value) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value !== "string") return null;
  const match = value.match(/-?\d+(?:\.\d+)?/);
  return match ? Number(match[0]) : null;
}

function pickField(object, names) {
  for (const name of names) {
    if (Object.prototype.hasOwnProperty.call(object, name)) return [name, object[name]];
  }

  const normalizedNames = new Set(names.map(normalizeLabel));
  for (const [key, value] of Object.entries(object)) {
    if (normalizedNames.has(normalizeLabel(key))) return [key, value];
  }

  return ["", undefined];
}

function toLimitRecord(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;

  const [percentField, percentValue] = pickField(value, [
    "used_percentage",
    "usage_percentage",
    "percent_used",
    "percentage",
    "usedPercent",
    "usedPercentage",
    "usagePercent",
    "usagePercentage",
    "used_ratio",
    "usage_ratio",
  ]);
  const percent = coerceNumber(percentValue);
  if (percent === null || percent < 0) return null;

  const isRatio = /ratio|fraction/i.test(percentField);
  const usedPercentage = isRatio && percent <= 1 ? percent * 100 : percent;
  const [, resetsAt] = pickField(value, ["resets_at", "reset_at", "resetsAt", "resetAt"]);

  return {
    used_percentage: Number(usedPercentage.toFixed(2)),
    resets_at: typeof resetsAt === "string" || typeof resetsAt === "number" ? resetsAt : "",
    raw_label: label,
  };
}

function normalizeRateLimits(rateLimits) {
  const normalized = { five_hour: null, weekly: null };
  if (!rateLimits || typeof rateLimits !== "object") return normalized;

  function assign(window, value, label) {
    if (!window || normalized[window]) return;
    const record = toLimitRecord(value, label);
    if (record) normalized[window] = record;
  }

  function walk(value, path = []) {
    if (!value || typeof value !== "object") return;

    if (Array.isArray(value)) {
      value.forEach((entry, index) => walk(entry, [...path, String(index)]));
      return;
    }

    const objectLabels = [
      value.name,
      value.label,
      value.window,
      value.window_name,
      value.type,
      value.id,
      ...path,
    ];
    assign(windowFromLabel(...objectLabels), value, objectLabels.filter(Boolean).join(" / "));

    for (const [key, child] of Object.entries(value)) {
      assign(windowFromLabel(key, ...path), child, [...path, key].join(" / "));
      walk(child, [...path, key]);
    }
  }

  walk(rateLimits);
  return normalized;
}

function formatPercent(record, label) {
  if (!record || typeof record.used_percentage !== "number") return "";
  return `${label} ${Math.min(999, Math.max(0, record.used_percentage)).toFixed(record.used_percentage >= 10 ? 0 : 1)}%`;
}

function statusLine(payload, rateLimits) {
  const model =
    payload?.model?.display_name ||
    payload?.model?.name ||
    payload?.model?.id ||
    payload?.model ||
    "Claude";
  const pieces = [String(model).replace(/\s+/g, " ").trim() || "Claude"];
  const fiveHour = formatPercent(rateLimits.five_hour, "5h");
  const weekly = formatPercent(rateLimits.weekly, "7d");
  if (fiveHour) pieces.push(fiveHour);
  if (weekly) pieces.push(weekly);
  return pieces.join(" | ");
}

const raw = readStdin();
const normalizedRaw = raw.replace(/^\uFEFF/, "").trim();
let payload = {};
try {
  payload = normalizedRaw ? JSON.parse(normalizedRaw) : {};
} catch {
  payload = {};
}

const rateLimits = normalizeRateLimits(payload.rate_limits);
const captured = {
  captured_at: new Date().toISOString(),
  source: "claude_statusline",
  rate_limits: rateLimits,
  raw_keys: payload && typeof payload === "object" ? Object.keys(payload).sort() : [],
};

try {
  mkdirSync(dirname(cachePath), { recursive: true });
  const tempPath = `${cachePath}.tmp`;
  writeFileSync(tempPath, `${JSON.stringify(captured, null, 2)}\n`, "utf8");
  renameSync(tempPath, cachePath);
} catch {
  // Statusline rendering should keep working even if the cache cannot be written.
}

process.stdout.write(`${statusLine(payload, rateLimits)}\n`);
