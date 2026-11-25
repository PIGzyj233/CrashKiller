import { ApiError } from "../../api/client";
import type { ProjectRead } from "../../api/types";
import { i18n } from "../../i18n";

export function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) {
    return "-";
  }
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

export function resolveInitialProjectKey(
  projects: ProjectRead[],
  defaultProjectKey?: string,
): string {
  if (defaultProjectKey) {
    if (
      projects.length === 0 ||
      projects.some((p) => p.key === defaultProjectKey)
    ) {
      return defaultProjectKey;
    }
  }
  return projects[0]?.key ?? "";
}

export function formatUploadError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401 || err.status === 403) {
      return i18n.t("upload.errorAuth", { message: err.message });
    }
    if (err.status === 413) {
      return i18n.t("upload.errorTooLarge", { message: err.message });
    }
    if (err.status === 507) {
      return i18n.t("upload.errorDiskFull", { message: err.message });
    }
    return err.message;
  }
  if (err instanceof Error) {
    return err.message;
  }
  return i18n.t("upload.errorUnexpected");
}

export function normalizeCrashTime(raw: string): string | undefined {
  const trimmed = raw.trim();
  if (!trimmed) {
    return undefined;
  }
  const parsed = new Date(trimmed);
  if (!Number.isNaN(parsed.getTime())) {
    return parsed.toISOString();
  }
  return trimmed;
}

export function getFileDedupKey(file: File): string {
  return `${file.name}:${file.size}:${file.lastModified}`;
}

export function mergeUniqueFiles(existing: File[], incoming: File[]): File[] {
  const seen = new Set(existing.map(getFileDedupKey));
  const merged = [...existing];
  for (const file of incoming) {
    const key = getFileDedupKey(file);
    if (!seen.has(key)) {
      seen.add(key);
      merged.push(file);
    }
  }
  return merged;
}

export function isBlockedLogFile(filename: string): boolean {
  return /\.(dmp|pdb|exe|dll)$/i.test(filename);
}
