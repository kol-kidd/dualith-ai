"use client";

import { useEffect, useRef, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:4200";

type ProviderId = "claude" | "openai" | "openrouter" | "gemini" | "custom";
type ProviderMode = "subscription" | "api_key";

interface SlotConfig {
  provider: ProviderId;
  mode: ProviderMode;
  apiKey: string;
  model: string;
  baseUrl: string;
}

interface TestResult {
  ok: boolean;
  message: string;
}

const PROVIDER_META: Record<ProviderId, {
  label: string;
  icon: string;
  supportsSubscription: boolean;
  defaultModel: string;
  apiKeyEnv: string;
  hint: string;
}> = {
  claude: {
    label: "Claude",
    icon: "◆",
    supportsSubscription: true,
    defaultModel: "claude-sonnet-4-6",
    apiKeyEnv: "ANTHROPIC_API_KEY",
    hint: "Anthropic · subscription via claude CLI or direct API key",
  },
  openai: {
    label: "OpenAI",
    icon: "⬡",
    supportsSubscription: true,
    defaultModel: "gpt-4o",
    apiKeyEnv: "OPENAI_API_KEY",
    hint: "OpenAI · subscription via codex CLI or direct API key",
  },
  openrouter: {
    label: "OpenRouter",
    icon: "⇄",
    supportsSubscription: false,
    defaultModel: "anthropic/claude-sonnet-4-6",
    apiKeyEnv: "OPENROUTER_API_KEY",
    hint: "Routes to any model — API key only",
  },
  gemini: {
    label: "Gemini",
    icon: "✦",
    supportsSubscription: false,
    defaultModel: "gemini-2.0-flash",
    apiKeyEnv: "GEMINI_API_KEY",
    hint: "Google · API key only",
  },
  custom: {
    label: "Custom",
    icon: "⚙",
    supportsSubscription: false,
    defaultModel: "",
    apiKeyEnv: "",
    hint: "Any OpenAI-compatible endpoint",
  },
};

const SLOT_LABELS = ["Runner A", "Runner B"];

function defaultSlot(): SlotConfig {
  return {
    provider: "claude",
    mode: "subscription",
    apiKey: "",
    model: "",
    baseUrl: "",
  };
}

function ProviderCard({
  id,
  selected,
  onClick,
}: {
  id: ProviderId;
  selected: boolean;
  onClick: () => void;
}) {
  const meta = PROVIDER_META[id];
  return (
    <button
      onClick={onClick}
      style={{
        background: selected
          ? "color-mix(in srgb, var(--dualith-cyan) 12%, var(--dualith-surface))"
          : "var(--dualith-surface)",
        border: `1.5px solid ${selected ? "var(--dualith-cyan)" : "var(--dualith-line)"}`,
        borderRadius: 8,
        padding: "12px 14px",
        textAlign: "left",
        cursor: "pointer",
        transition: "border-color 0.15s, background 0.15s",
        display: "flex",
        flexDirection: "column",
        gap: 4,
        minWidth: 120,
      }}
    >
      <span style={{ fontSize: 20, color: "var(--dualith-cyan)", lineHeight: 1 }}>
        {meta.icon}
      </span>
      <span style={{ fontSize: 13, fontWeight: 600, color: "var(--dualith-text-strong)" }}>
        {meta.label}
      </span>
      <span style={{ fontSize: 11, color: "var(--dualith-text-faint)", lineHeight: 1.3 }}>
        {meta.hint}
      </span>
    </button>
  );
}

const inputStyle: React.CSSProperties = {
  width: "100%",
  padding: "9px 12px",
  background: "var(--dualith-input-bg)",
  color: "var(--dualith-input-text)",
  border: "1.5px solid var(--dualith-line)",
  borderRadius: 7,
  fontSize: 13,
  fontFamily: "monospace",
  boxSizing: "border-box",
  outline: "none",
};

const labelStyle: React.CSSProperties = {
  display: "block",
  fontSize: 12,
  color: "var(--dualith-text-muted)",
  marginBottom: 6,
  textTransform: "uppercase",
  letterSpacing: "0.08em",
};

function ModelField({
  slot,
  onChange,
  token,
}: {
  slot: SlotConfig;
  onChange: (updated: SlotConfig) => void;
  token: string;
}) {
  const meta = PROVIDER_META[slot.provider];
  const [models, setModels] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [fetchError, setFetchError] = useState("");
  // Track the latest request so a stale in-flight response can't overwrite state.
  const reqId = useRef(0);

  // Fetch the live model catalog whenever the key/provider/base-url settles.
  useEffect(() => {
    if (!slot.apiKey) {
      setModels([]);
      setFetchError("");
      setLoading(false);
      return;
    }
    const id = ++reqId.current;
    setLoading(true);
    setFetchError("");
    const handle = setTimeout(async () => {
      try {
        const resp = await fetch(`${API_BASE}/api/setup/models`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Dualith-Token": token },
          body: JSON.stringify({
            slot: {
              provider: slot.provider,
              mode: slot.mode,
              api_key: slot.apiKey || null,
              model: slot.model || null,
              base_url: slot.baseUrl || null,
            },
          }),
        });
        const data = await resp.json();
        if (id !== reqId.current) return; // superseded
        if (data.ok && Array.isArray(data.models) && data.models.length > 0) {
          const list: string[] = data.models;
          setModels(list);
          // Auto-select the default if present, else first, when current is invalid.
          if (!list.includes(slot.model)) {
            const next = list.includes(meta.defaultModel) ? meta.defaultModel : list[0];
            onChange({ ...slot, model: next });
          }
        } else {
          setModels([]);
          setFetchError(data.message || "Couldn't load models");
        }
      } catch {
        if (id !== reqId.current) return;
        setModels([]);
        setFetchError("Couldn't reach backend to load models");
      } finally {
        if (id === reqId.current) setLoading(false);
      }
    }, 500);
    return () => clearTimeout(handle);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slot.apiKey, slot.provider, slot.baseUrl, token]);

  const hint = !slot.apiKey
    ? "Enter your API key to load available models"
    : loading
    ? "Loading models…"
    : fetchError
    ? `${fetchError} — enter the model ID manually`
    : "";

  return (
    <div>
      <label style={labelStyle}>Model</label>
      {models.length > 0 ? (
        <select
          value={slot.model}
          onChange={(e) => onChange({ ...slot, model: e.target.value })}
          style={{ ...inputStyle, cursor: "pointer", appearance: "auto" }}
        >
          {models.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
      ) : (
        <input
          type="text"
          value={slot.model}
          onChange={(e) => onChange({ ...slot, model: e.target.value })}
          placeholder={meta.defaultModel || "e.g. gpt-4o"}
          style={inputStyle}
        />
      )}
      {hint && (
        <p style={{ margin: "6px 0 0", fontSize: 11, color: "var(--dualith-text-faint)" }}>
          {hint}
        </p>
      )}
    </div>
  );
}

function SlotStep({
  slotIndex,
  slot,
  onChange,
  token,
}: {
  slotIndex: number;
  slot: SlotConfig;
  onChange: (updated: SlotConfig) => void;
  token: string;
}) {
  const meta = PROVIDER_META[slot.provider];

  function setProvider(id: ProviderId) {
    const m = PROVIDER_META[id];
    onChange({
      ...slot,
      provider: id,
      mode: m.supportsSubscription ? "subscription" : "api_key",
      model: m.defaultModel,
    });
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <div>
        <p style={{ margin: "0 0 10px", fontSize: 12, color: "var(--dualith-text-muted)", textTransform: "uppercase", letterSpacing: "0.08em" }}>
          {SLOT_LABELS[slotIndex]} — choose provider
        </p>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 8 }}>
          {(Object.keys(PROVIDER_META) as ProviderId[]).map((id) => (
            <ProviderCard
              key={id}
              id={id}
              selected={slot.provider === id}
              onClick={() => setProvider(id)}
            />
          ))}
        </div>
      </div>

      {meta.supportsSubscription && (
        <div>
          <p style={{ margin: "0 0 8px", fontSize: 12, color: "var(--dualith-text-muted)", textTransform: "uppercase", letterSpacing: "0.08em" }}>
            Connection mode
          </p>
          <div style={{ display: "flex", gap: 8 }}>
            {(["subscription", "api_key"] as ProviderMode[]).map((m) => (
              <button
                key={m}
                onClick={() => onChange({ ...slot, mode: m })}
                style={{
                  padding: "7px 16px",
                  borderRadius: 6,
                  border: `1.5px solid ${slot.mode === m ? "var(--dualith-cyan)" : "var(--dualith-line)"}`,
                  background: slot.mode === m
                    ? "color-mix(in srgb, var(--dualith-cyan) 14%, var(--dualith-surface))"
                    : "var(--dualith-surface)",
                  color: slot.mode === m ? "var(--dualith-cyan)" : "var(--dualith-text-muted)",
                  fontSize: 13,
                  fontWeight: slot.mode === m ? 600 : 400,
                  cursor: "pointer",
                  transition: "all 0.15s",
                }}
              >
                {m === "subscription" ? "Subscription (CLI)" : "API Key"}
              </button>
            ))}
          </div>
          {slot.mode === "subscription" && (
            <p style={{ margin: "8px 0 0", fontSize: 12, color: "var(--dualith-text-faint)" }}>
              Dualith will call the{" "}
              <code style={{ background: "var(--dualith-surface-hover)", padding: "1px 5px", borderRadius: 3 }}>
                {slot.provider === "claude" ? "claude" : "codex"}
              </code>{" "}
              CLI — make sure you're logged in.
            </p>
          )}
        </div>
      )}

      {slot.mode === "api_key" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div>
            <label style={{ display: "block", fontSize: 12, color: "var(--dualith-text-muted)", marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.08em" }}>
              API Key{meta.apiKeyEnv ? ` (${meta.apiKeyEnv})` : ""}
            </label>
            <input
              type="password"
              value={slot.apiKey}
              onChange={(e) => onChange({ ...slot, apiKey: e.target.value })}
              placeholder="sk-…"
              style={{
                width: "100%",
                padding: "9px 12px",
                background: "var(--dualith-input-bg)",
                color: "var(--dualith-input-text)",
                border: "1.5px solid var(--dualith-line)",
                borderRadius: 7,
                fontSize: 13,
                fontFamily: "monospace",
                boxSizing: "border-box",
                outline: "none",
              }}
            />
          </div>
          <ModelField slot={slot} onChange={onChange} token={token} />
          {slot.provider === "custom" && (
            <div>
              <label style={{ display: "block", fontSize: 12, color: "var(--dualith-text-muted)", marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.08em" }}>
                Base URL
              </label>
              <input
                type="text"
                value={slot.baseUrl}
                onChange={(e) => onChange({ ...slot, baseUrl: e.target.value })}
                placeholder="https://your-api.example.com/v1"
                style={{
                  width: "100%",
                  padding: "9px 12px",
                  background: "var(--dualith-input-bg)",
                  color: "var(--dualith-input-text)",
                  border: "1.5px solid var(--dualith-line)",
                  borderRadius: 7,
                  fontSize: 13,
                  fontFamily: "monospace",
                  boxSizing: "border-box",
                  outline: "none",
                }}
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SlotSummaryCard({
  label,
  slot,
  testResult,
}: {
  label: string;
  slot: SlotConfig;
  testResult: TestResult | null;
}) {
  const meta = PROVIDER_META[slot.provider];
  return (
    <div
      style={{
        background: "var(--dualith-surface)",
        border: "1.5px solid var(--dualith-line)",
        borderRadius: 10,
        padding: "16px 18px",
        flex: 1,
      }}
    >
      <div style={{ fontSize: 11, color: "var(--dualith-text-faint)", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 8 }}>
        {label}
      </div>
      <div style={{ fontSize: 20, marginBottom: 4 }}>{meta.icon}</div>
      <div style={{ fontSize: 14, fontWeight: 600, color: "var(--dualith-text-strong)", marginBottom: 2 }}>
        {meta.label}
      </div>
      <div style={{ fontSize: 12, color: "var(--dualith-text-muted)", marginBottom: 10 }}>
        {slot.mode === "subscription" ? "Subscription (CLI)" : `API key · ${slot.model || meta.defaultModel}`}
      </div>
      {testResult && (
        <div
          style={{
            fontSize: 12,
            color: testResult.ok ? "var(--dualith-cyan)" : "var(--dualith-red)",
            display: "flex",
            alignItems: "center",
            gap: 5,
          }}
        >
          <span>{testResult.ok ? "✓" : "✗"}</span>
          <span style={{ wordBreak: "break-word" }}>{testResult.message}</span>
        </div>
      )}
    </div>
  );
}

export function SetupWizard({ token, onComplete }: { token: string; onComplete: () => void }) {
  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [slotA, setSlotA] = useState<SlotConfig>(defaultSlot);
  const [slotB, setSlotB] = useState<SlotConfig>({
    ...defaultSlot(),
    provider: "openai",
    mode: "subscription",
    model: "",
  });
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testResults, setTestResults] = useState<{ runner_a: TestResult | null; runner_b: TestResult | null }>({
    runner_a: null,
    runner_b: null,
  });
  const [saveError, setSaveError] = useState("");

  const bothPassed =
    testResults.runner_a?.ok === true && testResults.runner_b?.ok === true;

  async function runTest() {
    setTesting(true);
    setTestResults({ runner_a: null, runner_b: null });
    setSaveError("");
    try {
      const resp = await fetch(`${API_BASE}/api/setup/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Dualith-Token": token },
        body: JSON.stringify({
          runner_a: {
            provider: slotA.provider,
            mode: slotA.mode,
            api_key: slotA.apiKey || null,
            model: slotA.model || null,
            base_url: slotA.baseUrl || null,
          },
          runner_b: {
            provider: slotB.provider,
            mode: slotB.mode,
            api_key: slotB.apiKey || null,
            model: slotB.model || null,
            base_url: slotB.baseUrl || null,
          },
        }),
      });
      const data = await resp.json();
      setTestResults({ runner_a: data.runner_a, runner_b: data.runner_b });
    } catch {
      setSaveError("Could not reach Dualith backend. Is it running?");
    } finally {
      setTesting(false);
    }
  }

  async function handleLaunch() {
    setSaving(true);
    setSaveError("");
    try {
      const resp = await fetch(`${API_BASE}/api/setup/save`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Dualith-Token": token },
        body: JSON.stringify({
          runner_a: {
            provider: slotA.provider,
            mode: slotA.mode,
            api_key: slotA.apiKey || null,
            model: slotA.model || null,
            base_url: slotA.baseUrl || null,
          },
          runner_b: {
            provider: slotB.provider,
            mode: slotB.mode,
            api_key: slotB.apiKey || null,
            model: slotB.model || null,
            base_url: slotB.baseUrl || null,
          },
        }),
      });
      if (!resp.ok) {
        const txt = await resp.text();
        setSaveError(`Save failed: ${txt.slice(0, 200)}`);
        return;
      }
      onComplete();
    } catch {
      setSaveError("Could not save configuration. Is the backend running?");
    } finally {
      setSaving(false);
    }
  }

  const STEPS = ["Runner A", "Runner B", "Review & Launch"];

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 200,
        background: "var(--dualith-bg)",
        display: "flex",
        alignItems: "stretch",
        fontFamily: "inherit",
      }}
    >
      {/* Left panel — branding + step indicator */}
      <div
        style={{
          width: 220,
          flexShrink: 0,
          borderRight: "1px solid var(--dualith-line)",
          padding: "40px 28px",
          display: "flex",
          flexDirection: "column",
          gap: 0,
        }}
      >
        <div style={{ marginBottom: 40 }}>
          <div style={{ fontSize: 18, fontWeight: 700, color: "var(--dualith-text-strong)", letterSpacing: "-0.02em" }}>
            Dualith
          </div>
          <div style={{ fontSize: 12, color: "var(--dualith-text-faint)", marginTop: 2 }}>
            First-time setup
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 0 }}>
          {STEPS.map((label, i) => {
            const stepNum = (i + 1) as 1 | 2 | 3;
            const isActive = step === stepNum;
            const isDone = step > stepNum;
            return (
              <div key={label} style={{ display: "flex", flexDirection: "column", alignItems: "flex-start" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <div
                    style={{
                      width: 22,
                      height: 22,
                      borderRadius: "50%",
                      border: `2px solid ${isActive ? "var(--dualith-cyan)" : isDone ? "var(--dualith-cyan)" : "var(--dualith-line)"}`,
                      background: isDone ? "var(--dualith-cyan)" : "transparent",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      flexShrink: 0,
                      fontSize: 11,
                      color: isDone ? "var(--dualith-bg)" : isActive ? "var(--dualith-cyan)" : "var(--dualith-text-faint)",
                      fontWeight: 700,
                    }}
                  >
                    {isDone ? "✓" : stepNum}
                  </div>
                  <span
                    style={{
                      fontSize: 13,
                      color: isActive ? "var(--dualith-text-strong)" : isDone ? "var(--dualith-cyan)" : "var(--dualith-text-faint)",
                      fontWeight: isActive ? 600 : 400,
                    }}
                  >
                    {label}
                  </span>
                </div>
                {i < STEPS.length - 1 && (
                  <div
                    style={{
                      width: 2,
                      height: 28,
                      background: isDone ? "var(--dualith-cyan)" : "var(--dualith-line)",
                      marginLeft: 10,
                      marginTop: 2,
                      marginBottom: 2,
                      opacity: 0.5,
                    }}
                  />
                )}
              </div>
            );
          })}
        </div>

        <div style={{ marginTop: "auto", fontSize: 11, color: "var(--dualith-text-faint)", lineHeight: 1.5 }}>
          Config is stored locally in{" "}
          <code style={{ background: "var(--dualith-surface-hover)", padding: "1px 4px", borderRadius: 3 }}>
            .dualith/
          </code>
        </div>
      </div>

      {/* Right panel — step content */}
      <div
        style={{
          flex: 1,
          padding: "40px 48px",
          overflowY: "auto",
          display: "flex",
          flexDirection: "column",
        }}
      >
        {step === 1 && (
          <>
            <h2 style={{ margin: "0 0 6px", fontSize: 20, fontWeight: 700, color: "var(--dualith-text-strong)" }}>
              Configure Runner A
            </h2>
            <p style={{ margin: "0 0 28px", fontSize: 14, color: "var(--dualith-text-muted)" }}>
              Runner A handles primary agent work — Lead, PM, Architect, and planning phases.
            </p>
            <SlotStep slotIndex={0} slot={slotA} onChange={setSlotA} token={token} />
            <div style={{ marginTop: 32, display: "flex", justifyContent: "flex-end" }}>
              <button onClick={() => setStep(2)} style={primaryBtn}>
                Next →
              </button>
            </div>
          </>
        )}

        {step === 2 && (
          <>
            <h2 style={{ margin: "0 0 6px", fontSize: 20, fontWeight: 700, color: "var(--dualith-text-strong)" }}>
              Configure Runner B
            </h2>
            <p style={{ margin: "0 0 28px", fontSize: 14, color: "var(--dualith-text-muted)" }}>
              Runner B handles review agents — Tester, specialists, and the final reviewer.
            </p>
            <SlotStep slotIndex={1} slot={slotB} onChange={setSlotB} token={token} />
            <div style={{ marginTop: 32, display: "flex", gap: 10, justifyContent: "flex-end" }}>
              <button onClick={() => setStep(1)} style={ghostBtn}>← Back</button>
              <button onClick={() => setStep(3)} style={primaryBtn}>Next →</button>
            </div>
          </>
        )}

        {step === 3 && (
          <>
            <h2 style={{ margin: "0 0 6px", fontSize: 20, fontWeight: 700, color: "var(--dualith-text-strong)" }}>
              Review & Launch
            </h2>
            <p style={{ margin: "0 0 28px", fontSize: 14, color: "var(--dualith-text-muted)" }}>
              Test both connections before launching. You can reconfigure any time from settings.
            </p>

            <div style={{ display: "flex", gap: 12, marginBottom: 24 }}>
              <SlotSummaryCard label="Runner A" slot={slotA} testResult={testResults.runner_a} />
              <SlotSummaryCard label="Runner B" slot={slotB} testResult={testResults.runner_b} />
            </div>

            <button
              onClick={runTest}
              disabled={testing}
              style={{
                ...ghostBtn,
                opacity: testing ? 0.6 : 1,
                marginBottom: 12,
                alignSelf: "flex-start",
              }}
            >
              {testing ? "Testing…" : "Test connections"}
            </button>

            {saveError && (
              <p style={{ color: "var(--dualith-red)", fontSize: 13, margin: "0 0 12px" }}>{saveError}</p>
            )}

            <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
              <button onClick={() => setStep(2)} style={ghostBtn}>← Back</button>
              <button
                onClick={handleLaunch}
                disabled={!bothPassed || saving}
                style={{
                  ...primaryBtn,
                  opacity: (!bothPassed || saving) ? 0.45 : 1,
                  cursor: (!bothPassed || saving) ? "not-allowed" : "pointer",
                }}
              >
                {saving ? "Saving…" : "Launch Dualith →"}
              </button>
            </div>

            {!bothPassed && !testing && (
              <p style={{ fontSize: 12, color: "var(--dualith-text-faint)", textAlign: "right", marginTop: 8 }}>
                Test both connections first to enable launch.
              </p>
            )}
          </>
        )}
      </div>
    </div>
  );
}

const primaryBtn: React.CSSProperties = {
  padding: "9px 22px",
  borderRadius: 7,
  border: "none",
  background: "var(--dualith-cyan)",
  color: "#fff",
  fontSize: 13,
  fontWeight: 600,
  cursor: "pointer",
};

const ghostBtn: React.CSSProperties = {
  padding: "9px 18px",
  borderRadius: 7,
  border: "1.5px solid var(--dualith-line)",
  background: "transparent",
  color: "var(--dualith-text-muted)",
  fontSize: 13,
  cursor: "pointer",
};
