import { Link } from "react-router-dom";
import { Link2 } from "lucide-react";
import type { CrashSummary } from "../../api/types";
import { useAppI18n } from "../../i18n";

interface RelatedSessionCrashesCardProps {
  sessionId: string;
  sessionLabel: string;
  relatedCrashes: CrashSummary[];
  relatedCount: number;
}

export function RelatedSessionCrashesCard({
  sessionId,
  sessionLabel,
  relatedCrashes,
  relatedCount,
}: RelatedSessionCrashesCardProps) {
  const { t, formatDateTime, formatCrashStatus, formatVerdict } = useAppI18n();

  if (relatedCrashes.length === 0) {
    return null;
  }

  const effectiveCount = relatedCount || relatedCrashes.length;

  return (
    <div className="rounded-xl border border-emerald-200 bg-white p-4 shadow-sm">
      <div className="mb-2.5 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-semibold text-slate-900">
          <Link2 className="h-4 w-4 text-emerald-600" />
          <span>
            {t("crashDetail.relatedSessionTitle", {
              label: sessionLabel,
              id: sessionId,
              count: effectiveCount,
            })}
          </span>
        </div>
        <Link
          to={`/crashes?session_id=${encodeURIComponent(sessionId)}`}
          className="text-xs font-medium text-indigo-600 hover:underline"
        >
          {t("crashDetail.relatedSessionViewAll", {
            count: effectiveCount + 1,
          })}
        </Link>
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-full divide-y divide-slate-200 text-left text-xs">
          <thead>
            <tr className="text-slate-500">
              <th className="pb-1.5 pr-3 font-medium">
                {t("crashes.colCrashId")}
              </th>
              <th className="pb-1.5 pr-3 font-medium">
                {t("crashes.colReasonIssue")}
              </th>
              <th className="pb-1.5 pr-3 font-medium">
                {t("crashes.colVersionOs")}
              </th>
              <th className="pb-1.5 pr-3 font-medium">
                {t("crashes.colStatus")}
              </th>
              <th className="pb-1.5 font-medium">
                {t("crashes.colCrashTime")}
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {relatedCrashes.map((rel: CrashSummary) => (
              <tr key={rel.id} className="hover:bg-slate-50">
                <td className="py-1.5 pr-3">
                  <Link
                    to={`/crashes/${encodeURIComponent(rel.id)}`}
                    className="font-mono font-semibold text-indigo-600 hover:underline"
                  >
                    {rel.id.slice(0, 8)}
                  </Link>
                </td>
                <td className="max-w-xs truncate py-1.5 pr-3 font-mono text-slate-700">
                  {rel.crash_reason || t("crashes.pendingSymbolication")}
                </td>
                <td className="py-1.5 pr-3 font-mono text-slate-600">
                  v{rel.app_version}
                </td>
                <td className="py-1.5 pr-3">
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] font-medium text-slate-700">
                    {formatCrashStatus(rel.status)}
                  </span>
                  {rel.ai_verdict && (
                    <span className="ml-1 rounded bg-indigo-50 px-1.5 py-0.5 text-[11px] text-indigo-700">
                      {formatVerdict(rel.ai_verdict)}
                    </span>
                  )}
                </td>
                <td className="whitespace-nowrap py-1.5 font-mono text-slate-500">
                  {formatDateTime(rel.crash_time || rel.created_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
