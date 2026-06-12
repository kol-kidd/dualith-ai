"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Typed delta protocol shared with backend/app/events.py.
 *
 * Snapshot frames keep the legacy `{ type, payload }` shape and are only sent
 * on connect or after an explicit resync request. Everything else is a small
 * flat frame describing one orchestration change.
 */

type Envelope = {
  v: number;
  type: string;
  ts: string;
  project: string;
  run_id?: string;
  task_id?: string;
};

export type AgentStatusEvent = Envelope & {
  type: "agent_status";
  agent: string;
  role_label: string;
  runner: string;
  model: string;
  state: "starting" | "running" | "done" | "error" | "stopped";
  round: number;
  detail: string;
};

export type AgentOutputDeltaEvent = Envelope & {
  type: "agent_output_delta";
  agent: string;
  seq: number;
  kind: "message" | "command" | "progress";
  text: string;
};

export type HandoffEvent = Envelope & {
  type: "handoff";
  from: string;
  to: string;
  note: string;
  question?: string;
  round: number;
};

export type VerdictEvent = Envelope & {
  type: "verdict";
  agent: string;
  verdict: "approved" | "changes_requested";
  summary: string;
  round: number;
};

export type PhaseEvent = Envelope & {
  type: "phase";
  phase: string;
  status: string;
  runner?: string;
  round: number;
  narration: string;
};

export type RunErrorEvent = Envelope & {
  type: "run_error";
  agent: string;
  runner: string;
  code: string;
  message: string;
  reset_hint: string;
  action: string;
};

export type ChatEvent = Envelope & {
  type: "chat";
  file: string;
  body: string;
};

export type DualithDeltaEvent =
  | AgentStatusEvent
  | AgentOutputDeltaEvent
  | HandoffEvent
  | VerdictEvent
  | PhaseEvent
  | RunErrorEvent
  | ChatEvent;

const DELTA_TYPES = new Set([
  "agent_status",
  "agent_output_delta",
  "handoff",
  "verdict",
  "phase",
  "run_error",
  "chat",
]);

export type SocketStatus = "Connecting..." | "Live" | "Reconnecting..." | "Connection error";

type UseDualithSocketOptions = {
  url: string;
  onSnapshot: (payload: unknown) => void;
  onDelta: (event: DualithDeltaEvent) => void;
};

export function useDualithSocket({ url, onSnapshot, onDelta }: UseDualithSocketOptions): {
  status: SocketStatus;
  resync: () => void;
} {
  const [status, setStatus] = useState<SocketStatus>("Connecting...");
  const socketRef = useRef<WebSocket | null>(null);
  const onSnapshotRef = useRef(onSnapshot);
  const onDeltaRef = useRef(onDelta);
  // Per-run delta sequence tracking: a gap means a slow client dropped frames.
  const seqRef = useRef<Map<string, number>>(new Map());

  onSnapshotRef.current = onSnapshot;
  onDeltaRef.current = onDelta;

  const resync = useCallback(() => {
    const socket = socketRef.current;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: "resync" }));
    }
  }, []);

  useEffect(() => {
    let closed = false;
    let reconnectTimer: number | undefined;

    const connect = () => {
      const socket = new WebSocket(url);
      socketRef.current = socket;
      socket.addEventListener("open", () => {
        setStatus("Live");
        seqRef.current.clear();
        // The server sends a fresh snapshot on connect — no REST fetch needed.
      });
      socket.addEventListener("close", () => {
        socketRef.current = null;
        setStatus("Reconnecting...");
        if (!closed) reconnectTimer = window.setTimeout(connect, 1500);
      });
      socket.addEventListener("error", () => setStatus("Connection error"));
      socket.addEventListener("message", (event) => {
        let message: { type?: unknown; payload?: unknown };
        try {
          message = JSON.parse(event.data as string);
        } catch {
          return;
        }
        if (!message || typeof message.type !== "string") return;

        if (message.type === "snapshot") {
          seqRef.current.clear();
          onSnapshotRef.current(message.payload);
          return;
        }
        if (DELTA_TYPES.has(message.type)) {
          const delta = message as unknown as DualithDeltaEvent;
          if (delta.type === "agent_output_delta" && delta.run_id) {
            const last = seqRef.current.get(delta.run_id) ?? 0;
            seqRef.current.set(delta.run_id, delta.seq);
            if (delta.seq > last + 1 && last !== 0) {
              resync(); // gap — re-hydrate, but still apply the delta below
            }
          }
          onDeltaRef.current(delta);
          return;
        }
        // Legacy broadcast types still carry a full snapshot payload.
        const payload = message.payload as { projects?: unknown } | undefined;
        if (payload && Array.isArray(payload.projects)) {
          onSnapshotRef.current(payload);
        }
      });
    };

    connect();
    return () => {
      closed = true;
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, [url, resync]);

  return { status, resync };
}
