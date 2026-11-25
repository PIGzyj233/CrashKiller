import type { StoragePartitionUsage, StorageReport } from "../../api/types";

export const GIB = 1024 ** 3;
export const DAY_OPTIONS = [7, 14, 30] as const;
export type DaysOption = (typeof DAY_OPTIONS)[number];

const PARTITION_ORDER: readonly string[] = [
  "logs",
  "dumps",
  "symbols/sym",
  "symbols/raw",
  "symbols/public_cache",
  "db",
  "staging",
];

export const VERDICT_COLORS: Record<string, string> = {
  KNOWN_ISSUE: "#10b981",
  NEW_ISSUE: "#0ea5e9",
  REGRESSION: "#f43f5e",
  NEEDS_REVIEW: "#f59e0b",
};

export const ISSUE_STATUS_COLORS: Record<string, string> = {
  NEW: "#0ea5e9",
  KNOWN: "#6366f1",
  FIXED: "#10b981",
  WONTFIX: "#94a3b8",
  REGRESSION: "#f43f5e",
};

export interface NormalizedPartition extends StoragePartitionUsage {
  key: string;
  label: string;
  usedGb: number;
  quotaGb: number;
  pct: number;
}

export interface NormalizedStorageSummary {
  storage: StorageReport;
  floorGb: number;
  isBreached: boolean;
  isNearFloor: boolean;
  totalUsedGb: number;
  totalQuotaGb: number;
  totalPct: number;
  partitions: NormalizedPartition[];
}

export function normalizePartitions(
  partitions: StorageReport["partitions"],
  getPartitionLabel: (key: string) => string,
): NormalizedPartition[] {
  const rawList: Array<{ key: string; usage: StoragePartitionUsage }> =
    Array.isArray(partitions)
      ? partitions.map((usage, idx) => ({
          key: usage.name ?? `partition-${idx}`,
          usage,
        }))
      : Object.entries(partitions).map(([key, usage]) => ({ key, usage }));

  return rawList
    .map(({ key, usage }) => {
      const usedGb = usage.used_gb ?? usage.used_bytes / GIB;
      const quotaGb = usage.quota_gb ?? usage.quota_bytes / GIB;
      const computedPct =
        usage.quota_bytes > 0
          ? (usage.used_bytes / usage.quota_bytes) * 100
          : 0;
      const rawPct = Number.isFinite(usage.usage_percent)
        ? usage.usage_percent
        : computedPct;
      return {
        ...usage,
        key,
        label: getPartitionLabel(key),
        usedGb,
        quotaGb,
        pct: Math.min(100, Math.max(0, rawPct)),
      };
    })
    .sort((a, b) => {
      const idxA = PARTITION_ORDER.indexOf(a.key);
      const idxB = PARTITION_ORDER.indexOf(b.key);
      const rankA = idxA === -1 ? 999 : idxA;
      const rankB = idxB === -1 ? 999 : idxB;
      if (rankA !== rankB) {
        return rankA - rankB;
      }
      return a.key.localeCompare(b.key);
    });
}

export function normalizeStorageSummary(
  storage: StorageReport,
  getPartitionLabel: (key: string) => string,
): NormalizedStorageSummary {
  const floorGb = storage.host_safety_floor_gb ?? 50;
  const isBreached = storage.safety_floor_breached === true;
  const isNearFloor =
    !isBreached && floorGb > 0 && storage.host_free_gb <= floorGb * 1.25;

  const totalUsedGb = storage.total_used_gb ?? storage.total_used_bytes / GIB;
  const totalQuotaGb =
    storage.total_quota_gb ?? storage.total_quota_bytes / GIB;
  const rawTotalPct =
    storage.total_usage_percent ??
    (storage.total_quota_bytes > 0
      ? (storage.total_used_bytes / storage.total_quota_bytes) * 100
      : 0);

  return {
    storage,
    floorGb,
    isBreached,
    isNearFloor,
    totalUsedGb,
    totalQuotaGb,
    totalPct: Math.min(100, Math.max(0, rawTotalPct)),
    partitions: normalizePartitions(storage.partitions, getPartitionLabel),
  };
}

export function formatGb(gb: number): string {
  if (!Number.isFinite(gb) || gb < 0) {
    return "0.00 GB";
  }
  if (gb >= 100) {
    return `${gb.toFixed(1)} GB`;
  }
  return `${gb.toFixed(2)} GB`;
}

export function formatTooltipNumber(
  value: number | string | Array<number | string> | undefined,
  formatNum: (n: number) => string,
): string {
  if (typeof value === "number") {
    return formatNum(value);
  }
  if (Array.isArray(value)) {
    return value.join(", ");
  }
  return String(value ?? "");
}

export function getBarColorClass(pct: number): string {
  if (pct >= 85) {
    return "bg-rose-500";
  }
  if (pct >= 70) {
    return "bg-amber-500";
  }
  return "bg-indigo-600";
}
