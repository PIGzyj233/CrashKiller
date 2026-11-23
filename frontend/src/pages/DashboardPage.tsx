import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  AlertOctagon,
  AlertTriangle,
  ArrowRight,
  Bug,
  CheckCircle2,
  Clock,
  FileWarning,
  HardDrive,
  Layers,
  LayoutDashboard,
  RefreshCw,
} from "lucide-react";
import { ApiError, getDashboard } from "../api/client";
import type {
  StoragePartitionUsage,
  StorageReport,
} from "../api/types";
import { useProjectContext } from "../components/Layout";
import { useAppI18n } from "../i18n";

const GIB = 1024 ** 3;
const DAY_OPTIONS = [7, 14, 30] as const;
type DaysOption = (typeof DAY_OPTIONS)[number];

const PARTITION_ORDER: readonly string[] = [
  "logs",
  "dumps",
  "symbols/sym",
  "symbols/raw",
  "symbols/public_cache",
  "db",
  "staging",
];

const VERDICT_COLORS: Record<string, string> = {
  KNOWN_ISSUE: "#10b981",
  NEW_ISSUE: "#0ea5e9",
  REGRESSION: "#f43f5e",
  NEEDS_REVIEW: "#f59e0b",
};

const ISSUE_STATUS_COLORS: Record<string, string> = {
  NEW: "#0ea5e9",
  KNOWN: "#6366f1",
  FIXED: "#10b981",
  WONTFIX: "#94a3b8",
  REGRESSION: "#f43f5e",
};

interface NormalizedPartition extends StoragePartitionUsage {
  key: string;
  label: string;
  usedGb: number;
  quotaGb: number;
  pct: number;
}

