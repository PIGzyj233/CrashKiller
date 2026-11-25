import type { SignatureFrameItem } from "../../api/types";

export function formatSignatureFrames(
  frames: SignatureFrameItem[],
  fallbackText?: string | null,
): string {
  if (!Array.isArray(frames) || frames.length === 0) {
    return fallbackText || "—";
  }
  return frames
    .slice(0, 5)
    .map((f) =>
      typeof f === "string"
        ? f
        : `${f.module ? `${f.module}!` : ""}${f.function || "<unknown>"}`,
    )
    .join(" ← ");
}

export function formatConfidence(confidence: number | null): string | null {
  if (confidence === null || !Number.isFinite(confidence)) {
    return null;
  }
  const pct = confidence <= 1 ? confidence * 100 : confidence;
  return `${Math.round(pct)}%`;
}

export function isValidRegex(pattern: string): boolean {
  try {
    new RegExp(pattern);
    return true;
  } catch {
    return false;
  }
}
