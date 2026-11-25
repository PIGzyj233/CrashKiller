import { Eye, EyeOff, KeyRound } from "lucide-react";
import type { ProjectRead } from "../../api/types";
import { useAppI18n } from "../../i18n";

export interface ProjectAuthFieldsProps {
  projects: ProjectRead[];
  selectedProjectKey: string;
  appVersion: string;
  apiKey: string;
  rememberApiKey: boolean;
  showApiKey: boolean;
  isApiKeyPrefilled: boolean;
  isUploading: boolean;
  onSelectProjectKey: (key: string) => void;
  onAppVersionChange: (value: string) => void;
  onApiKeyChange: (value: string) => void;
  onToggleShowApiKey: () => void;
  onRememberApiKeyChange: (checked: boolean) => void;
}

export function ProjectAuthFields({
  projects,
  selectedProjectKey,
  appVersion,
  apiKey,
  rememberApiKey,
  showApiKey,
  isApiKeyPrefilled,
  isUploading,
  onSelectProjectKey,
  onAppVersionChange,
  onApiKeyChange,
  onToggleShowApiKey,
  onRememberApiKeyChange,
}: ProjectAuthFieldsProps) {
  const { t } = useAppI18n();

  return (
    <>
      {/* Project + App Version */}
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <label
            htmlFor="upload-project-select"
            className="mb-1 block text-xs font-medium text-slate-700"
          >
            {t("upload.projectLabel")} <span className="text-rose-500">*</span>
          </label>
          <select
            id="upload-project-select"
            value={selectedProjectKey}
            disabled={isUploading || projects.length === 0}
            onChange={(e) => onSelectProjectKey(e.target.value)}
            className="h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 text-xs text-slate-800 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:bg-slate-100"
          >
            {projects.length === 0 ? (
              <option value="">{t("upload.noProjects")}</option>
            ) : (
              projects.map((p) => (
                <option key={p.id} value={p.key}>
                  {p.name} ({p.key})
                </option>
              ))
            )}
          </select>
        </div>

        <div>
          <label
            htmlFor="upload-app-version"
            className="mb-1 block text-xs font-medium text-slate-700"
          >
            {t("upload.appVersionLabel")}{" "}
            <span className="text-rose-500">*</span>
          </label>
          <input
            id="upload-app-version"
            type="text"
            value={appVersion}
            disabled={isUploading}
            onChange={(e) => onAppVersionChange(e.target.value)}
            placeholder={t("upload.appVersionPlaceholder")}
            className="h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 font-mono text-xs text-slate-800 placeholder:font-sans placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:bg-slate-100"
          />
        </div>
      </div>

      {/* Project API Key */}
      <div>
        <div className="mb-1 flex items-center justify-between">
          <label
            htmlFor="upload-api-key"
            className="flex items-center gap-1.5 text-xs font-medium text-slate-700"
          >
            <KeyRound className="h-3.5 w-3.5 text-slate-400" />
            <span>
              {t("upload.apiKeyLabel")} <span className="text-rose-500">*</span>
            </span>
          </label>
          {isApiKeyPrefilled && (
            <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700">
              {t("upload.apiKeyPrefilled")}
            </span>
          )}
        </div>

        <div className="relative">
          <input
            id="upload-api-key"
            type={showApiKey ? "text" : "password"}
            value={apiKey}
            disabled={isUploading}
            onChange={(e) => onApiKeyChange(e.target.value)}
            placeholder={t("upload.apiKeyPlaceholder")}
            className="h-9 w-full rounded-md border border-slate-300 bg-white pl-2.5 pr-9 font-mono text-xs text-slate-800 placeholder:font-sans placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:bg-slate-100"
          />
          <button
            type="button"
            onClick={onToggleShowApiKey}
            aria-label={
              showApiKey ? t("upload.hideApiKey") : t("upload.showApiKey")
            }
            className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600"
          >
            {showApiKey ? (
              <EyeOff className="h-4 w-4" />
            ) : (
              <Eye className="h-4 w-4" />
            )}
          </button>
        </div>

        <label className="mt-1.5 inline-flex cursor-pointer items-center gap-1.5 text-xs text-slate-600">
          <input
            type="checkbox"
            checked={rememberApiKey}
            disabled={isUploading}
            onChange={(e) => onRememberApiKeyChange(e.target.checked)}
            className="h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
          />
          <span>{t("upload.rememberApiKey")}</span>
        </label>
      </div>
    </>
  );
}
