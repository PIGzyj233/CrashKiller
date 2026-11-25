import type { FormEvent } from "react";
import {
  Check,
  CheckCircle2,
  Copy,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  Save,
} from "lucide-react";
import { ApiError } from "../../api/client";
import type { AITriageMode, ProjectRead } from "../../api/types";
import { useAppI18n } from "../../i18n";
import { AI_TRIAGE_MODES, maskProjectApiKey } from "./helpers";

export interface ProjectEditorValues {
  name: string;
  aiTriageMode: AITriageMode | "";
  sessionIdLabel: string;
  noiseModulesText: string;
  noiseFunctionsText: string;
  customHandlersText: string;
  logRetentionDays: string;
  dumpRetentionDays: string;
}

interface ProjectEditorFormProps {
  project: ProjectRead;
  values: ProjectEditorValues;
  projectSavedMsg: string | null;
  isSaving: boolean;
  saveError: unknown;
  apiKeyData: string | null | undefined;
  isApiKeyLoading: boolean;
  isApiKeyError: boolean;
  apiKeyError: unknown;
  showEditorApiKey: boolean;
  copiedProjectKeyTarget: string | null;
  projectKeyError: { projectId: string; message: string } | null;
  onFieldChange: <K extends keyof ProjectEditorValues>(
    field: K,
    value: ProjectEditorValues[K],
  ) => void;
  onToggleShowEditorApiKey: () => void;
  onCopyApiKey: (proj: ProjectRead, targetKey: string) => void;
  onSubmit: (e: FormEvent) => void;
}

