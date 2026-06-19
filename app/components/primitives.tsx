"use client";

// Dualith team-room UI components. Extracted from page.tsx.

import React  from "react";
import type { ReactNode, CSSProperties } from "react";
import { humanizeRoleKind } from "../../lib/humanize";
import type { RunnerId, LaneInfo, TeamMessageRole, PixelMascotConfig } from "../_types";
import {
  sanitizeRunnerOutput,
  renderMentions,
  extractQuoteRef,
  pixelMascotAccessory,
  splitOutputBlocks,
  teamRoomRoleKind,
} from "../_helpers";
import { TeamConversationPanel, TeamRoom } from "./chat";

export function SectionHeader({ title, meta, children }: { title: string; meta?: string; children?: ReactNode }) {
  return (
    <div className="flex h-9 shrink-0 items-center justify-between border-b border-line px-3 text-xs">
      <span className="font-medium uppercase tracking-widest text-muted">{title}</span>
      {children ?? (meta ? <span className="text-faint">{meta}</span> : null)}
    </div>
  );
}

export function Badge({ label, tone, className = "" }: { label: string; tone: "green" | "amber" | "red" | "cyan" | "muted"; className?: string }) {
  const cls =
    tone === "green"
      ? "bg-emerald-950 text-ok border-emerald-800"
      : tone === "amber"
        ? "bg-amber-950 text-warn border-amber-800"
        : tone === "red"
          ? "bg-red-950 text-danger border-red-800"
          : tone === "cyan"
            ? "bg-cyan-950 text-accent border-cyan-800"
            : "bg-surface text-muted border-line";
  return <span className={`border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider ${cls} ${className}`}>{label}</span>;
}

export function EmptyState({ message }: { message: string }) {
  return <div role="status" className="px-3 py-4 text-xs text-faint">{message}</div>;
}

export function RunnerMascot({ runner, size = 18 }: { runner: RunnerId; size?: number }) {
  const tone =
    runner === "codex"
      ? "text-accent"
      : runner === "claude"
        ? "text-ok"
        : "text-warn";
  const mascotClass = `shrink-0 ${tone}`;
  const bgFill = "var(--dualith-bg)";

  if (runner === "codex") {
    return (
      <svg
        aria-hidden="true"
        viewBox="0 0 24 24"
        width={size}
        height={size}
        className={mascotClass}
        fill="currentColor"
        shapeRendering="crispEdges"
      >
        <rect x="11" y="2" width="2" height="3" opacity="0.75" />
        <rect x="9" y="1" width="6" height="1" opacity="0.45" />
        <rect x="7" y="5" width="10" height="2" opacity="0.55" />
        <rect x="5" y="7" width="14" height="10" opacity="0.9" />
        <rect x="3" y="10" width="2" height="4" opacity="0.65" />
        <rect x="19" y="10" width="2" height="4" opacity="0.65" />
        <rect x="7" y="17" width="10" height="2" opacity="0.65" />
        <rect x="8" y="19" width="8" height="3" opacity="0.5" />
        <rect x="7" y="22" width="3" height="1" opacity="0.75" />
        <rect x="14" y="22" width="3" height="1" opacity="0.75" />
        <rect x="8" y="10" width="2" height="2" fill={bgFill} />
        <rect x="14" y="10" width="2" height="2" fill={bgFill} />
        <rect x="10" y="14" width="4" height="1" fill={bgFill} opacity="0.85" />
        <rect x="6" y="8" width="1" height="8" fill={bgFill} opacity="0.25" />
      </svg>
    );
  }

  if (runner === "claude") {
    return (
      <svg
        aria-hidden="true"
        viewBox="0 0 24 24"
        width={size}
        height={size}
        className={mascotClass}
        fill="currentColor"
        shapeRendering="crispEdges"
      >
        <rect x="10" y="3" width="4" height="2" opacity="0.45" />
        <rect x="8" y="5" width="8" height="2" opacity="0.65" />
        <rect x="6" y="7" width="12" height="9" opacity="0.9" />
        <rect x="4" y="10" width="2" height="4" opacity="0.55" />
        <rect x="18" y="10" width="2" height="4" opacity="0.55" />
        <rect x="8" y="16" width="8" height="3" opacity="0.7" />
        <rect x="10" y="19" width="4" height="2" opacity="0.5" />
        <rect x="7" y="8" width="2" height="2" fill={bgFill} opacity="0.25" />
        <rect x="15" y="8" width="2" height="2" fill={bgFill} opacity="0.25" />
        <rect x="9" y="11" width="2" height="2" fill={bgFill} />
        <rect x="13" y="11" width="2" height="2" fill={bgFill} />
        <rect x="10" y="15" width="4" height="1" fill={bgFill} opacity="0.85" />
        <rect x="11" y="7" width="2" height="12" fill={bgFill} opacity="0.18" />
      </svg>
    );
  }

  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      width={size}
      height={size}
      className={mascotClass}
      fill="currentColor"
      shapeRendering="crispEdges"
    >
      <rect x="4" y="5" width="5" height="5" opacity="0.8" />
      <rect x="15" y="14" width="5" height="5" opacity="0.8" />
      <rect x="9" y="6" width="5" height="2" opacity="0.65" />
      <rect x="14" y="8" width="2" height="3" opacity="0.65" />
      <rect x="16" y="11" width="2" height="3" opacity="0.65" />
      <rect x="10" y="17" width="5" height="2" opacity="0.65" />
      <rect x="8" y="15" width="2" height="3" opacity="0.65" />
      <rect x="6" y="12" width="2" height="3" opacity="0.65" />
      <rect x="5" y="6" width="2" height="2" fill={bgFill} />
      <rect x="17" y="16" width="2" height="2" fill={bgFill} />
      <rect x="13" y="4" width="4" height="1" opacity="0.45" />
      <rect x="16" y="3" width="1" height="3" opacity="0.45" />
      <rect x="7" y="20" width="4" height="1" opacity="0.45" />
      <rect x="7" y="18" width="1" height="3" opacity="0.45" />
    </svg>
  );
}

