import { Check, Copy, Eye, EyeOff, Loader2, RefreshCw } from "lucide-react";
import { getStoredProjectApiKey } from "../../api/client";
import type { ProjectRead } from "../../api/types";
import { useAppI18n } from "../../i18n";

interface ProjectListPanelProps {
  projects: ProjectRead[];
  editingProjectId: string;
  editingProjectKeyData: string | null | undefined;
  isEditingProjectKeyLoading: boolean;
  resolvedProjectKeys: Record<string, string | null>;
  visibleCardKeyIds: Record<string, boolean>;
  loadingCardKeyId: string | null;
  copiedProjectKeyTarget: string | null;
  projectKeyError: { projectId: string; message: string } | null;
  confirmRotateProjectId: string | null;
  isRotatePending: boolean;
  onSelectProject: (projectId: string) => void;
  onToggleCardApiKey: (proj: ProjectRead) => void;
  onCopyProjectApiKey: (proj: ProjectRead, targetKey: string) => void;
  onRequestRotate: (projectId: string) => void;
  onCancelRotate: () => void;
  onConfirmRotate: (projectId: string) => void;
}

export function ProjectListPanel({
  projects,
  editingProjectId,
  editingProjectKeyData,
  isEditingProjectKeyLoading,
  resolvedProjectKeys,
  visibleCardKeyIds,
  loadingCardKeyId,
  copiedProjectKeyTarget,
  projectKeyError,
  confirmRotateProjectId,
  isRotatePending,
  onSelectProject,
  onToggleCardApiKey,
  onCopyProjectApiKey,
  onRequestRotate,
  onCancelRotate,
  onConfirmRotate,
}: ProjectListPanelProps) {
  const { t } = useAppI18n();

  return (
    <div className="overflow-hidden rounded-lg border border-slate-200 lg:col-span-5">
      <div className="divide-y divide-slate-200">
        {projects.map((proj) => {
          const isSelected = proj.id === editingProjectId;
          const resolvedKey =
            isSelected && editingProjectKeyData !== undefined
              ? editingProjectKeyData
              : resolvedProjectKeys[proj.id];
          const hasStoredKey = Boolean(
            resolvedKey || getStoredProjectApiKey(proj.key),
          );
          const isConfirmingRotate = confirmRotateProjectId === proj.id;
          const isCardKeyVisible = Boolean(visibleCardKeyIds[proj.id]);
          const isCardLoading =
            loadingCardKeyId === proj.id ||
            (isSelected && isEditingProjectKeyLoading);
          const cardCopyTarget = `card:${proj.id}`;
          const isCardCopied = copiedProjectKeyTarget === cardCopyTarget;
          const cardErr =
            projectKeyError?.projectId === proj.id
              ? projectKeyError.message
              : null;

          return (
            <div
              key={proj.id}
              className={`p-3.5 transition-colors ${
                isSelected ? "bg-indigo-50/50" : "bg-white"
              }`}
            >
              <div className="flex items-start justify-between gap-2">
                <div>
                  <div className="font-medium text-slate-900">{proj.name}</div>
                  <div className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-slate-500">
                    <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-slate-700">
                      {proj.key}
                    </code>
                    {hasStoredKey && (
                      <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10px] font-medium text-emerald-700">
                        {t("admin.projects.keyInBrowser")}
                      </span>
                    )}
                  </div>
                </div>

                <button
                  type="button"
                  onClick={() => onSelectProject(proj.id)}
                  className={`rounded-md border px-2.5 py-1 text-xs font-medium ${
                    isSelected
                      ? "border-indigo-500 bg-indigo-600 text-white"
                      : "border-slate-300 bg-white text-slate-700 hover:bg-slate-50"
                  }`}
                >
                  {isSelected ? t("common.editing") : t("common.edit")}
                </button>
              </div>

              {isCardKeyVisible && (
                <div className="mt-2.5 rounded-md border border-slate-200 bg-slate-50 p-2 text-xs">
                  {isCardLoading ? (
                    <div className="flex items-center gap-1.5 text-slate-500">
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      <span>{t("admin.projects.loadingKey")}</span>
                    </div>
                  ) : resolvedKey ? (
                    <div className="flex items-center gap-1.5">
                      <code
                        data-testid={`project-card-api-key-${proj.key}`}
                        className="flex-1 select-all break-all rounded border border-slate-200 bg-white px-2 py-1 font-mono text-[11px] text-slate-900"
                      >
                        {resolvedKey}
                      </code>
                      <button
                        type="button"
                        onClick={() =>
                          onCopyProjectApiKey(proj, cardCopyTarget)
                        }
                        className="inline-flex shrink-0 items-center gap-1 rounded border border-slate-300 bg-white px-2 py-1 text-[11px] font-medium text-slate-700 hover:bg-slate-100"
                      >
                        {isCardCopied ? (
                          <>
                            <Check className="h-3 w-3 text-emerald-600" />
                            {t("common.copied")}
                          </>
                        ) : (
                          <>
                            <Copy className="h-3 w-3" />
                            {t("common.copy")}
                          </>
                        )}
                      </button>
                    </div>
                  ) : (
                    <p className="text-[11px] text-amber-800">
                      {t("admin.projects.keyUnavailable")}
                    </p>
                  )}
                </div>
              )}

              {cardErr && (
                <div className="mt-2 rounded border border-rose-200 bg-rose-50 px-2 py-1 text-[11px] text-rose-700">
                  {cardErr}
                </div>
              )}

              <div className="mt-2.5 flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 pt-2 text-xs">
                {isConfirmingRotate ? (
                  <div className="flex w-full items-center justify-between gap-2">
                    <span className="font-medium text-amber-800">
                      {t("admin.projects.confirmRotatePrompt")}
                    </span>
                    <div className="flex items-center gap-1.5">
                      <button
                        type="button"
                        onClick={onCancelRotate}
                        className="rounded border border-slate-300 bg-white px-2 py-0.5 text-slate-700"
                      >
                        {t("common.cancel")}
                      </button>
                      <button
                        type="button"
                        disabled={isRotatePending}
                        onClick={() => onConfirmRotate(proj.id)}
                        className="rounded bg-amber-600 px-2 py-0.5 font-medium text-white hover:bg-amber-700"
                      >
                        {t("admin.projects.confirmRotate")}
                      </button>
                    </div>
                  </div>
                ) : (
                  <>
                    <div className="flex flex-wrap items-center gap-3">
                      <button
                        type="button"
                        disabled={isCardLoading}
                        onClick={() => onToggleCardApiKey(proj)}
                        className="inline-flex items-center gap-1 font-medium text-indigo-600 hover:text-indigo-800 disabled:opacity-50"
                      >
                        {isCardKeyVisible ? (
                          <>
                            <EyeOff className="h-3.5 w-3.5" />
                            {t("admin.projects.hideKey")}
                          </>
                        ) : (
                          <>
                            <Eye className="h-3.5 w-3.5" />
                            {t("admin.projects.viewKey")}
                          </>
                        )}
                      </button>
                      <button
                        type="button"
                        disabled={isCardLoading}
                        onClick={() =>
                          onCopyProjectApiKey(proj, cardCopyTarget)
                        }
                        className="inline-flex items-center gap-1 font-medium text-slate-600 hover:text-indigo-600 disabled:opacity-50"
                      >
                        {isCardCopied ? (
                          <>
                            <Check className="h-3.5 w-3.5 text-emerald-600" />
                            <span className="text-emerald-700">
                              {t("common.copied")}
                            </span>
                          </>
                        ) : (
                          <>
                            <Copy className="h-3.5 w-3.5" />
                            {t("admin.projects.copyKey")}
                          </>
                        )}
                      </button>
                    </div>
                    <button
                      type="button"
                      onClick={() => onRequestRotate(proj.id)}
                      className="inline-flex items-center gap-1 font-medium text-slate-600 hover:text-amber-700"
                    >
                      <RefreshCw className="h-3 w-3" />
                      {t("admin.projects.rotateKey")}
                    </button>
                  </>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
