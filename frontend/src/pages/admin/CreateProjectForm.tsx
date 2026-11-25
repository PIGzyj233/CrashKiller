import type { FormEvent } from "react";
import { Plus } from "lucide-react";
import { ApiError } from "../../api/client";
import type { AITriageMode } from "../../api/types";
import { useAppI18n } from "../../i18n";
import { AI_TRIAGE_MODES } from "./helpers";

interface CreateProjectFormProps {
  projectKey: string;
  projectName: string;
  triageMode: AITriageMode | "";
  sessionIdLabel: string;
  isPending: boolean;
  isError: boolean;
  error: unknown;
  onKeyChange: (val: string) => void;
  onNameChange: (val: string) => void;
  onTriageModeChange: (val: AITriageMode | "") => void;
  onSessionIdLabelChange: (val: string) => void;
  onSubmit: (e: FormEvent) => void;
}

export function CreateProjectForm({
  projectKey,
  projectName,
  triageMode,
  sessionIdLabel,
  isPending,
  isError,
  error,
  onKeyChange,
  onNameChange,
  onTriageModeChange,
  onSessionIdLabelChange,
  onSubmit,
}: CreateProjectFormProps) {
  const { t, formatTriageMode } = useAppI18n();

  return (
    <form
      onSubmit={onSubmit}
      className="rounded-lg border border-slate-200 bg-slate-50 p-4"
    >
      <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-600">
        {t("admin.projects.createTitle")}
      </h3>
      {isError && (
        <div className="mt-2 rounded border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {error instanceof ApiError
            ? error.message
            : t("admin.projects.createFailed")}
        </div>
      )}
      <div className="mt-3 grid gap-3 sm:grid-cols-4">
        <div>
          <label className="block text-xs font-medium text-slate-700">
            {t("admin.projects.projectKey")}{" "}
            <span className="text-rose-500">*</span>
          </label>
          <input
            type="text"
            required
            value={projectKey}
            onChange={(e) => onKeyChange(e.target.value)}
            placeholder={t("admin.projects.projectKeyPlaceholder")}
            className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-3 font-mono text-xs text-slate-900"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-slate-700">
            {t("admin.projects.displayName")}{" "}
            <span className="text-rose-500">*</span>
          </label>
          <input
            type="text"
            required
            value={projectName}
            onChange={(e) => onNameChange(e.target.value)}
            placeholder={t("admin.projects.displayNamePlaceholder")}
            className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-slate-700">
            {t("admin.projects.aiTriageMode")}
          </label>
          <select
            value={triageMode}
            onChange={(e) =>
              onTriageModeChange(e.target.value as AITriageMode | "")
            }
            className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-900"
          >
            <option value="">{t("admin.projects.inheritGlobal")}</option>
            {AI_TRIAGE_MODES.map((m) => (
              <option key={m} value={m}>
                {formatTriageMode(m)}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-xs font-medium text-slate-700">
            {t("admin.projects.sessionIdLabel")}
          </label>
          <input
            type="text"
            maxLength={64}
            value={sessionIdLabel}
            onChange={(e) => onSessionIdLabelChange(e.target.value)}
            placeholder={t("admin.projects.sessionIdLabelPlaceholder")}
            className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900"
          />
        </div>
        <div className="flex items-end">
          <button
            type="submit"
            disabled={isPending || !projectKey.trim() || !projectName.trim()}
            className="inline-flex h-9 w-full items-center justify-center gap-1.5 rounded-md bg-indigo-600 px-3 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
          >
            <Plus className="h-4 w-4" />
            {isPending
              ? t("common.creating")
              : t("admin.projects.createButton")}
          </button>
        </div>
      </div>
    </form>
  );
}