export function DualithLogo() {
  return (
    <div className="dualith-logo" aria-label="Dualith">
      <svg viewBox="0 0 40 24" role="img" aria-hidden="true" focusable="false" shapeRendering="crispEdges">
        <rect className="dualith-logo__shadow" x="5" y="20" width="17" height="2" />
        <rect className="dualith-logo__outline" x="7" y="2" width="12" height="2" />
        <rect className="dualith-logo__outline" x="5" y="4" width="16" height="2" />
        <rect className="dualith-logo__outline" x="3" y="6" width="20" height="12" />
        <rect className="dualith-logo__outline" x="5" y="18" width="16" height="2" />
        <rect className="dualith-logo__outline" x="8" y="20" width="10" height="2" />

        <rect className="dualith-logo__left" x="5" y="6" width="8" height="12" />
        <rect className="dualith-logo__left" x="7" y="4" width="6" height="2" />
        <rect className="dualith-logo__left-light" x="6" y="7" width="3" height="2" />
        <rect className="dualith-logo__left-light" x="5" y="10" width="2" height="5" />
        <rect className="dualith-logo__right" x="14" y="6" width="7" height="12" />
        <rect className="dualith-logo__right" x="14" y="4" width="5" height="2" />
        <rect className="dualith-logo__right-raw" x="19" y="7" width="2" height="4" />
        <rect className="dualith-logo__right-raw" x="17" y="14" width="4" height="3" />

        <rect className="dualith-logo__split" x="13" y="5" width="1" height="14" />
        <rect className="dualith-logo__split" x="14" y="10" width="1" height="3" />
        <rect className="dualith-logo__scar" x="19" y="5" width="2" height="2" />
        <rect className="dualith-logo__scar" x="17" y="9" width="2" height="2" />
        <rect className="dualith-logo__scar" x="20" y="13" width="2" height="2" />

        <rect className="dualith-logo__cut" x="7" y="9" width="3" height="2" />
        <rect className="dualith-logo__cut" x="16" y="8" width="3" height="3" />
        <rect className="dualith-logo__cut" x="8" y="15" width="4" height="1" />
        <rect className="dualith-logo__cut" x="15" y="15" width="5" height="1" />

        <rect className="dualith-logo__coin" x="29" y="6" width="5" height="1" />
        <rect className="dualith-logo__coin" x="27" y="7" width="9" height="7" />
        <rect className="dualith-logo__coin" x="29" y="14" width="5" height="1" />
        <rect className="dualith-logo__coin-dark" x="31" y="8" width="1" height="5" />
        <rect className="dualith-logo__coin-dark" x="28" y="10" width="7" height="1" />
      </svg>
      <span>DUALITH</span>
    </div>
  );
}

