import { Link } from "react-router-dom";
import { GitBranch, Split } from "lucide-react";
import type { CrashClusterRead } from "../../api/types";
import { useAppI18n } from "../../i18n";
import { formatSignatureFrames } from "./utils";

interface AssociatedClustersCardProps {
  clusters: CrashClusterRead[];
  onOpenSplitModal: (cluster: CrashClusterRead) => void;
}

export function AssociatedClustersCard({
  clusters,
  onOpenSplitModal,
}: AssociatedClustersCardProps) {
  const { t } = useAppI18n();

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <GitBranch className="h-4 w-4 text-indigo-600" />
          <h2 className="text-sm font-semibold text-slate-900">
            {t("issueDetail.associatedClusters", {
              count: clusters.length,
            })}
          </h2>
        </div>
      </div>

      {clusters.length === 0 ? (
        <p className="text-xs text-slate-500">{t("issueDetail.noClusters")}</p>
      ) : (
        <div className="space-y-3">
          {clusters.map((cluster) => (
            <div
              key={cluster.id}
              className="rounded-lg border border-slate-200 p-3"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs text-slate-800">
                    {cluster.signature_hash.slice(0, 12)}
                  </code>
                  {cluster.exception_type && (
                    <span className="font-mono text-xs text-slate-600">
                      {cluster.exception_type}
                    </span>
                  )}
                  <span className="rounded-full bg-indigo-50 px-2 py-0.5 font-mono text-[11px] font-medium text-indigo-700">
                    {t("issueDetail.clusterCrashesBadge", {
                      count: cluster.crash_count,
                    })}
                  </span>
                </div>

                <div className="flex items-center gap-2">
                  <Link
                    to={`/crashes?cluster_id=${encodeURIComponent(cluster.id)}`}
                    className="text-xs font-medium text-indigo-600 hover:underline"
                  >
                    {t("issueDetail.crashesLink")}
                  </Link>
                  <button
                    type="button"
                    onClick={() => onOpenSplitModal(cluster)}
                    className="inline-flex items-center gap-1 rounded border border-slate-200 bg-white px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
                  >
                    <Split className="h-3 w-3" />
                    {t("issueDetail.splitReassign")}
                  </button>
                </div>
              </div>
              <div className="mt-2 truncate font-mono text-[11px] text-slate-600">
                {formatSignatureFrames(
                  cluster.signature_frames_json,
                  cluster.signature_text,
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
