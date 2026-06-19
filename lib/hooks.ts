"use client";
// React hooks extracted from _helpers.tsx.
// All hooks use React state/effects — this module must remain client-only.

import { useState, useEffect, useRef, useMemo } from "react";
import type { ChatMessage, TeamMessage } from "../app/_types";
import {
  ThemeId,
  DensityId,
  THEME_KEY,
  DENSITY_KEY,
} from "../app/_constants";
import {
  appendTranscriptChunk,
  makeTranscriptCache,
  parseChatHistory as _parseChatHistory,
  sortChatMessages,
} from "./transcript";
import { sanitizeRunnerOutput } from "./runner-output";
import { parseAgentChat } from "./team-room";

function parseChatHistory(raw: string) {
  return _parseChatHistory(raw, sanitizeRunnerOutput);
}

export function useIncrementalChatHistory(raw: string): ChatMessage[] {
  const cache = useRef(makeTranscriptCache(parseChatHistory));
  return useMemo(() => sortChatMessages(cache.current(raw)), [raw]);
}

export function useIncrementalAgentChat(raw: string): TeamMessage[] {
  const cache = useRef(makeTranscriptCache(parseAgentChat));
  return useMemo(() => cache.current(raw), [raw]);
}

export function useRunHeartbeat(active: boolean) {
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!active) return undefined;
    const timer = window.setInterval(() => setTick((value) => value + 1), 30_000);
    return () => window.clearInterval(timer);
  }, [active]);

  return tick;
}

export function useElapsedSeconds(startedAt: string) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setTick((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const started = new Date(startedAt).getTime();
  return Number.isFinite(started) ? Math.max(0, Math.floor((Date.now() - started) / 1000)) : 0;
}

export function useAppearance() {
  const [theme, setTheme] = useState<ThemeId>("daylight");
  const [density, setDensity] = useState<DensityId>("comfortable");
  const [appearanceLoaded, setAppearanceLoaded] = useState(false);

  useEffect(() => {
    const savedTheme = (localStorage.getItem(THEME_KEY) as ThemeId | null) ?? "daylight";
    const savedDensity = (localStorage.getItem(DENSITY_KEY) as DensityId | null) ?? "comfortable";
    setTheme(savedTheme);
    setDensity(savedDensity);
    setAppearanceLoaded(true);
  }, []);

  useEffect(() => {
    if (!appearanceLoaded) return;
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem(THEME_KEY, theme);
  }, [appearanceLoaded, theme]);

  useEffect(() => {
    if (!appearanceLoaded) return;
    if (density === "comfortable") document.documentElement.removeAttribute("data-density");
    else document.documentElement.setAttribute("data-density", density);
    localStorage.setItem(DENSITY_KEY, density);
  }, [appearanceLoaded, density]);

  return { theme, setTheme, density, setDensity };
}