// Project setup forms

export function AgentProse({ body }: { body: string }) {
  const quote = extractQuoteRef(body);
  const text = sanitizeRunnerOutput(quote ? quote.rest : body);
  const paragraphs = text.split(/\n{2,}/).filter(Boolean);
  return (
    <div className="team-turn__prose">
      {quote && (
        <div className="team-quote">
          <div className="team-quote__head">re: {quote.quoteRole}{quote.quoteRef ? ` · ${quote.quoteRef}` : ""}</div>
        </div>
      )}
      {paragraphs.map((para, i) => (
        <p key={i} style={{ margin: "0 0 0.4em" }}>{renderMentions(para)}</p>
      ))}
    </div>
  );
}

// ─── Direction E: TeamRoom ────────────────────────────────────────────────────
// Replaces TeamConversationPanel + LiveWorkingBubble for the team stream.

export function LaneMatrix({ lanes }: { lanes: LaneInfo[] }) {
  if (!lanes || lanes.length < 2) return null;
  return (
    <div className="lane-matrix" role="table" aria-label="Parallel build lanes">
      <div className="lane-matrix__head" role="row">
        <span role="columnheader" aria-label="Status" />
        <span role="columnheader">lane</span>
        <span role="columnheader">files</span>
        <span role="columnheader">progress</span>
      </div>
      {lanes.map((l) => {
        const isDone = l.status === "done" || l.status === "completed";
        const isRunning = l.status === "running" || l.status === "active";
        const isFailed = l.status === "failed" || l.status === "error";
        const isSkipped = l.status === "skipped";
        const statusClass = isDone ? "is-ok" : isRunning ? "is-run" : isFailed ? "is-err" : isSkipped ? "is-na" : "is-queued";
        const pctLabel = isDone ? "done" : l.pct != null ? `${l.pct}%` : "--";
        return (
          <div key={l.lane} className={`lane-matrix__row ${statusClass}`} role="row">
            <span className="lane-matrix__glyph" role="cell" aria-hidden="true" />
            <span className="lane-matrix__name" role="cell">{l.lane}</span>
            <span className="lane-matrix__files" role="cell" title={l.files?.join(", ")}>
              {l.files && l.files.length > 0
                ? l.files.slice(0, 2).map((f) => <code key={f}>{f.split("/").pop()}</code>)
                : <span>--</span>}
              {l.files && l.files.length > 2 && <span className="lane-matrix__more">+{l.files.length - 2}</span>}
            </span>
            <span className="lane-matrix__pct" role="cell">{pctLabel}</span>
          </div>
        );
      })}
    </div>
  );
}

