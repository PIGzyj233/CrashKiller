import { Link } from "react-router-dom";
import {
  Bug,
  CheckCircle2,
  Clock,
  FileWarning,
  Layers,
} from "lucide-react";
import type { getDashboard } from "../../api/client";
import { useAppI18n } from "../../i18n";

type DashboardData = Awaited<ReturnType<typeof getDashboard>>;

interface DashboardKpiGridProps {
  kpis: DashboardData["kpis"];
}

export function DashboardKpiGrid({ kpis }: DashboardKpiGridProps) {
  const { t, formatNumber } = useAppI18n();

  return (
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
          {formatNumber(kpis.total_crashes)}
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
          {formatNumber(kpis.analyzed_crashes)}
        </div>
        <div className="mt-1 text-xs text-slate-500">
          {typeof kpis.failed_crashes === "number" && kpis.failed_crashes > 0
            ? t("dashboard.kpiFailedCount", {
                value: formatNumber(kpis.failed_crashes),
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
          {formatNumber(kpis.pending_crashes)}
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
          {formatNumber(kpis.open_issues)}
        </div>
        <div className="mt-1 text-xs text-slate-500">
          {typeof kpis.total_issues === "number"
            ? t("dashboard.kpiTotalTrackedIssues", {
                value: formatNumber(kpis.total_issues),
              })
            : t("dashboard.kpiOpenStatuses")}
        </div>
      </Link>

      <Link
        to="/symbols?tab=missing"
        className={`group rounded-xl border p-4 shadow-sm transition hover:shadow ${
          kpis.missing_symbols_count > 0
            ? "border-amber-200 bg-amber-50/40 hover:border-amber-300"
            : "border-slate-200 bg-white hover:border-indigo-300"
        }`}
      >
        <div className="flex items-center justify-between text-xs font-medium text-slate-500">
          <span>{t("dashboard.kpiMissingSymbols")}</span>
          <FileWarning
            className={`h-4 w-4 ${
              kpis.missing_symbols_count > 0
                ? "text-amber-600"
                : "text-slate-400"
            }`}
          />
        </div>
        <div
          className={`mt-2 font-mono text-2xl font-bold ${
            kpis.missing_symbols_count > 0
              ? "text-amber-700"
              : "text-slate-900"
          }`}
        >
          {formatNumber(kpis.missing_symbols_count)}
        </div>
        <div className="mt-1 text-xs text-slate-500 group-hover:text-amber-700">
          {typeof kpis.total_symbols === "number"
            ? t("dashboard.kpiIndexedSymbols", {
                value: formatNumber(kpis.total_symbols),
              })
            : t("dashboard.kpiViewMissingQueue")}
        </div>
      </Link>
    </div>
  );
}
