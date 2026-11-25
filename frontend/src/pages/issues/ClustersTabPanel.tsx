import { useState } from "react";
import { Link } from "react-router-dom";
import { Check, ChevronLeft, ChevronRight, Copy } from "lucide-react";
import type { CrashClusterRead, ProjectRead } from "../../api/types";
import { useAppI18n } from "../../i18n";
import { formatSignatureFrames, PAGE_SIZE } from "./utils";

interface ClustersTabPanelProps {
  unassignedOnly: boolean;
  onUnassignedOnlyChange: (checked: boolean) => void;
  selectedProjectId: string;
  projectById: Map<string, ProjectRead>;
  clustersList: CrashClusterRead[];
  clustersTotal: number;
  clustersPage: number;
  onPageChange: (nextPage: number) => void;
  isPending: boolean;
  isError: boolean;
  error: unknown;
  onRetry: () => void;
}

export function ClustersTabPanel({
  unassignedOnly,
  onUnassignedOnlyChange,
  selectedProjectId,
  projectById,
  clustersList,
  clustersTotal,
  clustersPage,
  onPageChange,
  isPending,
  isError,
  error,
  onRetry,
}: ClustersTabPanelProps) {
  const { t, formatNumber } = useAppI18n();
  const [copiedHash, setCopiedHash] = useState<string | null>(null);

  const clustersFrom = clustersTotal === 0 ? 0 : clustersPage * PAGE_SIZE + 1;
  const clustersTo = Math.min(clustersTotal, (clustersPage + 1) * PAGE_SIZE);

  const handleCopyHash = (hash: string) => {
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      void navigator.clipboard.writeText(hash);
      setCopiedHash(hash);
      window.setTimeout(() => setCopiedHash(null), 1500);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white p-3.5 shadow-sm">
        <label className="inline-flex cursor-pointer items-center gap-2 text-sm text-slate-700">
          <input
            type="checkbox"
            checked={unassignedOnly}
            onChange={(e) => onUnassignedOnlyChange(e.target.checked)}
            className="h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
          />
          {t("issues.unassignedOnly")}
        </label>
        <span className="text-xs text-slate-500">
          {t("issues.clustersHint")}
        </span>
      </div>

      {isError && (
        <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          <span>
            {error instanceof Error
              ? error.message
              : t("issues.loadClustersFailed")}
          </span>
          <button
            type="button"
            onClick={onRetry}
            className="rounded border border-rose-300 bg-white px-2.5 py-1 text-xs font-medium text-rose-700"
          >
            {t("common.retry")}
          </button>
        </div>
      )}

      <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-200 text-left text-sm">
            <thead className="bg-slate-50 text-xs font-medium uppercase tracking-wider text-slate-500">
              <tr>
                <th className="px-4 py-3">{t("issues.colSignatureHash")}</th>
                <th className="px-4 py-3">{t("issues.colException")}</th>
                <th className="px-4 py-3">{t("issues.colSignatureFrames")}</th>
                <th className="px-4 py-3">{t("issues.colCrashes")}</th>
                <th className="px-4 py-3">{t("issues.colAssignedIssue")}</th>
                <th className="px-4 py-3 text-right">
                  {t("issues.colExplore")}
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {isPending ? (
                <tr>
                  <td
                    colSpan={6}
                    className="px-4 py-10 text-center text-sm text-slate-500"
                  >
                    {t("issues.loadingClusters")}
                  </td>
                </tr>
              ) : clustersList.length === 0 ? (
                <tr>
                  <td
                    colSpan={6}
                    className="px-4 py-12 text-center text-sm text-slate-500"
                  >
                    {t("issues.emptyClusters")}
                  </td>
                </tr>
              ) : (
                clustersList.map((cluster: CrashClusterRead) => {
                  const project = projectById.get(cluster.project_id);
                  return (
                    <tr key={cluster.id} className="hover:bg-slate-50">
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-1.5">
                          <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs text-slate-800">
                            {cluster.signature_hash.slice(0, 12)}
                          </code>
                          <button
                            type="button"
                            onClick={() =>
                              handleCopyHash(cluster.signature_hash)
                            }
                            className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
                            title={t("issues.copyHashTitle")}
                          >
                            {copiedHash === cluster.signature_hash ? (
                              <Check className="h-3.5 w-3.5 text-emerald-600" />
                            ) : (
                              <Copy className="h-3.5 w-3.5" />
                            )}
                          </button>
                        </div>
                        {!selectedProjectId && project && (
                          <div className="mt-1 font-mono text-[11px] text-slate-500">
                            {project.key}
                          </div>
                        )}
                      </td>
                      <td className="px-4 py-3 font-mono text-xs text-slate-700">
                        {cluster.exception_type || "—"}
                      </td>
                      <td className="max-w-md px-4 py-3 font-mono text-xs text-slate-600">
                        <div className="truncate">
                          {formatSignatureFrames(
                            cluster.signature_frames_json,
                            cluster.signature_text,
                          )}
                        </div>
                      </td>
                      <td className="px-4 py-3 font-mono text-xs font-semibold text-slate-800">
                        {formatNumber(cluster.crash_count)}
                      </td>
                      <td className="px-4 py-3 text-xs">
                        {cluster.issue_id !== null ? (
                          <Link
                            to={`/issues/${cluster.issue_id}`}
                            className="font-medium text-indigo-600 hover:underline"
                          >
                            {t("issues.issueRef", { id: cluster.issue_id })}
                          </Link>
                        ) : (
                          <span className="inline-flex rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-700">
                            {t("issues.unassignedBadge")}
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-right text-xs">
                        <Link
                          to={`/crashes?cluster_id=${encodeURIComponent(
                            cluster.id,
                          )}`}
                          className="font-medium text-indigo-600 hover:underline"
                        >
                          {t("issues.viewCrashes")}
                        </Link>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 bg-slate-50 px-4 py-3 text-xs text-slate-600">
          <span>
            {t("common.showingRange", {
              from: clustersFrom,
              to: clustersTo,
              total: formatNumber(clustersTotal),
            })}
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={clustersPage === 0}
              onClick={() => onPageChange(Math.max(0, clustersPage - 1))}
              className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1 font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
              {t("common.previous")}
            </button>
            <button
              type="button"
              disabled={(clustersPage + 1) * PAGE_SIZE >= clustersTotal}
              onClick={() => onPageChange(clustersPage + 1)}
              className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1 font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {t("common.next")}
              <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