const ROLE_PIXEL_MASCOTS: Partial<Record<TeamMessageRole, PixelMascotConfig>> = {
  task: { accent: "#8aa0b8", variant: "note" },
  pm: { accent: "#df7d55", variant: "target" },
  architect: { accent: "#9a8fd6", variant: "blueprint" },
  planner: { accent: "#a4bd68", variant: "clipboard" },
  decomposer: { accent: "#7d73d6", variant: "decompose" },
  lead: { accent: "#4fa8d5", variant: "bolt" },
  tester: { accent: "#4caf7d", variant: "test" },
  architecture_reviewer: { accent: "#a78bfa", variant: "blueprint" },
  security_reviewer: { accent: "#f87171", variant: "shield" },
  performance_reviewer: { accent: "#f0b84a", variant: "speed" },
  maintainability_reviewer: { accent: "#64d9af", variant: "wrench" },
  teammate: { accent: "#5fa8c8", variant: "review" },
  summarizer: { accent: "#e8a838", variant: "summary" },
  plan: { accent: "#8fbf75", variant: "clipboard" },
  note: { accent: "#8aa0b8", variant: "note" },
  agent: { accent: "#8aa0b8", variant: "default" },
};

const DUALITH_PIXEL_MASCOT: PixelMascotConfig = { accent: "#4fa8d5", variant: "dualith" };

const DEFAULT_PIXEL_MASCOT: PixelMascotConfig = { accent: "#8aa0b8", variant: "default" };

function PixelAgentMascot({ config, size = 32, label = "Agent mascot", className = "" }: { config: PixelMascotConfig; size?: number; label?: string; className?: string }) {
  const style = {
    width: size,
    height: size,
    "--agent-mascot-accent": config.accent,
    "--agent-mascot-bg": `${config.accent}22`,
    "--agent-mascot-border": `${config.accent}66`,
  } as React.CSSProperties;
  const dualith = config.variant === "dualith";
  return (
    <div className={`agent-mascot ${dualith ? "agent-mascot--dualith" : ""} ${className}`.trim()} style={style} role="img" aria-label={label}>
      <svg viewBox="0 0 24 24" className="agent-mascot__sprite" shapeRendering="crispEdges" aria-hidden="true">
        {dualith ? (
          <>
            <rect className="agent-mascot__shadow" x="5" y="20" width="17" height="2" />
            <rect className="agent-mascot__outline" x="7" y="2" width="12" height="2" />
            <rect className="agent-mascot__outline" x="5" y="4" width="16" height="2" />
            <rect className="agent-mascot__outline" x="3" y="6" width="20" height="12" />
            <rect className="agent-mascot__outline" x="5" y="18" width="16" height="2" />
            <rect className="agent-mascot__outline" x="8" y="20" width="10" height="2" />
            {pixelMascotAccessory(config.variant)}
          </>
        ) : (
          <>
            <rect className="agent-mascot__shadow" x="6" y="21" width="12" height="1" />
            <rect className="agent-mascot__body" x="11" y="2" width="2" height="2" />
            <rect className="agent-mascot__body" x="8" y="4" width="8" height="2" />
            <rect className="agent-mascot__body" x="5" y="7" width="14" height="11" />
            <rect className="agent-mascot__body" x="8" y="18" width="8" height="2" />
            <rect className="agent-mascot__body" x="3" y="10" width="2" height="4" />
            <rect className="agent-mascot__body" x="19" y="10" width="2" height="4" />
            <rect className="agent-mascot__glint" x="7" y="8" width="3" height="2" />
            <rect className="agent-mascot__dark" x="9" y="11" width="2" height="2" />
            <rect className="agent-mascot__dark" x="14" y="11" width="2" height="2" />
            <rect className="agent-mascot__dark" x="10" y="16" width="4" height="1" />
            {pixelMascotAccessory(config.variant)}
          </>
        )}
      </svg>
    </div>
  );
}

export function RolePixelMascot({ role, size = 32 }: { role: TeamMessageRole; size?: number }) {
  const config = ROLE_PIXEL_MASCOTS[role] ?? DEFAULT_PIXEL_MASCOT;
  return <PixelAgentMascot config={config} size={size} label={`${humanizeRoleKind(teamRoomRoleKind(role))} mascot`} />;
}

