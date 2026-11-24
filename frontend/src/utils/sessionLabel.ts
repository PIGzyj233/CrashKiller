import type { ProjectRead } from "../api/types";

export function getSessionIdLabel(
  project: ProjectRead | null | undefined,
  fallback: string,
): string {
  const custom = project?.settings_json?.session_id_label;
  if (typeof custom === "string" && custom.trim()) {
    return custom.trim();
  }
  return fallback;
}
