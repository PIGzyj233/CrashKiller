import { Link } from "react-router-dom";
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
import { ArrowRight, CheckCircle2 } from "lucide-react";
import type { getDashboard } from "../../api/client";
import { useAppI18n } from "../../i18n";
import {
  type DaysOption,
  formatTooltipNumber,
  ISSUE_STATUS_COLORS,
  VERDICT_COLORS,
} from "./helpers";

type DashboardData = Awaited<ReturnType<typeof getDashboard>>;
type VerdictItem = DashboardData["verdict_distribution"][number] & {
  displayLabel: string;
};
type IssueStatusItem = DashboardData["issue_status_distribution"][number] & {
  displayLabel: string;
};

interface DashboardChartsSectionProps {
  days: DaysOption;
  crashTrend: DashboardData["crash_trend"];
  hasAnalyzedSeries: boolean;
  verdictData: VerdictItem[];
  issueStatusData: IssueStatusItem[];
  topMissingSymbols: DashboardData["top_missing_symbols"];
}

export function DashboardChartsSection({
  days,
  crashTrend,
  hasAnalyzedSeries,
  verdictData,
  issueStatusData,
  topMissingSymbols,
}: DashboardChartsSectionProps) {
  const { t, formatDateTime, formatNumber } = useAppI18n();

  return (
    <>
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

          {crashTrend.length === 0 ? (
            <div className="flex h-[260px] items-center justify-center rounded-lg border border-dashed border-slate-200 text-sm text-slate-500">
              {t("dashboard.trendEmpty", { days })}
            </div>
          ) : (
            <div className="h-[260px] w-full">
              <ResponsiveContainer width="100%" height={260}>
                <AreaChart
                  data={crashTrend}
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
                        fill={ISSUE_STATUS_COLORS[entry.status] ?? "#6366f1"}
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

          {topMissingSymbols.length === 0 ? (
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
                  {topMissingSymbols.map((sym) => (
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
    </>
  );
}