function normalizePartitions(
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

function formatGb(gb: number): string {
  if (!Number.isFinite(gb) || gb < 0) {
    return "0.00 GB";
  }
  if (gb >= 100) {
    return `${gb.toFixed(1)} GB`;
  }
  return `${gb.toFixed(2)} GB`;
}

function formatTooltipNumber(
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

function getBarColorClass(pct: number): string {
  if (pct >= 85) {
    return "bg-rose-500";
  }
  if (pct >= 70) {
    return "bg-amber-500";
  }
  return "bg-indigo-600";
}

export function DashboardPage() {
  const {
    t,
    formatDateTime,
    formatNumber,
    formatVerdict,
    formatIssueStatus,
  } = useAppI18n();
  const { projects, selectedProject, selectedProjectId, isLoadingProjects } =
    useProjectContext();
  const [days, setDays] = useState<DaysOption>(14);

  const dashboardQuery = useQuery({
    queryKey: ["dashboard", selectedProjectId || "all", days],
    queryFn: () =>
      getDashboard({
        project_id: selectedProjectId || undefined,
        days,
      }),
  });

  const scopeLabel = selectedProject
    ? `${selectedProject.name} (${selectedProject.key})`
    : selectedProjectId || t("nav.allProjects");

  const data = dashboardQuery.data;

  const verdictData = useMemo(
    () =>
      (data?.verdict_distribution ?? [])
        .filter((item) => item.count > 0)
        .map((item) => ({
          ...item,
          displayLabel: formatVerdict(item.verdict),
        })),
    [data?.verdict_distribution, formatVerdict],
  );

  const issueStatusData = useMemo(
    () =>
      (data?.issue_status_distribution ?? []).map((item) => ({
        ...item,
        displayLabel: formatIssueStatus(item.status),
      })),
    [data?.issue_status_distribution, formatIssueStatus],
  );

  const hasAnalyzedSeries = useMemo(
    () =>
      (data?.crash_trend ?? []).some(
        (point) => typeof point.analyzed === "number",
      ),
    [data?.crash_trend],
  );

  const normalizedStorage = useMemo(() => {
    if (!data?.storage) {
      return null;
    }
    const storage = data.storage;
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

    const getPartitionLabel = (key: string): string => {
      switch (key) {
        case "logs":
          return t("dashboard.partitions.logs");
        case "dumps":
          return t("dashboard.partitions.dumps");
        case "symbols/sym":
          return t("dashboard.partitions.symbols/sym");
        case "symbols/raw":
          return t("dashboard.partitions.symbols/raw");
        case "symbols/public_cache":
          return t("dashboard.partitions.symbols/public_cache");
        case "db":
          return t("dashboard.partitions.db");
        case "staging":
          return t("dashboard.partitions.staging");
        default:
          return key;
      }
    };

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
  }, [data?.storage, t]);

  const errorMessage =
    dashboardQuery.error instanceof ApiError
      ? dashboardQuery.error.message
      : dashboardQuery.error instanceof Error
        ? dashboardQuery.error.message
        : t("dashboard.loadFailed");

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-indigo-50 text-indigo-600">
            <LayoutDashboard className="h-5 w-5" />
          </div>
          <div>
            <h1 className="text-xl font-semibold text-slate-900">
              {t("dashboard.title")}
            </h1>
            <p className="text-sm text-slate-500">{t("dashboard.subtitle")}</p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2.5">
          <span className="rounded-full border border-slate-200 bg-white px-3 py-1 text-xs font-medium text-slate-700">
            {t("dashboard.scope", { scope: scopeLabel })}
          </span>
          <span className="rounded-full border border-slate-200 bg-white px-3 py-1 text-xs text-slate-600">
            {isLoadingProjects
              ? t("dashboard.loadingProjects")
              : t("dashboard.projectsCount", { count: projects.length })}
          </span>

          <div
            className="inline-flex rounded-lg border border-slate-200 bg-white p-0.5 shadow-sm"
            role="group"
            aria-label={t("dashboard.trendWindowAria")}
          >
            {DAY_OPTIONS.map((opt) => (
              <button
                key={opt}
                type="button"
                onClick={() => setDays(opt)}
                className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                  days === opt
                    ? "bg-indigo-600 text-white"
                    : "text-slate-600 hover:bg-slate-100 hover:text-slate-900"
                }`}
              >
                {t("dashboard.daysLabel", { days: opt })}
              </button>
            ))}
          </div>

          <button
            type="button"
            onClick={() => void dashboardQuery.refetch()}
            disabled={dashboardQuery.isFetching}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 shadow-sm hover:bg-slate-50 disabled:opacity-60"
          >
            <RefreshCw
              className={`h-3.5 w-3.5 ${
                dashboardQuery.isFetching ? "animate-spin" : ""
              }`}
            />
            {t("common.refresh")}
          </button>
        </div>
      </div>

      {/* Error State */}
      {dashboardQuery.isError && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          <div className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 shrink-0 text-rose-600" />
            <span>{errorMessage}</span>
          </div>
          <button
            type="button"
            onClick={() => void dashboardQuery.refetch()}
            className="rounded-md border border-rose-300 bg-white px-3 py-1 text-xs font-medium text-rose-700 hover:bg-rose-100"
          >
            {t("common.retry")}
          </button>
        </div>
      )}

      {/* Loading Skeleton */}
      {dashboardQuery.isPending && !data && (
        <div className="space-y-6">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
            {Array.from({ length: 5 }).map((_, idx) => (
              <div
                key={idx}
                className="h-24 animate-pulse rounded-xl border border-slate-200 bg-white p-4"
              >
                <div className="h-3 w-24 rounded bg-slate-200" />
                <div className="mt-4 h-7 w-16 rounded bg-slate-200" />
              </div>
            ))}
          </div>
          <div className="grid gap-6 lg:grid-cols-3">
            <div className="h-72 animate-pulse rounded-xl border border-slate-200 bg-white lg:col-span-2" />
            <div className="h-72 animate-pulse rounded-xl border border-slate-200 bg-white" />
          </div>
        </div>
      )}

      {data && (
        <>
          {/* 50GB Host Safety Floor Alert Banner */}
          {normalizedStorage &&
            (normalizedStorage.isBreached || normalizedStorage.isNearFloor) && (
              <div
                role="alert"
                className={`flex flex-wrap items-center justify-between gap-3 rounded-xl border px-4 py-3.5 text-sm shadow-sm ${
                  normalizedStorage.isBreached
                    ? "border-rose-300 bg-rose-50 text-rose-900"
                    : "border-amber-300 bg-amber-50 text-amber-900"
                }`}
              >
                <div className="flex items-start gap-3">
                  {normalizedStorage.isBreached ? (
                    <AlertOctagon className="mt-0.5 h-5 w-5 shrink-0 text-rose-600" />
                  ) : (
                    <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-amber-600" />
                  )}
                  <div>
                    <div className="font-semibold">
                      {normalizedStorage.isBreached
                        ? t("dashboard.floorBreachedTitle", {
                            free: normalizedStorage.storage.host_free_gb.toFixed(1),
                            floor: normalizedStorage.floorGb,
                          })
                        : t("dashboard.floorNearTitle", {
                            free: normalizedStorage.storage.host_free_gb.toFixed(1),
                            floor: normalizedStorage.floorGb,
                          })}
                    </div>
                    <p className="mt-0.5 text-xs opacity-90">
                      {normalizedStorage.isBreached
                        ? t("dashboard.floorBreachedDesc")
                        : t("dashboard.floorNearDesc")}
                    </p>
                  </div>
                </div>
                <Link
                  to="/admin"
                  className={`inline-flex items-center gap-1 rounded-md px-3 py-1.5 text-xs font-medium shadow-sm ${
                    normalizedStorage.isBreached
                      ? "bg-rose-600 text-white hover:bg-rose-700"
                      : "bg-amber-600 text-white hover:bg-amber-700"
                  }`}
                >
                  {t("dashboard.manageStorageGc")}
                  <ArrowRight className="h-3.5 w-3.5" />
                </Link>
              </div>
            )}

          {/* 5 KPI Cards */}
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
            <Link
              to="/crashes"
              className="group rounded-xl border border-slate-200 bg-white p-4 shadow-sm transition hover:border-indigo-300 hover:shadow"
            >
              <div className="flex items-center justify-between text-xs font-medium text-slate-500">
                <span>{t("dashboard.kpiTotalCrashes")}</span>
                <Bug className="h-4 w-4 text-indigo-600" />
              </div>
              <div className="mt-2 font-mono text-2xl font-bold text-slate-900">
                {formatNumber(data.kpis.total_crashes)}
              </div>
              <div className="mt-1 text-xs text-slate-500 group-hover:text-indigo-600">
                {t("dashboard.kpiViewAllCrashes")}
              </div>
            </Link>

            <Link
              to="/crashes?status=ANALYZED"
              className="group rounded-xl border border-slate-200 bg-white p-4 shadow-sm transition hover:border-emerald-300 hover:shadow"
            >
              <div className="flex items-center justify-between text-xs font-medium text-slate-500">
                <span>{t("dashboard.kpiAnalyzed")}</span>
                <CheckCircle2 className="h-4 w-4 text-emerald-600" />
              </div>
              <div className="mt-2 font-mono text-2xl font-bold text-slate-900">
                {formatNumber(data.kpis.analyzed_crashes)}
              </div>
              <div className="mt-1 text-xs text-slate-500">
                {typeof data.kpis.failed_crashes === "number" &&
                data.kpis.failed_crashes > 0
                  ? t("dashboard.kpiFailedCount", {
                      value: formatNumber(data.kpis.failed_crashes),
                    })
                  : t("dashboard.kpiCompletedPipeline")}
              </div>
            </Link>

            <Link
              to="/crashes?status=PENDING"
              className="group rounded-xl border border-slate-200 bg-white p-4 shadow-sm transition hover:border-sky-300 hover:shadow"
            >
              <div className="flex items-center justify-between text-xs font-medium text-slate-500">
                <span>{t("dashboard.kpiPendingActive")}</span>
                <Clock className="h-4 w-4 text-sky-600" />
              </div>
              <div className="mt-2 font-mono text-2xl font-bold text-slate-900">
                {formatNumber(data.kpis.pending_crashes)}
              </div>
              <div className="mt-1 text-xs text-slate-500 group-hover:text-sky-600">
                {t("dashboard.kpiPendingDesc")}
              </div>
            </Link>

            <Link
              to="/issues"
              className="group rounded-xl border border-slate-200 bg-white p-4 shadow-sm transition hover:border-indigo-300 hover:shadow"
            >
              <div className="flex items-center justify-between text-xs font-medium text-slate-500">
                <span>{t("dashboard.kpiOpenIssues")}</span>
                <Layers className="h-4 w-4 text-indigo-600" />
              </div>
              <div className="mt-2 font-mono text-2xl font-bold text-slate-900">
                {formatNumber(data.kpis.open_issues)}
              </div>
              <div className="mt-1 text-xs text-slate-500">
                {typeof data.kpis.total_issues === "number"
                  ? t("dashboard.kpiTotalTrackedIssues", {
                      value: formatNumber(data.kpis.total_issues),
                    })
                  : t("dashboard.kpiOpenStatuses")}
              </div>
            </Link>

            <Link
              to="/symbols?tab=missing"
              className={`group rounded-xl border p-4 shadow-sm transition hover:shadow ${
                data.kpis.missing_symbols_count > 0
                  ? "border-amber-200 bg-amber-50/40 hover:border-amber-300"
                  : "border-slate-200 bg-white hover:border-indigo-300"
              }`}
            >
              <div className="flex items-center justify-between text-xs font-medium text-slate-500">
                <span>{t("dashboard.kpiMissingSymbols")}</span>
                <FileWarning
                  className={`h-4 w-4 ${
                    data.kpis.missing_symbols_count > 0
                      ? "text-amber-600"
                      : "text-slate-400"
                  }`}
                />
              </div>
              <div
                className={`mt-2 font-mono text-2xl font-bold ${
                  data.kpis.missing_symbols_count > 0
                    ? "text-amber-700"
                    : "text-slate-900"
                }`}
              >
                {formatNumber(data.kpis.missing_symbols_count)}
              </div>
              <div className="mt-1 text-xs text-slate-500 group-hover:text-amber-700">
                {typeof data.kpis.total_symbols === "number"
                  ? t("dashboard.kpiIndexedSymbols", {
                      value: formatNumber(data.kpis.total_symbols),
                    })
                  : t("dashboard.kpiViewMissingQueue")}
              </div>
            </Link>
          </div>

          {/* Charts Row 1: Crash Trend + AI Verdict Distribution */}
          <div className="grid gap-6 lg:grid-cols-3">
            <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm lg:col-span-2">
              <div className="mb-4 flex items-center justify-between">
                <div>
                  <h2 className="text-sm font-semibold text-slate-900">
                    {t("dashboard.trendTitle", { days })}
                  </h2>
                  <p className="text-xs text-slate-500">
                    {t("dashboard.trendSubtitle")}
                  </p>
                </div>
              </div>

              {data.crash_trend.length === 0 ? (
                <div className="flex h-[260px] items-center justify-center rounded-lg border border-dashed border-slate-200 text-sm text-slate-500">
                  {t("dashboard.trendEmpty", { days })}
                </div>
              ) : (
                <div className="h-[260px] w-full">
                  <ResponsiveContainer width="100%" height={260}>
                    <AreaChart
                      data={data.crash_trend}
                      margin={{ top: 10, right: 16, left: 0, bottom: 0 }}
                    >
                      <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                      <XAxis
                        dataKey="date"
                        tick={{ fontSize: 12, fill: "#64748b" }}
                        tickFormatter={(v) => String(v).slice(5)}
                      />
                      <YAxis
                        allowDecimals={false}
                        tick={{ fontSize: 12, fill: "#64748b" }}
                        width={40}
                      />
                      <Tooltip
                        formatter={(value, name) => [
                          formatTooltipNumber(value, formatNumber),
                          String(name ?? ""),
                        ]}
                      />
                      <Legend />
                      <Area
                        type="monotone"
                        dataKey="count"
                        name={t("dashboard.seriesTotalCrashes")}
                        stroke="#4f46e5"
                        fill="#4f46e5"
                        fillOpacity={0.14}
                        strokeWidth={2}
                      />
                      {hasAnalyzedSeries && (
                        <Area
                          type="monotone"
                          dataKey="analyzed"
                          name={t("dashboard.seriesAnalyzed")}
                          stroke="#10b981"
                          fill="#10b981"
                          fillOpacity={0.1}
                          strokeWidth={2}
                        />
                      )}
                    </AreaChart>
                  </ResponsiveContainer>
                </div>
              )}
            </div>

            <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
              <div className="mb-4">
                <h2 className="text-sm font-semibold text-slate-900">
                  {t("dashboard.verdictTitle")}
                </h2>
                <p className="text-xs text-slate-500">
                  {t("dashboard.verdictSubtitle")}
                </p>
              </div>

              {verdictData.length === 0 ? (
                <div className="flex h-[260px] items-center justify-center rounded-lg border border-dashed border-slate-200 text-sm text-slate-500">
                  {t("dashboard.verdictEmpty")}
                </div>
              ) : (
                <div className="h-[260px] w-full">
                  <ResponsiveContainer width="100%" height={260}>
                    <PieChart>
                      <Pie
                        data={verdictData}
                        dataKey="count"
                        nameKey="displayLabel"
                        cx="50%"
                        cy="46%"
                        innerRadius={52}
                        outerRadius={82}
                        paddingAngle={2}
                      >
                        {verdictData.map((entry) => (
                          <Cell
                            key={entry.verdict}
                            fill={VERDICT_COLORS[entry.verdict] ?? "#94a3b8"}
                          />
                        ))}
                      </Pie>
                      <Tooltip
                        formatter={(value, name) => [
                          formatTooltipNumber(value, formatNumber),
                          String(name ?? ""),
                        ]}
                      />
                      <Legend />
                    </PieChart>
                  </ResponsiveContainer>
                </div>
              )}
            </div>
          </div>

          {/* Charts Row 2: Issue Status Distribution + Missing Symbols Alert Table */}
          <div className="grid gap-6 lg:grid-cols-5">
            <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm lg:col-span-2">
              <div className="mb-4 flex items-center justify-between">
                <div>
                  <h2 className="text-sm font-semibold text-slate-900">
                    {t("dashboard.issueStatusTitle")}
                  </h2>
                  <p className="text-xs text-slate-500">
                    {t("dashboard.issueStatusSubtitle")}
                  </p>
                </div>
                <Link
                  to="/issues"
                  className="text-xs font-medium text-indigo-600 hover:underline"
                >
                  {t("dashboard.allIssuesLink")}
                </Link>
              </div>

              {issueStatusData.length === 0 ||
              issueStatusData.every((item) => item.count === 0) ? (
                <div className="flex h-[240px] items-center justify-center rounded-lg border border-dashed border-slate-200 text-sm text-slate-500">
                  {t("dashboard.issueStatusEmpty")}
                </div>
              ) : (
                <div className="h-[240px] w-full">
                  <ResponsiveContainer width="100%" height={240}>
                    <BarChart
                      data={issueStatusData}
                      margin={{ top: 10, right: 16, left: 0, bottom: 0 }}
                    >
                      <CartesianGrid
                        strokeDasharray="3 3"
                        stroke="#e2e8f0"
                        vertical={false}
                      />
                      <XAxis
                        dataKey="displayLabel"
                        tick={{ fontSize: 11, fill: "#64748b" }}
                      />
                      <YAxis
                        allowDecimals={false}
                        tick={{ fontSize: 12, fill: "#64748b" }}
                        width={40}
                      />
                      <Tooltip
                        formatter={(value) => [
                          formatTooltipNumber(value, formatNumber),
                          t("dashboard.issuesTooltipLabel"),
                        ]}
                      />
                      <Bar dataKey="count" radius={[4, 4, 0, 0]}>
                        {issueStatusData.map((entry) => (
                          <Cell
                            key={entry.status}
                            fill={
                              ISSUE_STATUS_COLORS[entry.status] ?? "#6366f1"
                            }
                          />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              )}
            </div>

            <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm lg:col-span-3">
              <div className="mb-4 flex items-center justify-between gap-2">
                <div>
                  <h2 className="text-sm font-semibold text-slate-900">
                    {t("dashboard.missingSymbolsTitle")}
                  </h2>
                  <p className="text-xs text-slate-500">
                    {t("dashboard.missingSymbolsSubtitle")}
                  </p>
                </div>
                <Link
                  to="/symbols?tab=missing"
                  className="inline-flex items-center gap-1 text-xs font-medium text-indigo-600 hover:underline"
                >
                  {t("dashboard.openMissingQueue")}
                  <ArrowRight className="h-3.5 w-3.5" />
                </Link>
              </div>

              {data.top_missing_symbols.length === 0 ? (
                <div className="flex h-[240px] flex-col items-center justify-center rounded-lg border border-dashed border-slate-200 text-center text-sm text-slate-500">
                  <CheckCircle2 className="mb-2 h-6 w-6 text-emerald-500" />
                  {t("dashboard.missingSymbolsEmpty")}
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="min-w-full divide-y divide-slate-200 text-left text-xs">
                    <thead>
                      <tr className="text-slate-500">
                        <th className="pb-2 pr-3 font-medium">
                          {t("dashboard.colModuleDebugFile")}
                        </th>
                        <th className="pb-2 pr-3 font-medium">
                          {t("dashboard.colDebugId")}
                        </th>
                        <th className="pb-2 pr-3 font-medium">
                          {t("dashboard.colCrashes")}
                        </th>
                        <th className="pb-2 pr-3 font-medium">
                          {t("dashboard.colLatestCrash")}
                        </th>
                        <th className="pb-2 text-right font-medium">
                          {t("dashboard.colAction")}
                        </th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {data.top_missing_symbols.map((sym) => (
                        <tr
                          key={`${sym.debug_file}-${sym.debug_id}`}
                          className="hover:bg-slate-50"
                        >
                          <td className="py-2.5 pr-3">
                            <div className="font-medium text-slate-900">
                              {sym.module_name || sym.debug_file}
                            </div>
                            <div className="font-mono text-[11px] text-slate-500">
                              {sym.debug_file}
                            </div>
                          </td>
                          <td className="py-2.5 pr-3 font-mono text-[11px] text-slate-700">
                            {sym.debug_id}
                          </td>
                          <td className="py-2.5 pr-3">
                            <span className="inline-flex items-center rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 font-mono text-[11px] font-medium text-amber-700">
                              {sym.crash_count}
                            </span>
                          </td>
                          <td className="py-2.5 pr-3 text-slate-600">
                            {formatDateTime(sym.latest_crash_at)}
                          </td>
                          <td className="py-2.5 text-right">
                            <Link
                              to="/symbols?tab=missing"
                              className="font-medium text-indigo-600 hover:underline"
                            >
                              {t("dashboard.resolveAction")}
                            </Link>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>

          {/* Storage Quotas & 150GB Hard Cap */}
          {normalizedStorage && (
            <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-2.5">
                  <HardDrive className="h-5 w-5 text-indigo-600" />
                  <div>
                    <h2 className="text-sm font-semibold text-slate-900">
                      {t("dashboard.storageTitle")}
                    </h2>
                    <p className="text-xs text-slate-500">
                      {t("dashboard.storageSubtitle")}
                    </p>
                  </div>
                </div>

                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <span
                    className={`rounded-full border px-2.5 py-1 font-medium ${
                      normalizedStorage.isBreached
                        ? "border-rose-200 bg-rose-50 text-rose-700"
                        : normalizedStorage.isNearFloor
                          ? "border-amber-200 bg-amber-50 text-amber-700"
                          : "border-emerald-200 bg-emerald-50 text-emerald-700"
                    }`}
                  >
                    {t("dashboard.hostFreeBadge", {
                      free: normalizedStorage.storage.host_free_gb.toFixed(1),
                      floor: normalizedStorage.floorGb,
                    })}
                  </span>
                  <Link
                    to="/admin"
                    className="rounded-md border border-slate-200 bg-white px-2.5 py-1 font-medium text-slate-700 hover:bg-slate-50"
                  >
                    {t("dashboard.quotaGcSettings")}
                  </Link>
                </div>
              </div>

              {/* Total Cap Bar */}
              <div className="mt-4 rounded-lg border border-slate-200 bg-slate-50 p-3.5">
                <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                  <span className="font-semibold text-slate-800">
                    {t("dashboard.totalDiskUsage")}
                  </span>
                  <span className="font-mono text-slate-700">
                    {formatGb(normalizedStorage.totalUsedGb)} /{" "}
                    {formatGb(normalizedStorage.totalQuotaGb)} (
                    {normalizedStorage.totalPct.toFixed(1)}%)
                  </span>
                </div>
                <div className="mt-2 h-2.5 w-full overflow-hidden rounded-full bg-slate-200">
                  <div
                    className={`h-full rounded-full transition-all ${getBarColorClass(
                      normalizedStorage.totalPct,
                    )}`}
                    style={{ width: `${normalizedStorage.totalPct}%` }}
                  />
                </div>
              </div>

              {/* Per-Partition Progress Bars */}
              <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
                {normalizedStorage.partitions.map((part) => (
                  <div
                    key={part.key}
                    className="rounded-lg border border-slate-200 p-3"
                  >
                    <div className="flex items-center justify-between gap-2 text-xs">
                      <span
                        className="truncate font-medium text-slate-800"
                        title={part.key}
                      >
                        {part.label}
                      </span>
                      <span className="shrink-0 font-mono text-[11px] font-semibold text-slate-700">
                        {part.pct.toFixed(1)}%
                      </span>
                    </div>
                    <div className="mt-1.5 h-2 w-full overflow-hidden rounded-full bg-slate-100">
                      <div
                        className={`h-full rounded-full ${getBarColorClass(
                          part.pct,
                        )}`}
                        style={{ width: `${part.pct}%` }}
                      />
                    </div>
                    <div className="mt-1.5 flex items-center justify-between font-mono text-[11px] text-slate-500">
                      <span>{formatGb(part.usedGb)}</span>
                      <span>
                        {t("dashboard.capLabel", {
                          quota: formatGb(part.quotaGb),
                        })}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default DashboardPage;
