import { useEffect, useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { Plus, UserCheck, X } from "lucide-react";
import { listIssues } from "../../api/client";
import type {
  AIAnalysisResult,
  CrashDetailRead,
  CrashVerdictRequest,
  IssueStatus,
  VerdictAction,
} from "../../api/types";
import { useAppI18n } from "../../i18n";
import { ISSUE_STATUSES } from "./utils";

interface HumanVerdictModalProps {
  crash: CrashDetailRead;
  latestAIResult: AIAnalysisResult | null;
  isPending: boolean;
  errorMessage: string | null;
  onClose: () => void;
  onSubmit: (payload: CrashVerdictRequest) => void;
}

export function HumanVerdictModal({
  crash,
  latestAIResult,
  isPending,
  errorMessage,
  onClose,
  onSubmit,
}: HumanVerdictModalProps) {
  const { t, formatIssueStatus } = useAppI18n();

  const linkedIssueId = crash.issue_id ?? crash.cluster?.issue_id ?? null;

  const [verdictAction, setVerdictAction] = useState<VerdictAction>(() =>
    linkedIssueId
      ? "confirm"
      : latestAIResult?.matched_issue_id != null
        ? "assign_issue"
        : "create_issue",
  );
  const [issueSearchInput, setIssueSearchInput] = useState("");
  const [debouncedIssueSearch, setDebouncedIssueSearch] = useState("");
  const [assignIssueId, setAssignIssueId] = useState<string>(() =>
    linkedIssueId
      ? String(linkedIssueId)
      : latestAIResult?.matched_issue_id != null
        ? String(latestAIResult.matched_issue_id)
        : "",
  );
  const [newIssueTitle, setNewIssueTitle] = useState<string>(
    () =>
      latestAIResult?.suggested_title ||
      crash.crash_reason ||
      t("crashDetail.defaultNewIssueTitle", { version: crash.app_version }),
  );
  const [newIssueComponent, setNewIssueComponent] = useState<string>(
    () => latestAIResult?.suggested_component ?? "",
  );
  const [newIssueStatus, setNewIssueStatus] = useState<IssueStatus>("KNOWN");
  const [verdictRootCause, setVerdictRootCause] = useState<string>(
    () => crash.issue?.root_cause ?? latestAIResult?.root_cause_analysis ?? "",
  );
  const [verdictLogPatterns, setVerdictLogPatterns] = useState<string[]>(() =>
    latestAIResult?.key_log_patterns
      ? [...latestAIResult.key_log_patterns]
      : [],
  );
  const [patternDraft, setPatternDraft] = useState("");
  const [verdictAmbiguousStack, setVerdictAmbiguousStack] = useState<boolean>(
    () => crash.issue?.is_ambiguous_stack ?? false,
  );

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedIssueSearch(issueSearchInput.trim());
    }, 250);
    return () => window.clearTimeout(timer);
  }, [issueSearchInput]);

  const issuesPickerQuery = useQuery({
    queryKey: ["issues", "picker", crash.project_id, debouncedIssueSearch],
    queryFn: () =>
      listIssues({
        project_id: crash.project_id,
        q: debouncedIssueSearch || undefined,
        limit: 50,
        offset: 0,
      }),
    enabled: Boolean(crash.project_id),
  });

  const handleAddVerdictPattern = (pat?: string) => {
    const trimmed = (pat ?? patternDraft).trim();
    if (!trimmed) {
      return;
    }
    if (!verdictLogPatterns.includes(trimmed)) {
      setVerdictLogPatterns((prev) => [...prev, trimmed]);
    }
    if (pat === undefined) {
      setPatternDraft("");
    }
  };

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    const payload: CrashVerdictRequest = {
      action: verdictAction,
      root_cause: verdictRootCause.trim() || undefined,
      add_log_patterns:
        verdictLogPatterns.length > 0 ? verdictLogPatterns : undefined,
      is_ambiguous_stack: verdictAmbiguousStack,
    };

    if (verdictAction === "confirm") {
      if (!crash.issue_id && !crash.cluster?.issue_id) {
        if (latestAIResult?.matched_issue_id == null) {
          return;
        }
        payload.action = "assign_issue";
        payload.issue_id = latestAIResult.matched_issue_id;
      }
    } else if (verdictAction === "assign_issue") {
      const targetId = Number(assignIssueId);
      if (!Number.isInteger(targetId) || targetId <= 0) {
        return;
      }
      payload.issue_id = targetId;
    } else if (verdictAction === "create_issue") {
      if (!newIssueTitle.trim()) {
        return;
      }
      payload.new_issue = {
        title: newIssueTitle.trim(),
        component: newIssueComponent.trim() || undefined,
        root_cause: verdictRootCause.trim() || undefined,
        status: newIssueStatus,
      };
    }

    onSubmit(payload);
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="verdict-modal-title"
    >
      <div className="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-xl border border-slate-200 bg-white p-6 shadow-xl">
        <div className="flex items-center justify-between">
          <div>
            <h2
              id="verdict-modal-title"
              className="text-base font-semibold text-slate-900"
            >
              {t("crashDetail.modalTitle")}
            </h2>
            <p className="mt-0.5 text-xs text-slate-500">
              {t("crashDetail.modalSubtitle")}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="mt-4 space-y-4">
          {errorMessage && (
            <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
              {errorMessage}
            </div>
          )}

          {/* Action Tabs */}
          <div className="grid gap-2 sm:grid-cols-3">
            <button
              type="button"
              disabled={
                !crash.issue_id &&
                !crash.cluster?.issue_id &&
                latestAIResult?.matched_issue_id == null
              }
              onClick={() => setVerdictAction("confirm")}
              className={`rounded-lg border p-2.5 text-left text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                verdictAction === "confirm"
                  ? "border-indigo-500 bg-indigo-50 text-indigo-900"
                  : "border-slate-200 hover:bg-slate-50"
              }`}
            >
              <div className="font-semibold">
                {t("crashDetail.actionConfirm")}
              </div>
              <div className="mt-0.5 text-[11px] text-slate-500">
                {crash.issue_id
                  ? t("crashDetail.actionConfirmIssue", {
                      id: crash.issue_id,
                    })
                  : crash.cluster?.issue_id
                    ? t("crashDetail.actionConfirmClusterIssue", {
                        id: crash.cluster.issue_id,
                      })
                    : latestAIResult?.matched_issue_id != null
                      ? t("crashDetail.actionConfirmAiIssue", {
                          id: latestAIResult.matched_issue_id,
                        })
                      : t("crashDetail.actionConfirmRequiresLink")}
              </div>
            </button>

            <button
              type="button"
              onClick={() => setVerdictAction("assign_issue")}
              className={`rounded-lg border p-2.5 text-left text-xs transition-colors ${
                verdictAction === "assign_issue"
                  ? "border-indigo-500 bg-indigo-50 text-indigo-900"
                  : "border-slate-200 hover:bg-slate-50"
              }`}
            >
              <div className="font-semibold">
                {t("crashDetail.actionAssign")}
              </div>
              <div className="mt-0.5 text-[11px] text-slate-500">
                {t("crashDetail.actionAssignDesc")}
              </div>
            </button>

            <button
              type="button"
              onClick={() => setVerdictAction("create_issue")}
              className={`rounded-lg border p-2.5 text-left text-xs transition-colors ${
                verdictAction === "create_issue"
                  ? "border-indigo-500 bg-indigo-50 text-indigo-900"
                  : "border-slate-200 hover:bg-slate-50"
              }`}
            >
              <div className="font-semibold">
                {t("crashDetail.actionCreate")}
              </div>
              <div className="mt-0.5 text-[11px] text-slate-500">
                {t("crashDetail.actionCreateDesc")}
              </div>
            </button>
          </div>

          {verdictAction === "assign_issue" && (
            <div className="space-y-2 rounded-lg border border-slate-200 bg-slate-50 p-3">
              <label className="block text-xs font-medium text-slate-700">
                {t("crashDetail.searchSelectIssue")}
              </label>
              <input
                type="text"
                value={issueSearchInput}
                onChange={(e) => setIssueSearchInput(e.target.value)}
                placeholder={t("crashDetail.filterIssuesPlaceholder")}
                className="h-8 w-full rounded-md border border-slate-300 bg-white px-2.5 text-xs text-slate-900"
              />
              <select
                required
                value={assignIssueId}
                onChange={(e) => setAssignIssueId(e.target.value)}
                className="h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-900"
              >
                <option value="">{t("crashDetail.selectIssueOption")}</option>
                {(issuesPickerQuery.data?.items ?? []).map((iss) => (
                  <option key={iss.id} value={iss.id}>
                    #{iss.id} — {iss.title} [{formatIssueStatus(iss.status)}]
                  </option>
                ))}
              </select>
            </div>
          )}

          {verdictAction === "create_issue" && (
            <div className="space-y-3 rounded-lg border border-slate-200 bg-slate-50 p-3">
              <div>
                <label className="block text-xs font-medium text-slate-700">
                  {t("crashDetail.newIssueTitle")}{" "}
                  <span className="text-rose-500">*</span>
                </label>
                <input
                  type="text"
                  required
                  value={newIssueTitle}
                  onChange={(e) => setNewIssueTitle(e.target.value)}
                  className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900"
                />
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    {t("crashDetail.component")}
                  </label>
                  <input
                    type="text"
                    value={newIssueComponent}
                    onChange={(e) => setNewIssueComponent(e.target.value)}
                    placeholder={t("crashDetail.componentPlaceholder")}
                    className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    {t("crashDetail.initialStatus")}
                  </label>
                  <select
                    value={newIssueStatus}
                    onChange={(e) =>
                      setNewIssueStatus(e.target.value as IssueStatus)
                    }
                    className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-900"
                  >
                    {ISSUE_STATUSES.map((st) => (
                      <option key={st} value={st}>
                        {formatIssueStatus(st)}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
            </div>
          )}

          <div>
            <label className="block text-xs font-medium text-slate-700">
              {t("crashDetail.rootCauseAnalysis")}
            </label>
            <textarea
              rows={3}
              value={verdictRootCause}
              onChange={(e) => setVerdictRootCause(e.target.value)}
              placeholder={t("crashDetail.rootCausePlaceholder")}
              className="mt-1 w-full rounded-md border border-slate-300 p-2.5 text-sm text-slate-900"
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-slate-700">
              {t("crashDetail.addLogPatterns")}
            </label>
            <div className="mt-1 flex gap-2">
              <input
                type="text"
                value={patternDraft}
                onChange={(e) => setPatternDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    handleAddVerdictPattern();
                  }
                }}
                placeholder={t("crashDetail.logPatternPlaceholder")}
                className="h-9 flex-1 rounded-md border border-slate-300 px-3 font-mono text-xs text-slate-900"
              />
              <button
                type="button"
                onClick={() => handleAddVerdictPattern()}
                className="inline-flex h-9 items-center gap-1 rounded-md border border-slate-300 bg-white px-3 text-xs font-medium text-slate-700 hover:bg-slate-50"
              >
                <Plus className="h-3.5 w-3.5" />
                {t("crashDetail.addPatternButton")}
              </button>
            </div>

            {latestAIResult && latestAIResult.key_log_patterns.length > 0 && (
              <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px]">
                <span className="text-slate-500">
                  {t("crashDetail.aiSuggestions")}
                </span>
                {latestAIResult.key_log_patterns.map((pat) => (
                  <button
                    key={pat}
                    type="button"
                    onClick={() => handleAddVerdictPattern(pat)}
                    className="rounded border border-indigo-200 bg-indigo-50 px-1.5 py-0.5 font-mono text-indigo-700 hover:bg-indigo-100"
                  >
                    + {pat}
                  </button>
                ))}
              </div>
            )}

            {verdictLogPatterns.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1.5">
                {verdictLogPatterns.map((pat) => (
                  <span
                    key={pat}
                    className="inline-flex items-center gap-1 rounded border border-slate-200 bg-slate-100 px-2 py-0.5 font-mono text-xs text-slate-800"
                  >
                    {pat}
                    <button
                      type="button"
                      onClick={() =>
                        setVerdictLogPatterns((prev) =>
                          prev.filter((p) => p !== pat),
                        )
                      }
                      className="rounded p-0.5 text-slate-500 hover:bg-slate-200"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </span>
                ))}
              </div>
            )}
          </div>

          <label className="flex cursor-pointer items-center gap-2 text-xs text-slate-700">
            <input
              type="checkbox"
              checked={verdictAmbiguousStack}
              onChange={(e) => setVerdictAmbiguousStack(e.target.checked)}
              className="h-4 w-4 rounded border-slate-300 text-indigo-600"
            />
            {t("crashDetail.ambiguousStackLabel")}
          </label>

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
                (verdictAction === "assign_issue" && !assignIssueId) ||
                (verdictAction === "create_issue" && !newIssueTitle.trim())
              }
              className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
            >
              <UserCheck className="h-4 w-4" />
              {isPending ? t("common.saving") : t("crashDetail.submitVerdict")}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
