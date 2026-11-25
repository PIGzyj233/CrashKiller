import { Link } from "react-router-dom";
import {
  AlertCircle,
  Check,
  CheckCircle2,
  CloudDownload,
  Copy,
  Loader2,
  RefreshCw,
  Upload,
} from "lucide-react";
import { ApiError } from "../../api/client";
import type {
  MissingSymbolQueueItem,
  PublicSymbolSyncRequest,
  ResymbolicateRequest,
  SymbolScope,
} from "../../api/types";
import type { UploadSymbolTargetHint } from "../../components/UploadModal";
import { useAppI18n } from "../../i18n";
import {
  buildMsSyncTarget,
  canMsSyncMissingItem,
} from "../../utils/symbolIdentity";
import { missingRowKey } from "./helpers";

interface MissingSymbolsTabProps {
  selectedProjectId: string;
  sortedMissingItems: MissingSymbolQueueItem[];
  selectableMissingItems: MissingSymbolQueueItem[];
  selectedMissingItems: MissingSymbolQueueItem[];
  selectedMissingKeys: Set<string>;
  uncachedWindowsMissingCount: number;
  missingTotalCount: number;
  isPending: boolean;
  isFetching: boolean;
  isError: boolean;
  error: unknown;
  isSyncRunning: boolean;
  activeSyncKey: string | null;
  isResymPending: boolean;
  activeResymDebugId: string | null;
  copiedText: string | null;
  onRefetch: () => void;
  onSelectAllChange: (checked: boolean) => void;
  onToggleRowSelection: (rowKey: string, checked: boolean) => void;
  onStartSync: (key: string, payload: PublicSymbolSyncRequest) => void;
  onOpenUploadModal: (
    scope: SymbolScope,
    hint?: UploadSymbolTargetHint | null,
  ) => void;
  onTriggerResym: (payload: ResymbolicateRequest) => void;
  onCopy: (text: string) => void;
}