export function ProjectEditorForm({
  project,
  values,
  projectSavedMsg,
  isSaving,
  saveError,
  apiKeyData,
  isApiKeyLoading,
  isApiKeyError,
  apiKeyError,
  showEditorApiKey,
  copiedProjectKeyTarget,
  projectKeyError,
  onFieldChange,
  onToggleShowEditorApiKey,
  onCopyApiKey,
  onSubmit,
}: ProjectEditorFormProps) {
  const { t, formatTriageMode } = useAppI18n();

  return (
    <form
      onSubmit={onSubmit}
      className="space-y-4 rounded-lg border border-slate-200 p-4 lg:col-span-7"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-slate-900">
          {t("admin.projects.editorTitle")}
          <code className="font-mono text-indigo-600">{project.key}</code>
        </h3>
        {projectSavedMsg && (
          <span className="inline-flex items-center gap-1 text-xs font-medium text-emerald-700">
            <CheckCircle2 className="h-3.5 w-3.5" />
            {projectSavedMsg}
          </span>
        )}
      </div>

      {Boolean(saveError) && (
        <div className="rounded border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {saveError instanceof ApiError
            ? saveError.message
            : t("admin.projects.saveFailed")}
        </div>
      )}

      {/* Project API Key Viewer & Copier */}
      <div className="rounded-lg border border-slate-200 bg-slate-50/80 p-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-1.5 text-xs font-medium text-slate-700">
            <KeyRound className="h-3.5 w-3.5 text-indigo-600" />
            <span>{t("admin.projects.apiKeyLabel")}</span>
          </div>
          <span className="text-[11px] text-slate-500">
            {t("admin.projects.apiKeyHeaderHint")}
          </span>
        </div>

        {isApiKeyLoading ? (
          <div className="mt-2 flex items-center gap-1.5 text-xs text-slate-500">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            <span>{t("admin.projects.loadingKey")}</span>
          </div>
        ) : isApiKeyError ? (
          <div className="mt-2 rounded border border-rose-200 bg-rose-50 px-2.5 py-1.5 text-xs text-rose-700">
            {apiKeyError instanceof ApiError
              ? apiKeyError.message
              : t("admin.projects.loadKeyFailed")}
          </div>
        ) : apiKeyData ? (
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <input
              type="text"
              readOnly
              aria-label={t("admin.projects.apiKeyLabel")}
              data-testid="editor-project-api-key"
              value={
                showEditorApiKey ? apiKeyData : maskProjectApiKey(apiKeyData)
              }
              onFocus={(e) => {
                if (showEditorApiKey) {
                  e.currentTarget.select();
                }
              }}
              className="h-9 min-w-[200px] flex-1 rounded-md border border-slate-300 bg-white px-2.5 font-mono text-xs text-slate-900 focus:border-indigo-500 focus:outline-none"
            />
            <button
              type="button"
              onClick={onToggleShowEditorApiKey}
              className="inline-flex h-9 items-center gap-1.5 rounded-md border border-slate-300 bg-white px-2.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
            >
              {showEditorApiKey ? (
                <>
                  <EyeOff className="h-3.5 w-3.5" />
                  {t("admin.projects.hideKey")}
                </>
              ) : (
                <>
                  <Eye className="h-3.5 w-3.5" />
                  {t("admin.projects.showKey")}
                </>
              )}
            </button>
            <button
              type="button"
              onClick={() => onCopyApiKey(project, `editor:${project.id}`)}
              className="inline-flex h-9 items-center gap-1.5 rounded-md bg-indigo-600 px-3 text-xs font-medium text-white hover:bg-indigo-700"
            >
              {copiedProjectKeyTarget === `editor:${project.id}` ? (
                <>
                  <Check className="h-3.5 w-3.5" />
                  {t("common.copied")}
                </>
              ) : (
                <>
                  <Copy className="h-3.5 w-3.5" />
                  {t("admin.projects.copyKey")}
                </>
              )}
            </button>
          </div>
        ) : (
          <div className="mt-2 rounded border border-amber-200 bg-amber-50 px-2.5 py-2 text-xs text-amber-800">
            {t("admin.projects.keyUnavailable")}
          </div>
        )}

        {projectKeyError?.projectId === project.id && (
          <div className="mt-2 rounded border border-rose-200 bg-rose-50 px-2.5 py-1.5 text-xs text-rose-700">
            {projectKeyError.message}
          </div>
        )}
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <label className="block text-xs font-medium text-slate-700">
            {t("admin.projects.projectName")}
          </label>
          <input
            type="text"
            required
            value={values.name}
            onChange={(e) => onFieldChange("name", e.target.value)}
            className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 text-sm text-slate-900"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-slate-700">
            {t("admin.projects.triageModeOverride")}
          </label>
          <select
            value={values.aiTriageMode}
            onChange={(e) =>
              onFieldChange(
                "aiTriageMode",
                e.target.value as AITriageMode | "",
              )
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
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <div>
          <label className="block text-xs font-medium text-slate-700">
            {t("admin.projects.logRetentionOverride")}
          </label>
          <input
            type="number"
            min={1}
            value={values.logRetentionDays}
            onChange={(e) => onFieldChange("logRetentionDays", e.target.value)}
            placeholder={t("admin.projects.inheritGlobalPlaceholder")}
            className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 font-mono text-sm text-slate-900"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-slate-700">
            {t("admin.projects.dumpRetentionOverride")}
          </label>
          <input
            type="number"
            min={1}
            value={values.dumpRetentionDays}
            onChange={(e) => onFieldChange("dumpRetentionDays", e.target.value)}
            placeholder={t("admin.projects.inheritGlobalPlaceholder")}
            className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 font-mono text-sm text-slate-900"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-slate-700">
            {t("admin.projects.sessionIdLabel")}
          </label>
          <input
            type="text"
            maxLength={64}
            value={values.sessionIdLabel}
            onChange={(e) => onFieldChange("sessionIdLabel", e.target.value)}
            placeholder={t("admin.projects.sessionIdLabelPlaceholder")}
            className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 text-sm text-slate-900"
          />
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <div>
          <label className="block text-xs font-medium text-slate-700">
            {t("admin.projects.noiseModules")}
          </label>
          <textarea
            rows={3}
            value={values.noiseModulesText}
            onChange={(e) => onFieldChange("noiseModulesText", e.target.value)}
            placeholder={"ntdll.dll\nkernel32.dll"}
            className="mt-1 w-full rounded-md border border-slate-300 p-2 font-mono text-xs text-slate-900"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-slate-700">
            {t("admin.projects.noiseFunctions")}
          </label>
          <textarea
            rows={3}
            value={values.noiseFunctionsText}
            onChange={(e) =>
              onFieldChange("noiseFunctionsText", e.target.value)
            }
            placeholder={"abort\nraise"}
            className="mt-1 w-full rounded-md border border-slate-300 p-2 font-mono text-xs text-slate-900"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-slate-700">
            {t("admin.projects.customCrashHandlers")}
          </label>
          <textarea
            rows={3}
            value={values.customHandlersText}
            onChange={(e) =>
              onFieldChange("customHandlersText", e.target.value)
            }
            placeholder="MyCrashHandler"
            className="mt-1 w-full rounded-md border border-slate-300 p-2 font-mono text-xs text-slate-900"
          />
        </div>
      </div>

      <div className="flex justify-end">
        <button
          type="submit"
          disabled={isSaving || !values.name.trim()}
          className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
        >
          <Save className="h-3.5 w-3.5" />
          {isSaving
            ? t("common.saving")
            : t("admin.projects.saveProjectButton")}
        </button>
      </div>
    </form>
  );
}
