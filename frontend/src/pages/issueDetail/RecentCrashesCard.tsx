import { Link } from "react-router-dom";
import type { CrashSummary } from "../../api/types";
import { useAppI18n } from "../../i18n";
import { formatConfidence } from "./utils";

interface RecentCrashesCardProps {
  issueId: number;
  recentCrashes: CrashSummary[];
}

export function RecentCrashesCard({
  issueId,
  recentCrashes,
}: RecentCrashesCardProps) {
  const { t, formatDateTime, formatCrashStatus, formatVerdict } = useAppI18n();

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-900">
          {t("issueDetail.recentCrashes", {
            count: recentCrashes.length,
          })}
        </h2>
        <Link
          to={`/crashes?issue_id=${issueId}`}
          className="text-xs font-medium text-indigo-600 hover:underline"
        >
          {t("issueDetail.viewAllInCrashes")}
        </Link>
      </div>

      {recentCrashes.length === 0 ? (
        <p className="text-xs text-slate-500">
          {t("issueDetail.noRecentCrashes")}
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-200 text-left text-xs">
            <thead>
              <tr className="text-slate-500">
                <th className="pb-2 pr-3 font-medium">
                  {t("issueDetail.colCrash")}
                </th>
                <th className="pb-2 pr-3 font-medium">
                  {t("issueDetail.colVersion")}
                </th>
                <th className="pb-2 pr-3 font-medium">
                  {t("issueDetail.colStatusAi")}
                </th>
                <th className="pb-2 font-medium">{t("issueDetail.colTime")}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {recentCrashes.map((crash: CrashSummary) => {
                const conf = formatConfidence(crash.ai_confidence);
                return (
                  <tr key={crash.id} className="hover:bg-slate-50">
                    <td className="py-2 pr-3">
                      <Link
                        to={`/crashes/${crash.id}`}
                        className="font-mono font-medium text-indigo-600 hover:underline"
                      >
                        {crash.id.slice(0, 8)}
                      </Link>
                      {crash.session_id && (
                        <div className="mt-0.5">
                          <Link
                            to={`/crashes?session_id=${encodeURIComponent(crash.session_id)}`}
                            className="inline-block max-w-[160px] truncate rounded border border-emerald-200 bg-emerald-50 px-1.5 py-0.5 font-mono text-[10px] text-emerald-700 hover:bg-emerald-100"
                          >
                            {crash.session_id}
                          </Link>
                        </div>
                      )}
                      {crash.crash_reason && (
                        <div className="max-w-[180px] truncate text-[11px] text-slate-500">
                          {crash.crash_reason}
                        </div>
                      )}
                    </td>
                    <td className="py-2 pr-3 font-mono text-slate-700">
                      {crash.app_version}
                    </td>
                    <td className="py-2 pr-3">
                      <div className="flex flex-wrap items-center gap-1">
                        <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] font-medium text-slate-700">
                          {formatCrashStatus(crash.status)}
                        </span>
                        {crash.ai_verdict && (
                          <span className="rounded bg-indigo-50 px-1.5 py-0.5 text-[11px] text-indigo-700">
                            {formatVerdict(crash.ai_verdict)}
                            {conf ? ` (${conf})` : ""}
                          </span>
                        )}
                        {crash.is_human_verified && (
                          <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-[11px] text-emerald-700">
                            {t("issueDetail.verifiedBadge")}
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="py-2 text-slate-500">
                      {formatDateTime(crash.crash_time || crash.created_at)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
