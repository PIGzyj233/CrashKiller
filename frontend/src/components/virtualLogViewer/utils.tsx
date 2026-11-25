import type { ReactNode } from "react";
import type { LogDigestErrorEntry, LogLine } from "../../api/types";

export function lineContainsSession(
  text: string,
  sessionId: string | null | undefined,
): boolean {
  if (!sessionId || !sessionId.trim()) {
    return false;
  }
  return text.toLowerCase().includes(sessionId.trim().toLowerCase());
}

export function renderLineWithSessionHighlight(
  text: string,
  sessionId: string | null | undefined,
): ReactNode {
  const needle = sessionId?.trim();
  if (!needle) {
    return text;
  }
  const lowerText = text.toLowerCase();
  const lowerNeedle = needle.toLowerCase();
  const needleLen = needle.length;
  let pos = 0;
  let matchIdx = lowerText.indexOf(lowerNeedle, pos);
  if (matchIdx === -1) {
    return text;
  }
  const parts: ReactNode[] = [];
  let keyIdx = 0;
  while (matchIdx !== -1) {
    if (matchIdx > pos) {
      parts.push(text.slice(pos, matchIdx));
    }
    parts.push(
      <mark
        key={`sid-${keyIdx++}`}
        className="rounded bg-emerald-500/30 px-0.5 text-emerald-200 ring-1 ring-emerald-400/40"
      >
        {text.slice(matchIdx, matchIdx + needleLen)}
      </mark>,
    );
    pos = matchIdx + needleLen;
    matchIdx = lowerText.indexOf(lowerNeedle, pos);
  }
  if (pos < text.length) {
    parts.push(text.slice(pos));
  }
  return parts;
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) {
    return "-";
  }
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function getDigestEntryLine(entry: LogDigestErrorEntry): number | null {
  if (typeof entry.line_number === "number" && entry.line_number >= 1) {
    return entry.line_number;
  }
  if (typeof entry.line === "number" && entry.line >= 1) {
    return entry.line;
  }
  return null;
}

export function getLineTextColorClass(line: LogLine): string {
  const level = (line.level ?? "").toUpperCase();
  if (level === "FATAL" || level === "ASSERT" || level === "ERROR") {
    return "text-rose-300";
  }
  if (level === "WARN" || level === "WARNING") {
    return "text-amber-300";
  }
  if (level === "DEBUG" || level === "TRACE") {
    return "text-slate-400";
  }
  return "text-slate-200";
}

export function getDigestLevelBadgeClass(
  level: string | null | undefined,
): string {
  const upper = (level ?? "ERROR").toUpperCase();
  if (upper === "FATAL" || upper === "ASSERT") {
    return "bg-rose-500/20 text-rose-300 border-rose-500/30";
  }
  if (upper === "WARN" || upper === "WARNING") {
    return "bg-amber-500/20 text-amber-300 border-amber-500/30";
  }
  return "bg-red-500/20 text-red-300 border-red-500/30";
}

export function findClosestLineToTimestamp(
  lines: LogLine[],
  centerTimestamp: string,
  startLine: number,
  endLine: number,
): number {
  const centerMs = Date.parse(centerTimestamp);
  let bestLine = Math.round((startLine + endLine) / 2);
  if (Number.isNaN(centerMs)) {
    return bestLine;
  }
  let minDiff = Number.POSITIVE_INFINITY;
  for (const line of lines) {
    if (!line.timestamp) {
      continue;
    }
    const lineMs = Date.parse(line.timestamp);
    if (!Number.isNaN(lineMs)) {
      const diff = Math.abs(lineMs - centerMs);
      if (diff < minDiff) {
        minDiff = diff;
        bestLine = line.line_number;
      }
    }
  }
  return bestLine;
}
