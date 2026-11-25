import { useMemo, useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { Split, X } from "lucide-react";
import { listIssues } from "../../api/client";
import type { CrashClusterRead, IssueSplitRequest } from "../../api/types";
import { useAppI18n } from "../../i18n";

interface SplitClusterModalProps {
  cluster: CrashClusterRead;
  projectId: string;
  currentIssueId: number;
  isPending: boolean;
  errorMessage: string | null;
  onClose: () => void;
  onSubmit: (payload: IssueSplitRequest) => void;
}

export function SplitClusterModal({
  cluster,
  projectId,
  currentIssueId,
  isPending,
  errorMessage,
  onClose,
  onSubmit,
}: SplitClusterModalProps) {
  const { t, formatIssueStatus } = useAppI18n();

  const [splitMode, setSplitMode] = useState<"new" | "existing">("new");
  const [splitNewTitle, setSplitNewTitle] = useState("");
  const [splitTargetIssueId, setSplitTargetIssueId] = useState<string>("");

  const projectIssuesQuery = useQuery({
    queryKey: ["issues", "split-targets", projectId],
    queryFn: () =>
      listIssues({
        project_id: projectId,
        limit: 100,
        offset: 0,
      }),
    enabled: Boolean(projectId),
  });

  const otherProjectIssues = useMemo(
    () =>
      (projectIssuesQuery.data?.items ?? []).filter(
        (item) => item.id !== currentIssueId,
      ),
    [projectIssuesQuery.data?.items, currentIssueId],
  );

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (splitMode === "new") {
      if (!splitNewTitle.trim()) {
        return;
      }
      onSubmit({
        cluster_id: cluster.id,
        new_issue_title: splitNewTitle.trim(),
      });
    } else {
      const parsedTarget = Number(splitTargetIssueId);
      if (!Number.isInteger(parsedTarget) || parsedTarget <= 0) {
        return;
      }
      onSubmit({
        cluster_id: cluster.id,
        target_issue_id: parsedTarget,
      });
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="split-cluster-modal-title"
    >
      <div className="w-full max-w-lg rounded-xl border border-slate-200 bg-white p-6 shadow-xl">
        <div className="flex items-center justify-between">
          <h2
            id="split-cluster-modal-title"
            className="text-base font-semibold text-slate-900"
          >
            {t("issueDetail.splitModalTitle")}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <p className="mt-1 text-xs text-slate-500">
          {t("issueDetail.splitModalDescBefore")}
          <code className="font-mono">
            {cluster.signature_hash.slice(0, 12)}
          </code>
          {t("issueDetail.splitModalDescMiddle", {
            count: cluster.crash_count,
          })}
        </p>

        <form onSubmit={handleSubmit} className="mt-4 space-y-4">
          {errorMessage && (
            <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
              {errorMessage}
            </div>
          )}

          <div className="space-y-2">
            <label className="flex cursor-pointer items-center gap-2 text-sm font-medium text-slate-800">
              <input
                type="radio"
                name="split-mode"
                checked={splitMode === "new"}
                onChange={() => setSplitMode("new")}
              />
              {t("issueDetail.splitCreateNew")}
            </label>
            {splitMode === "new" && (
              <input
                type="text"
                required
                value={splitNewTitle}
                onChange={(e) => setSplitNewTitle(e.target.value)}
                placeholder={t("issueDetail.splitNewTitlePlaceholder")}
                className="h-9 w-full rounded-md border border-slate-300 px-3 text-sm text-slate-900"
              />
            )}
          </div>

          <div className="space-y-2">
            <label className="flex cursor-pointer items-center gap-2 text-sm font-medium text-slate-800">
              <input
                type="radio"
                name="split-mode"
                checked={splitMode === "existing"}
                onChange={() => setSplitMode("existing")}
              />
              {t("issueDetail.splitReassignExisting")}
            </label>
            {splitMode === "existing" && (
              <select
                required
                value={splitTargetIssueId}
                onChange={(e) => setSplitTargetIssueId(e.target.value)}
                className="h-9 w-full rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900"
              >
                <option value="">{t("issueDetail.splitSelectTarget")}</option>
                {otherProjectIssues.map((cand) => (
                  <option key={cand.id} value={cand.id}>
                    #{cand.id} — {cand.title} ({formatIssueStatus(cand.status)})
                  </option>
                ))}
              </select>
            )}
          </div>

          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
            >
              {t("common.cancel")}
            </button>
            <button
              type="submit"
              disabled={
                isPending ||
                (splitMode === "new"
                  ? !splitNewTitle.trim()
                  : !splitTargetIssueId)
              }
              className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
            >
              <Split className="h-4 w-4" />
              {isPending
                ? t("issueDetail.moving")
                : t("issueDetail.moveCluster")}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
