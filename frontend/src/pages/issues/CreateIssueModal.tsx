import { useEffect, useState, type FormEvent } from "react";
import { AlertTriangle, X } from "lucide-react";
import type {
  IssueCreateRequest,
  IssueStatus,
  ProjectRead,
} from "../../api/types";
import { ISSUE_STATUSES } from "../../components/IssueStatusWorkflow";
import { useAppI18n } from "../../i18n";
import { parsePatternLines } from "./utils";

interface CreateIssueModalProps {
  isOpen: boolean;
  projects: ProjectRead[];
  selectedProjectId: string;
  resetNonce: number;
  isPending: boolean;
  errorMessage: string | null;
  onClose: () => void;
  onSubmit: (payload: IssueCreateRequest) => void;
}

export function CreateIssueModal({
  isOpen,
  projects,
  selectedProjectId,
  resetNonce,
  isPending,
  errorMessage,
  onClose,
  onSubmit,
}: CreateIssueModalProps) {
  const { t, formatIssueStatus } = useAppI18n();

  const [newProjectId, setNewProjectId] = useState("");
  const [newTitle, setNewTitle] = useState("");
  const [newStatus, setNewStatus] = useState<IssueStatus>("NEW");
  const [newComponent, setNewComponent] = useState("");
  const [newFixedInVersion, setNewFixedInVersion] = useState("");
  const [newRootCause, setNewRootCause] = useState("");
  const [newLogPatternsText, setNewLogPatternsText] = useState("");
  const [newIsAmbiguous, setNewIsAmbiguous] = useState(false);
  const [newIsPinned, setNewIsPinned] = useState(false);

  useEffect(() => {
    if (isOpen) {
      setNewProjectId(selectedProjectId || projects[0]?.id || "");
    }
  }, [isOpen, selectedProjectId, projects]);

  useEffect(() => {
    if (resetNonce === 0) {
      return;
    }
    setNewTitle("");
    setNewStatus("NEW");
    setNewComponent("");
    setNewFixedInVersion("");
    setNewRootCause("");
    setNewLogPatternsText("");
    setNewIsAmbiguous(false);
    setNewIsPinned(false);
  }, [resetNonce]);

  if (!isOpen) {
    return null;
  }

  const effectiveCreateProjectId =
    selectedProjectId || newProjectId || projects[0]?.id || "";

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!effectiveCreateProjectId || !newTitle.trim()) {
      return;
    }
    onSubmit({
      project_id: effectiveCreateProjectId,
      title: newTitle.trim(),
      status: newStatus,
      component: newComponent.trim() || null,
      fixed_in_version: newFixedInVersion.trim() || null,
      root_cause: newRootCause.trim() || null,
      log_patterns_json: parsePatternLines(newLogPatternsText),
      is_ambiguous_stack: newIsAmbiguous,
      is_pinned: newIsPinned,
    });
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="create-issue-modal-title"
    >
      <div className="w-full max-w-xl rounded-xl border border-slate-200 bg-white p-6 shadow-xl">
        <div className="flex items-center justify-between">
          <h2
            id="create-issue-modal-title"
            className="text-base font-semibold text-slate-900"
          >
            {t("issues.createModalTitle")}
          </h2>
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
            <div className="flex items-center gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
              <AlertTriangle className="h-4 w-4 shrink-0" />
              <span>{errorMessage}</span>
            </div>
          )}

          <div>
            <label className="block text-xs font-medium text-slate-700">
              {t("issues.projectLabel")}
            </label>
            <select
              required
              disabled={Boolean(selectedProjectId)}
              value={effectiveCreateProjectId}
              onChange={(e) => setNewProjectId(e.target.value)}
              className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900 disabled:bg-slate-100"
            >
              {projects.length === 0 && (
                <option value="">{t("issues.noProjectsAvailable")}</option>
              )}
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} ({p.key})
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-xs font-medium text-slate-700">
              {t("issues.titleLabel")}{" "}
              <span className="text-rose-500">*</span>
            </label>
            <input
              type="text"
              required
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
              placeholder={t("issues.titlePlaceholder")}
              className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 text-sm text-slate-900"
            />
          </div>

          <div className="grid gap-3 sm:grid-cols-3">
            <div>
              <label className="block text-xs font-medium text-slate-700">
                {t("issues.statusLabel")}
              </label>
              <select
                value={newStatus}
                onChange={(e) => setNewStatus(e.target.value as IssueStatus)}
                className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-900"
              >
                {ISSUE_STATUSES.map((st) => (
                  <option key={st} value={st}>
                    {formatIssueStatus(st)}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-700">
                {t("issues.componentLabel")}
              </label>
              <input
                type="text"
                value={newComponent}
                onChange={(e) => setNewComponent(e.target.value)}
                placeholder={t("issues.componentExamplePlaceholder")}
                className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 text-sm text-slate-900"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-700">
                {t("issues.fixedInVersionLabel")}
              </label>
              <input
                type="text"
                value={newFixedInVersion}
                onChange={(e) => setNewFixedInVersion(e.target.value)}
                placeholder={t("issues.fixedInVersionPlaceholder")}
                className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 font-mono text-sm text-slate-900"
              />
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-slate-700">
              {t("issues.rootCauseLabel")}
            </label>
            <textarea
              rows={3}
              value={newRootCause}
              onChange={(e) => setNewRootCause(e.target.value)}
              placeholder={t("issues.rootCausePlaceholder")}
              className="mt-1 w-full rounded-md border border-slate-300 p-2.5 text-sm text-slate-900"
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-slate-700">
              {t("issues.logPatternsLabel")}
            </label>
            <textarea
              rows={2}
              value={newLogPatternsText}
              onChange={(e) => setNewLogPatternsText(e.target.value)}
              placeholder="FATAL:.*DeviceLost"
              className="mt-1 w-full rounded-md border border-slate-300 p-2.5 font-mono text-xs text-slate-900"
            />
          </div>

          <div className="flex flex-wrap gap-4">
            <label className="inline-flex items-center gap-2 text-xs text-slate-700">
              <input
                type="checkbox"
                checked={newIsAmbiguous}
                onChange={(e) => setNewIsAmbiguous(e.target.checked)}
                className="h-4 w-4 rounded border-slate-300 text-indigo-600"
              />
              {t("issues.ambiguousStackCheckbox")}
            </label>
            <label className="inline-flex items-center gap-2 text-xs text-slate-700">
              <input
                type="checkbox"
                checked={newIsPinned}
                onChange={(e) => setNewIsPinned(e.target.checked)}
                className="h-4 w-4 rounded border-slate-300 text-indigo-600"
              />
              {t("issues.pinIssueCheckbox")}
            </label>
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
                isPending || !effectiveCreateProjectId || !newTitle.trim()
              }
              className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
            >
              {isPending
                ? t("common.creating")
                : t("issues.createIssueButton")}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
