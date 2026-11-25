import { useEffect, useState, type FormEvent } from "react";
import { AlertTriangle, CheckCircle2, Plus, Save, X } from "lucide-react";
import type {
  IssueDetailRead,
  IssueRead,
  IssueStatus,
  IssueUpdateRequest,
} from "../../api/types";
import { ISSUE_STATUSES } from "../../components/IssueStatusWorkflow";
import { useAppI18n } from "../../i18n";
import { isValidRegex } from "./utils";

export interface SavedIssueSync {
  issue: IssueRead;
  version: number;
}

export interface TransitionFieldSync {
  status: IssueStatus;
  fixedInVersion: string;
  version: number;
}

interface IssueKnowledgeBaseFormProps {
  issue: IssueDetailRead;
  savedIssueSync: SavedIssueSync | null;
  transitionSync: TransitionFieldSync | null;
  saveBanner: string | null;
  isPending: boolean;
  errorMessage: string | null;
  onSubmit: (payload: IssueUpdateRequest) => void;
}

export function IssueKnowledgeBaseForm({
  issue,
  savedIssueSync,
  transitionSync,
  saveBanner,
  isPending,
  errorMessage,
  onSubmit,
}: IssueKnowledgeBaseFormProps) {
  const { t, formatIssueStatus } = useAppI18n();

  const [title, setTitle] = useState(issue.title);
  const [status, setStatus] = useState<IssueStatus>(issue.status);
  const [component, setComponent] = useState(issue.component ?? "");
  const [fixedInVersion, setFixedInVersion] = useState(
    issue.fixed_in_version ?? "",
  );
  const [rootCause, setRootCause] = useState(issue.root_cause ?? "");
  const [isAmbiguousStack, setIsAmbiguousStack] = useState(
    issue.is_ambiguous_stack,
  );
  const [logPatterns, setLogPatterns] = useState<string[]>(() =>
    Array.isArray(issue.log_patterns_json) ? issue.log_patterns_json : [],
  );
  const [newPatternInput, setNewPatternInput] = useState("");

  useEffect(() => {
    if (!savedIssueSync) {
      return;
    }
    const source = savedIssueSync.issue;
    setTitle(source.title);
    setStatus(source.status);
    setComponent(source.component ?? "");
    setFixedInVersion(source.fixed_in_version ?? "");
    setRootCause(source.root_cause ?? "");
    setIsAmbiguousStack(source.is_ambiguous_stack);
    setLogPatterns(
      Array.isArray(source.log_patterns_json) ? source.log_patterns_json : [],
    );
  }, [savedIssueSync]);

  useEffect(() => {
    if (!transitionSync) {
      return;
    }
    setStatus(transitionSync.status);
    setFixedInVersion(transitionSync.fixedInVersion);
  }, [transitionSync]);

  const handleAddPattern = () => {
    const trimmed = newPatternInput.trim();
    if (!trimmed) {
      return;
    }
    if (!logPatterns.includes(trimmed)) {
      setLogPatterns((prev) => [...prev, trimmed]);
    }
    setNewPatternInput("");
  };

  const handleRemovePattern = (pattern: string) => {
    setLogPatterns((prev) => prev.filter((item) => item !== pattern));
  };

  const handleSaveForm = (e: FormEvent) => {
    e.preventDefault();
    if (!title.trim()) {
      return;
    }
    onSubmit({
      title: title.trim(),
      status,
      component: component.trim() || null,
      fixed_in_version: fixedInVersion.trim() || null,
      root_cause: rootCause.trim() || null,
      is_ambiguous_stack: isAmbiguousStack,
      log_patterns_json: logPatterns,
    });
  };

  return (
    <form
      onSubmit={handleSaveForm}
      className="space-y-4 rounded-xl border border-slate-200 bg-white p-5 shadow-sm"
    >
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-900">
          {t("issueDetail.metadataTitle")}
        </h2>
        {saveBanner && (
          <span className="inline-flex items-center gap-1 text-xs font-medium text-emerald-700">
            <CheckCircle2 className="h-3.5 w-3.5" />
            {saveBanner}
          </span>
        )}
      </div>

      {errorMessage && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {errorMessage}
        </div>
      )}

      <div>
        <label className="block text-xs font-medium text-slate-700">
          {t("issueDetail.issueTitleLabel")}
        </label>
        <input
          type="text"
          required
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 text-sm text-slate-900 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
        />
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <div>
          <label className="block text-xs font-medium text-slate-700">
            {t("issueDetail.statusLabel")}
          </label>
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value as IssueStatus)}
            className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-900 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
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
            {t("issueDetail.componentLabel")}
          </label>
          <input
            type="text"
            value={component}
            onChange={(e) => setComponent(e.target.value)}
            placeholder={t("issueDetail.componentPlaceholder")}
            className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 text-sm text-slate-900"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-slate-700">
            {t("issueDetail.fixedInVersionLabel")}
          </label>
          <input
            type="text"
            value={fixedInVersion}
            onChange={(e) => setFixedInVersion(e.target.value)}
            placeholder={t("issueDetail.fixedInVersionPlaceholder")}
            className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 font-mono text-sm text-slate-900"
          />
        </div>
      </div>

      <div>
        <label className="block text-xs font-medium text-slate-700">
          {t("issueDetail.rootCauseLabel")}
        </label>
        <textarea
          rows={4}
          value={rootCause}
          onChange={(e) => setRootCause(e.target.value)}
          placeholder={t("issueDetail.rootCausePlaceholder")}
          className="mt-1 w-full rounded-md border border-slate-300 p-2.5 text-sm text-slate-900 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
        />
      </div>

      <label className="flex cursor-pointer items-start gap-2.5 rounded-lg border border-slate-200 bg-slate-50 p-3">
        <input
          type="checkbox"
          checked={isAmbiguousStack}
          onChange={(e) => setIsAmbiguousStack(e.target.checked)}
          className="mt-0.5 h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
        />
        <div>
          <div className="text-xs font-semibold text-slate-800">
            {t("issueDetail.ambiguousTitle")}
          </div>
          <p className="text-[11px] text-slate-500">
            {t("issueDetail.ambiguousDescBefore")}
            <code className="font-mono">SMART_TRIAGE</code>
            {t("issueDetail.ambiguousDescAfter")}
          </p>
        </div>
      </label>

      {/* Log Patterns Tag / Regex Editor */}
      <div>
        <label className="block text-xs font-medium text-slate-700">
          {t("issueDetail.logPatternsTitle")} (
          <code className="font-mono">log_patterns_json</code>)
        </label>
        <div className="mt-1.5 flex gap-2">
          <input
            type="text"
            value={newPatternInput}
            onChange={(e) => setNewPatternInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                handleAddPattern();
              }
            }}
            placeholder={t("issueDetail.addPatternPlaceholder")}
            className="h-9 flex-1 rounded-md border border-slate-300 px-3 font-mono text-xs text-slate-900"
          />
          <button
            type="button"
            onClick={handleAddPattern}
            className="inline-flex h-9 items-center gap-1 rounded-md border border-slate-300 bg-white px-3 text-xs font-medium text-slate-700 hover:bg-slate-50"
          >
            <Plus className="h-3.5 w-3.5" />
            {t("issueDetail.addPatternButton")}
          </button>
        </div>

        {logPatterns.length === 0 ? (
          <p className="mt-2 text-xs text-slate-400">
            {t("issueDetail.noPatterns")}
          </p>
        ) : (
          <div className="mt-2.5 flex flex-wrap gap-2">
            {logPatterns.map((pattern) => {
              const regexOk = isValidRegex(pattern);
              return (
                <span
                  key={pattern}
                  className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 font-mono text-xs ${
                    regexOk
                      ? "border-slate-200 bg-slate-100 text-slate-800"
                      : "border-amber-300 bg-amber-50 text-amber-800"
                  }`}
                  title={
                    regexOk
                      ? t("issueDetail.validRegexTitle")
                      : t("issueDetail.invalidRegexTitle")
                  }
                >
                  {!regexOk && (
                    <AlertTriangle className="h-3 w-3 text-amber-600" />
                  )}
                  <span>{pattern}</span>
                  <button
                    type="button"
                    onClick={() => handleRemovePattern(pattern)}
                    className="rounded p-0.5 text-slate-500 hover:bg-slate-200 hover:text-slate-900"
                    aria-label={t("issueDetail.removePatternAria", {
                      pattern,
                    })}
                  >
                    <X className="h-3 w-3" />
                  </button>
                </span>
              );
            })}
          </div>
        )}
      </div>

      <div className="flex justify-end pt-2">
        <button
          type="submit"
          disabled={isPending || !title.trim()}
          className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-indigo-700 disabled:opacity-50"
        >
          <Save className="h-4 w-4" />
          {isPending
            ? t("common.saving")
            : t("issueDetail.saveKnowledgeBase")}
        </button>
      </div>
    </form>
  );
}