export function MissingSymbolsTab({
  selectedProjectId,
  sortedMissingItems,
  selectableMissingItems,
  selectedMissingItems,
  selectedMissingKeys,
  uncachedWindowsMissingCount,
  missingTotalCount,
  isPending,
  isFetching,
  isError,
  error,
  isSyncRunning,
  activeSyncKey,
  isResymPending,
  activeResymDebugId,
  copiedText,
  onRefetch,
  onSelectAllChange,
  onToggleRowSelection,
  onStartSync,
  onOpenUploadModal,
  onTriggerResym,
  onCopy,
}: MissingSymbolsTabProps) {
  const { t, formatDateTime } = useAppI18n();

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white p-3.5 shadow-sm">
        <div className="flex items-center gap-2 text-xs text-slate-600">
          <AlertCircle className="h-4 w-4 shrink-0 text-amber-500" />
          <span>{t("symbols.missingHint")}</span>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={onRefetch}
            disabled={isFetching}
            className="inline-flex items-center gap-1.5 rounded-md border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
          >
            <RefreshCw
              className={`h-3.5 w-3.5 ${isFetching ? "animate-spin" : ""}`}
            />
            {t("common.refresh")}
          </button>

          <button
            type="button"
            disabled={sortedMissingItems.length === 0 || isSyncRunning}
            onClick={() => {
              if (selectedMissingItems.length > 0) {
                onStartSync("__SELECTED__", {
                  mode: "targets",
                  targets: selectedMissingItems.map((it) =>
                    buildMsSyncTarget(it),
                  ),
                  project_id: selectedProjectId || undefined,
                  force: true,
                  auto_resymbolicate: true,
                });
              } else {
                onStartSync("__MISSING_WINDOWS__", {
                  mode: "missing_windows",
                  project_id: selectedProjectId || undefined,
                  force: true,
                  auto_resymbolicate: true,
                });
              }
            }}
            className="inline-flex items-center gap-1.5 rounded-md border border-sky-300 bg-sky-50 px-3 py-1.5 text-xs font-medium text-sky-800 hover:bg-sky-100 disabled:opacity-50"
          >
            {isSyncRunning &&
            (activeSyncKey === "__SELECTED__" ||
              activeSyncKey === "__MISSING_WINDOWS__") ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <CloudDownload className="h-3.5 w-3.5" />
            )}
            {selectedMissingItems.length > 0
              ? t("symbols.syncSelectedBtn", {
                  count: selectedMissingItems.length,
                })
              : uncachedWindowsMissingCount > 0
                ? t("symbols.syncSystemMissingBtnCount", {
                    count: uncachedWindowsMissingCount,
                  })
                : t("symbols.syncSystemMissingBtn")}
          </button>

          <button
            type="button"
            onClick={() => onOpenUploadModal("public")}
            className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
          >
            <Upload className="h-3.5 w-3.5" />
            {t("symbols.uploadPublicSymbol")}
          </button>

          <button
            type="button"
            disabled={sortedMissingItems.length === 0 || isResymPending}
            onClick={() =>
              onTriggerResym({
                project_id: selectedProjectId || undefined,
                missing_only: true,
              })
            }
            className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
          >
            {isResymPending && activeResymDebugId === "__ALL__" ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <RefreshCw className="h-3.5 w-3.5" />
            )}
            {t("symbols.resymAllMissing")}
          </button>
        </div>
      </div>

      {missingTotalCount > sortedMissingItems.length && (
        <div className="flex items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 px-4 py-2.5 text-xs text-slate-600">
          <AlertCircle className="h-4 w-4 shrink-0 text-slate-500" />
          <span>
            {t("symbols.missingQueueTruncatedNote", {
              shown: sortedMissingItems.length,
              total: missingTotalCount,
            })}
          </span>
        </div>
      )}

      {isError && (
        <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          <span>
            {error instanceof ApiError
              ? error.message
              : t("symbols.loadMissingFailed")}
          </span>
          <button
            type="button"
            onClick={onRefetch}
            className="rounded border border-rose-300 bg-white px-2.5 py-1 text-xs font-medium text-rose-700"
          >
            {t("common.retry")}
          </button>
        </div>
      )}

      <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-200 text-left text-sm">
            <thead className="bg-slate-50 text-xs font-medium uppercase tracking-wider text-slate-500">
              <tr>
                <th className="w-10 px-3 py-3">
                  <input
                    type="checkbox"
                    aria-label={t("symbols.selectAllMissingAria")}
                    disabled={
                      selectableMissingItems.length === 0 || isSyncRunning
                    }
                    checked={
                      selectableMissingItems.length > 0 &&
                      selectedMissingItems.length ===
                        selectableMissingItems.length
                    }
                    onChange={(e) => onSelectAllChange(e.target.checked)}
                    className="h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500 disabled:opacity-40"
                  />
                </th>
                <th className="px-4 py-3">
                  {t("symbols.colModuleAndDebug")}
                </th>
                <th className="px-4 py-3">{t("symbols.colDebugId")}</th>
                <th className="px-4 py-3">{t("symbols.colCodeFileId")}</th>
                <th className="px-4 py-3">{t("symbols.colCrashes")}</th>
                <th className="px-4 py-3">
                  {t("symbols.colAffectedCrashes")}
                </th>
                <th className="px-4 py-3 text-right">
                  {t("symbols.colActions")}
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {isPending ? (
                <tr>
                  <td
                    colSpan={7}
                    className="px-4 py-10 text-center text-sm text-slate-500"
                  >
                    {t("symbols.loadingMissing")}
                  </td>
                </tr>
              ) : sortedMissingItems.length === 0 ? (
                <tr>
                  <td
                    colSpan={7}
                    className="px-4 py-12 text-center text-sm text-slate-500"
                  >
                    <CheckCircle2 className="mx-auto mb-2 h-6 w-6 text-emerald-500" />
                    {t("symbols.emptyMissing")}
                  </td>
                </tr>
              ) : (
                sortedMissingItems.map((item: MissingSymbolQueueItem) => {
                  const rowKey = missingRowKey(item);
                  const canMsSync = canMsSyncMissingItem(item);
                  const isRowPending =
                    isResymPending && activeResymDebugId === item.debug_id;
                  const isRowSyncing =
                    isSyncRunning && activeSyncKey === rowKey;
                  const visibleCrashIds = item.affected_crash_ids.slice(0, 4);
                  const extraCrashes =
                    item.affected_crash_ids.length - visibleCrashIds.length;

                  return (
                    <tr key={rowKey} className="hover:bg-slate-50">
                      <td className="px-3 py-3">
                        <input
                          type="checkbox"
                          disabled={!canMsSync || isSyncRunning}
                          checked={selectedMissingKeys.has(rowKey)}
                          aria-label={t("symbols.selectMissingRowAria", {
                            file: item.debug_file || item.module_name,
                          })}
                          onChange={(e) =>
                            onToggleRowSelection(rowKey, e.target.checked)
                          }
                          className="h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500 disabled:opacity-40"
                        />
                      </td>

                      <td className="px-4 py-3">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="font-medium text-slate-900">
                            {item.module_name || item.debug_file}
                          </span>
                          {item.is_windows_system && (
                            <span className="rounded-full border border-sky-200 bg-sky-50 px-2 py-0.5 text-[10px] font-medium text-sky-700">
                              {t("symbols.badgeWindowsSystem")}
                            </span>
                          )}
                          {item.is_available_locally && (
                            <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10px] font-medium text-emerald-700">
                              {t("symbols.badgeAvailableLocally")}
                            </span>
                          )}
                          {item.msdl_negative_cached &&
                            !item.is_available_locally && (
                              <span
                                title={item.msdl_last_reason ?? undefined}
                                className="rounded-full border border-slate-200 bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-600"
                              >
                                {t("symbols.badgeNegativeCached")}
                              </span>
                            )}
                        </div>
                        <div className="font-mono text-xs text-slate-500">
                          {item.debug_file}
                        </div>
                      </td>

                      <td className="px-4 py-3">
                        <div className="flex items-center gap-1.5">
                          <code className="font-mono text-xs text-slate-800">
                            {item.debug_id}
                          </code>
                          <button
                            type="button"
                            onClick={() => onCopy(item.debug_id)}
                            className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
                            title={t("symbols.copyDebugId")}
                          >
                            {copiedText === item.debug_id ? (
                              <Check className="h-3.5 w-3.5 text-emerald-600" />
                            ) : (
                              <Copy className="h-3.5 w-3.5" />
                            )}
                          </button>
                        </div>
                      </td>

                      <td className="px-4 py-3 font-mono text-xs text-slate-600">
                        <div>{item.code_file ?? "—"}</div>
                        <div className="text-[11px] text-slate-400">
                          {item.code_id ?? "—"}
                        </div>
                      </td>

                      <td className="px-4 py-3">
                        <span className="inline-flex rounded-full border border-amber-200 bg-amber-50 px-2.5 py-0.5 font-mono text-xs font-semibold text-amber-800">
                          {item.crash_count}
                        </span>
                        <div className="mt-1 text-[11px] text-slate-500">
                          {t("symbols.latestLabel", {
                            time: formatDateTime(item.latest_crash_at),
                          })}
                        </div>
                      </td>

                      <td className="px-4 py-3 text-xs">
                        {visibleCrashIds.length === 0 ? (
                          <span className="text-slate-400">—</span>
                        ) : (
                          <div className="flex flex-wrap gap-1.5">
                            {visibleCrashIds.map((cid) => (
                              <Link
                                key={cid}
                                to={`/crashes/${encodeURIComponent(cid)}`}
                                className="rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5 font-mono text-[11px] text-indigo-600 hover:border-indigo-300 hover:bg-indigo-50"
                              >
                                {cid.slice(0, 8)}
                              </Link>
                            ))}
                            {extraCrashes > 0 && (
                              <span className="text-[11px] text-slate-500">
                                {t("symbols.moreCrashes", {
                                  count: extraCrashes,
                                })}
                              </span>
                            )}
                          </div>
                        )}
                      </td>

                      <td className="px-4 py-3 text-right">
                        <div className="inline-flex flex-wrap items-center justify-end gap-1.5">
                          {canMsSync && (
                            <button
                              type="button"
                              disabled={isSyncRunning}
                              onClick={() =>
                                onStartSync(rowKey, {
                                  mode: "targets",
                                  targets: [buildMsSyncTarget(item)],
                                  project_id: selectedProjectId || undefined,
                                  force: true,
                                  auto_resymbolicate: true,
                                })
                              }
                              className="inline-flex items-center gap-1 rounded-md border border-sky-300 bg-sky-50 px-2.5 py-1 text-xs font-medium text-sky-800 hover:bg-sky-100 disabled:opacity-50"
                            >
                              {isRowSyncing ? (
                                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                              ) : (
                                <CloudDownload className="h-3.5 w-3.5" />
                              )}
                              {t("symbols.syncRowBtn")}
                            </button>
                          )}
                          <button
                            type="button"
                            onClick={() =>
                              onOpenUploadModal(
                                item.is_windows_system ? "public" : "project",
                                {
                                  module_name: item.module_name,
                                  debug_file: item.debug_file,
                                  debug_id: item.debug_id,
                                },
                              )
                            }
                            className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
                          >
                            <Upload className="h-3.5 w-3.5" />
                            {t("symbols.uploadBtn")}
                          </button>
                          <button
                            type="button"
                            disabled={isResymPending}
                            onClick={() =>
                              onTriggerResym({
                                project_id: selectedProjectId || undefined,
                                debug_id: item.debug_id,
                                crash_ids:
                                  item.affected_crash_ids.length > 0
                                    ? item.affected_crash_ids
                                    : undefined,
                              })
                            }
                            className="inline-flex items-center gap-1 rounded-md bg-indigo-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
                          >
                            {isRowPending ? (
                              <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            ) : (
                              <RefreshCw className="h-3.5 w-3.5" />
                            )}
                            {t("symbols.resymBtn")}
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