function InlineText({ text }: { text: string }) {
  // Tokenize inline code, bold, and italic so markdown renders instead of showing raw markers.
  const parts = text.split(/(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*|\[[^\]]+\]\([^)]+\))/g).filter(Boolean);
  return (
    <>
      {parts.map((part, index) => {
        if (part.startsWith("`") && part.endsWith("`")) {
          return <code key={index} className="border border-line-hard bg-surface px-1 py-0.5 text-[0.95em] text-text-soft">{part.slice(1, -1)}</code>;
        }
        if (part.startsWith("**") && part.endsWith("**")) {
          return <strong key={index} className="font-semibold text-text-strong">{part.slice(2, -2)}</strong>;
        }
        if (part.startsWith("*") && part.endsWith("*")) {
          return <em key={index} className="text-text-soft">{part.slice(1, -1)}</em>;
        }
        const link = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
        if (link) {
          return <span key={index} className="text-accent underline decoration-dotted underline-offset-2">{link[1]}</span>;
        }
        return <span key={index}>{part}</span>;
      })}
    </>
  );
}

function FormattedTextBlock({ value }: { value: string }) {
  const nodes: ReactNode[] = [];
  let paragraph: string[] = [];

  const flushParagraph = () => {
    const text = paragraph.join(" ").trim();
    if (text) {
      nodes.push(
        <p key={`p-${nodes.length}`} className="mb-2 last:mb-0">
          <InlineText text={text} />
        </p>
      );
    }
    paragraph = [];
  };

  for (const line of value.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) {
      flushParagraph();
      continue;
    }

    const heading = trimmed.match(/^#{1,4}\s+(.+)$/);
    if (heading) {
      flushParagraph();
      nodes.push(
        <h3 key={`h-${nodes.length}`} className="mb-2 mt-3 text-xs font-semibold uppercase tracking-widest text-text-soft first:mt-0">
          <InlineText text={heading[1]} />
        </h3>
      );
      continue;
    }

    const check = trimmed.match(/^(?:[-*]\s+)?\[(x| )\]\s+(.+)$/i);
    if (check) {
      flushParagraph();
      const done = check[1].toLowerCase() === "x";
      nodes.push(
        <div key={`c-${nodes.length}`} className="mb-1.5 grid grid-cols-[18px_1fr] gap-2">
          <span className={done ? "text-ok" : "text-text-faint"}>{done ? "x" : "-"}</span>
          <span className={done ? "text-text-faint line-through" : "text-text-soft"}><InlineText text={check[2]} /></span>
        </div>
      );
      continue;
    }

    const bullet = trimmed.match(/^(?:[-*]|\d+\.)\s+(.+)$/);
    if (bullet) {
      flushParagraph();
      nodes.push(
        <div key={`b-${nodes.length}`} className="mb-1.5 grid grid-cols-[18px_1fr] gap-2">
          <span className="text-accent">-</span>
          <span><InlineText text={bullet[1]} /></span>
        </div>
      );
      continue;
    }

    paragraph.push(trimmed);
  }

  flushParagraph();
  return <>{nodes}</>;
}

export const FormattedAgentOutput = React.memo(function FormattedAgentOutput({ content }: { content: string }) {
  const blocks = splitOutputBlocks(sanitizeRunnerOutput(content));
  return (
    <div className="dualith-agent-prose space-y-3 text-sm leading-6 text-text">
      {blocks.map((block, index) => (
        block.kind === "code" ? (
          <pre key={index} className="max-h-80 overflow-auto whitespace-pre-wrap break-words border border-line-hard bg-bg p-3 text-xs leading-5 text-text-muted" style={{ fontFamily: "var(--dualith-font-mono)" }}>
            {block.lang && <div className="mb-2 text-[10px] uppercase tracking-widest text-text-faint">{block.lang}</div>}
            <code>{block.value}</code>
          </pre>
        ) : (
          <div key={index}>
            <FormattedTextBlock value={block.value} />
          </div>
        )
      ))}
    </div>
  );
});

export function DualithMascot({ size = 32 }: { size?: number }) {
  return <PixelAgentMascot config={DUALITH_PIXEL_MASCOT} size={size} label="Dualith mascot" />;
}

