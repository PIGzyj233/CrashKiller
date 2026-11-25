import type { SignatureFrameItem } from "../../api/types";

export const PAGE_SIZE = 25;

export function formatSignatureFrames(
  frames: SignatureFrameItem[],
  fallbackText?: string | null,
): string {
  if (!Array.isArray(frames) || frames.length === 0) {
    return fallbackText || "—";
  }
  const formatted = frames.slice(0, 4).map((frame) => {
    if (typeof frame === "string") {
      return frame;
    }
    const mod = frame.module ? `${frame.module}!` : "";
    const fn = frame.function || "<unknown>";
    return `${mod}${fn}`;
  });
  return formatted.join(" ← ");
}

export function parsePatternLines(raw: string): string[] {
  return raw
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
}
