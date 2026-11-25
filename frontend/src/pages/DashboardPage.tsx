import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, LayoutDashboard, RefreshCw } from "lucide-react";
import { ApiError, getDashboard } from "../api/client";
import { useProjectContext } from "../components/Layout";
import { useAppI18n } from "../i18n";
import { DashboardChartsSection } from "./dashboard/DashboardChartsSection";
import { DashboardKpiGrid } from "./dashboard/DashboardKpiGrid";
import {
  DAY_OPTIONS,
  type DaysOption,
  normalizeStorageSummary,
} from "./dashboard/helpers";
import {
  SafetyFloorBanner,
  StorageQuotaCard,
} from "./dashboard/StorageQuotaSection";

export function DashboardPage() {
  const { t, formatVerdict, formatIssueStatus } = useAppI18n();
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

    return normalizeStorageSummary(data.storage, getPartitionLabel);
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
          {normalizedStorage && (
            <SafetyFloorBanner normalizedStorage={normalizedStorage} />
          )}

          <DashboardKpiGrid kpis={data.kpis} />

          <DashboardChartsSection
            days={days}
            crashTrend={data.crash_trend}
            hasAnalyzedSeries={hasAnalyzedSeries}
            verdictData={verdictData}
            issueStatusData={issueStatusData}
            topMissingSymbols={data.top_missing_symbols}
          />

          {normalizedStorage && (
            <StorageQuotaCard normalizedStorage={normalizedStorage} />
          )}
        </>
      )}
    </div>
  );
}

export default DashboardPage;
