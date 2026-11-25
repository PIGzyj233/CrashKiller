import { GitMerge, X } from "lucide-react";
import type { IssueRead } from "../../api/types";
import { useAppI18n } from "../../i18n";

interface MergeIssuesModalProps {
  isOpen: boolean;
  selectedIssues: IssueRead[];
  mergeTargetId: number | null;
  hasCrossProjectSelection: boolean;
  isPending: boolean;
  errorMessage: string | null;
  onSelectTarget: (issueId: number) => void;
  onClose: () => void;
  onConfirm: () => void;
}

export function MergeIssuesModal({
  isOpen,
  selectedIssues,
  mergeTargetId,
  hasCrossProjectSelection,
  isPending,
  errorMessage,
  onSelectTarget,
  onClose,
  onConfirm,
}: MergeIssuesModalProps) {
  const { t, formatIssueStatus } = useAppI18n();

  if (!isOpen) {
    return null;
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="merge-modal-title"
    >
      <div className="w-full max-w-lg rounded-xl border border-slate-200 bg-white p-6 shadow-xl">
        <div className="flex items-center justify-between">
          <h2
            id="merge-modal-title"
            className="text-base font-semibold text-slate-900"
          >
            {t("issues.mergeModalTitle")}
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
          {t("issues.mergeModalDesc")}
        </p>

        {errorMessage && (
          <div className="mt-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
            {errorMessage}
          </div>
        )}

        <div className="mt-4 max-h-64 space-y-2 overflow-y-auto">
          {selectedIssues.map((issue) => (
            <label
              key={issue.id}
              className={`flex cursor-pointer items-start gap-3 rounded-lg border p-3 text-sm ${
                mergeTargetId === issue.id
                  ? "border-indigo-500 bg-indigo-50/50"
                  : "border-slate-200 hover:bg-slate-50"
              }`}
            >
              <input
                type="radio"
                name="merge-target-issue"
                checked={mergeTargetId === issue.id}
                onChange={() => onSelectTarget(issue.id)}
                className="mt-0.5 text-indigo-600 focus:ring-indigo-500"
              />
              <div className="min-w-0 flex-1">
                <div className="font-medium text-slate-900">
                  #{issue.id} — {issue.title}
                </div>
                <div className="mt-0.5 text-xs text-slate-500">
                  {t("issues.mergeItemMeta", {
                    status: formatIssueStatus(issue.status),
                    count: issue.crash_count,
                  })}
                </div>
              </div>
            </label>
          ))}
        </div>

        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
          >
            {t("common.cancel")}
          </button>
          <button
            type="button"
            disabled={
              mergeTargetId === null || hasCrossProjectSelection || isPending
            }
            onClick={onConfirm}
            className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
          >
            <GitMerge className="h-4 w-4" />
            {isPending ? t("issues.merging") : t("issues.confirmMerge")}
          </button>
        </div>
      </div>
    </div>
  );
}
