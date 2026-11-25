import { Link } from "react-router-dom";
import {
  AlertTriangle,
  CheckCircle2,
  Loader2,
  RefreshCw,
  Square,
  X,
} from "lucide-react";
import { ApiError } from "../../api/client";
import type {
  PublicSymbolSyncItemResult,
  PublicSymbolSyncItemStatus,
  PublicSymbolSyncResponse,
  ResymbolicateRequest,
} from "../../api/types";
import { useAppI18n } from "../../i18n";
import type {
  CancelledSyncInfo,
  LiveSyncProgressState,
  ResymFeedbackState,
  TrackedResymState,
} from "./helpers";

interface GlobalStatusBannersProps {
  isSyncRunning: boolean;
  syncProgress: LiveSyncProgressState | null;
  syncCancelledInfo: CancelledSyncInfo | null;
  syncFeedback: PublicSymbolSyncResponse | null;
  syncError: string | null;
  trackedResym: TrackedResymState | null;
  resymFeedback: ResymFeedbackState | null;
  isResymPending: boolean;
  isResymError: boolean;
  resymError: unknown;
  onCancelSync: () => void;
  onDismissCancelled: () => void;
  onDismissSyncFeedback: () => void;
  onDismissSyncError: () => void;
  onDismissTrackedResym: () => void;
  onDismissResymFeedback: () => void;
  onResetResymError: () => void;
  onTriggerResym: (payload: ResymbolicateRequest) => void;
}

export function GlobalStatusBanners({
  isSyncRunning,
  syncProgress,
  syncCancelledInfo,
  syncFeedback,
  syncError,
  trackedResym,
  resymFeedback,
  isResymPending,
  isResymError,
  resymError,
  onCancelSync,
  onDismissCancelled,
  onDismissSyncFeedback,
  onDismissSyncError,
  onDismissTrackedResym,
  onDismissResymFeedback,
  onResetResymError,
  onTriggerResym,
}: GlobalStatusBannersProps) {
  const { t } = useAppI18n();

  const hasGlobalStatusBanner = Boolean(
    (isSyncRunning && syncProgress) ||
      (!isSyncRunning && syncCancelledInfo) ||
      syncFeedback ||
      syncError ||
      trackedResym ||
      resymFeedback ||
      isResymError,
  );

  if (!hasGlobalStatusBanner) {
    return null;
  }

  const renderSyncStatusLabel = (status: PublicSymbolSyncItemStatus) => {
    switch (status) {
      case "imported":
        return t("symbols.syncStatusImported");
      case "upgraded":
        return t("symbols.syncStatusUpgraded");
      case "cached_hit":
        return t("symbols.syncStatusCachedHit");
      case "not_found":
        return t("symbols.syncStatusNotFound");
      case "network_error":
        return t("symbols.syncStatusNetworkError");
      case "skipped":
        return t("symbols.syncStatusSkipped");
      case "failed":
        return t("symbols.syncStatusFailed");
    }
  };

  const renderSyncResultItem = (
    item: PublicSymbolSyncItemResult,
    idx: number,
  ) => (
    <li
      key={`${item.debug_file.toLowerCase()}-${item.debug_id.toUpperCase()}-${idx}`}
      className="flex flex-wrap items-center gap-2 font-mono text-[11px]"
    >
      <span className="font-semibold text-slate-900">{item.debug_file}</span>
      <span className="text-slate-500">{item.debug_id}</span>
      <span
        className={`rounded-full px-2 py-0.5 font-sans text-[10px] font-medium ${
          item.status === "imported" ||
          item.status === "upgraded" ||
          item.status === "cached_hit"
            ? "bg-emerald-100 text-emerald-800"
            : item.status === "not_found" || item.status === "skipped"
              ? "bg-amber-100 text-amber-800"
              : "bg-rose-100 text-rose-800"
        }`}
      >
        {renderSyncStatusLabel(item.status)}
      </span>
      {item.detail && (
        <span className="font-sans text-slate-600">{item.detail}</span>
      )}
    </li>
  );

  return (
    <div className="space-y-3">
      {isSyncRunning && syncProgress && (
        <div className="rounded-xl border border-sky-300 bg-sky-50/80 px-4 py-3.5 text-xs text-sky-950 shadow-sm">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2 font-semibold text-sky-900">
              <Loader2 className="h-4 w-4 animate-spin text-sky-600" />
              <span>{t("symbols.syncProgressTitle")}</span>
              {syncProgress.total > 0 && (
                <span className="font-mono text-sky-700">
                  {t("symbols.syncProgressCount", {
                    completed: syncProgress.completed,
                    total: syncProgress.total,
                    percent: Math.min(
                      100,
                      Math.round(
                        (syncProgress.completed / syncProgress.total) * 100,
                      ),
                    ),
                  })}
                </span>
              )}
            </div>
            <button
              type="button"
              onClick={onCancelSync}
              className="inline-flex items-center gap-1 rounded-md border border-sky-300 bg-white px-2.5 py-1 text-[11px] font-medium text-sky-800 hover:bg-sky-100"
            >
              <Square className="h-3 w-3 fill-current" />
              {t("symbols.syncCancelBtn")}
            </button>
          </div>

          <div className="mt-2.5 h-2 w-full overflow-hidden rounded-full bg-sky-200/80">
            <div
              className="h-full rounded-full bg-sky-600 transition-all duration-300"
              style={{
                width:
                  syncProgress.total > 0
                    ? `${Math.min(100, Math.round((syncProgress.completed / syncProgress.total) * 100))}%`
                    : "15%",
              }}
            />
          </div>

          <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-[11px] text-sky-900">
            <div>
              {t("symbols.syncSummaryCounts", {
                imported: syncProgress.imported,
                upgraded: syncProgress.upgraded,
                cached: syncProgress.cachedHit,
                notFound: syncProgress.notFound,
                skipped: syncProgress.skipped,
                failed: syncProgress.failed,
              })}
            </div>
            {syncProgress.alreadyCached > 0 && (
              <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10px] font-medium text-emerald-700">
                {t("symbols.syncProgressAlreadyCached", {
                  count: syncProgress.alreadyCached,
                })}
              </span>
            )}
          </div>

          {syncProgress.activeModules.length > 0 && (
            <div className="mt-1.5 truncate font-mono text-[11px] text-sky-700">
              {t("symbols.syncProgressActive", {
                modules: syncProgress.activeModules.join(", "),
              })}
            </div>
          )}

          {syncProgress.results.length > 0 && (
            <ul className="ck-scrollbar mt-2.5 max-h-44 space-y-1 overflow-y-auto border-t border-sky-200/70 pt-2 pr-1">
              {syncProgress.results.map((item, idx) =>
                renderSyncResultItem(item, idx),
              )}
            </ul>
          )}
        </div>
      )}

      {!isSyncRunning && syncCancelledInfo && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-900">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span>
              {t("symbols.syncCancelledNotice", {
                completed: syncCancelledInfo.completed,
                total: syncCancelledInfo.total,
                imported:
                  syncCancelledInfo.imported + syncCancelledInfo.upgraded,
              })}
            </span>
            <div className="flex items-center gap-2">
              {syncCancelledInfo.imported + syncCancelledInfo.upgraded > 0 && (
                <button
                  type="button"
                  disabled={isResymPending}
                  onClick={() =>
                    onTriggerResym({
                      project_id: syncCancelledInfo.projectId,
                      missing_only: true,
                    })
                  }
                  className="inline-flex items-center gap-1 rounded-md bg-indigo-600 px-2.5 py-1 text-[11px] font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
                >
                  <RefreshCw className="h-3 w-3" />
                  {t("symbols.syncCancelledResymBtn")}
                </button>
              )}
              <button
                type="button"
                onClick={onDismissCancelled}
                className="rounded p-1 hover:bg-amber-100"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>
          {syncCancelledInfo.results.length > 0 && (
            <ul className="ck-scrollbar mt-2 max-h-40 space-y-1 overflow-y-auto border-t border-amber-200/70 pt-2 pr-1">
              {syncCancelledInfo.results.map((item, idx) =>
                renderSyncResultItem(item, idx),
              )}
            </ul>
          )}
        </div>
      )}

      {syncFeedback && (
        <div className="rounded-xl border border-sky-200 bg-sky-50/70 px-4 py-3 text-xs text-sky-950">
          <div className="flex items-start justify-between gap-2">
            <div className="space-y-1">
              <div className="font-semibold">
                {t("symbols.syncSummaryTitle")}
              </div>
              <div>
                {syncFeedback.results.length === 0
                  ? t("symbols.syncNoEligibleTargets")
                  : t("symbols.syncSummaryCounts", {
                      imported: syncFeedback.imported,
                      upgraded: syncFeedback.upgraded,
                      cached: syncFeedback.cached_hit,
                      notFound: syncFeedback.not_found,
                      skipped: syncFeedback.skipped,
                      failed: syncFeedback.failed,
                    })}
                {syncFeedback.resymbolicate_queued_count > 0 && (
                  <span className="ml-2 font-medium text-emerald-700">
                    · {t("symbols.resymQueuedPrefix")}
                    {syncFeedback.resymbolicate_queued_count}
                    {t("symbols.resymQueuedSuffix")}
                  </span>
                )}
                {(syncFeedback.resymbolicate_skipped_active_count ?? 0) > 0 && (
                  <span className="ml-2 font-medium text-amber-800">
                    ·{" "}
                    {syncFeedback.resymbolicate_queued_count > 0
                      ? t("symbols.resymPartialSkippedNote", {
                          count:
                            syncFeedback.resymbolicate_skipped_active_count ??
                            0,
                        })
                      : t("symbols.syncSummaryResymSkippedActive", {
                          count:
                            syncFeedback.resymbolicate_skipped_active_count ??
                            0,
                        })}
                  </span>
                )}
              </div>
              {syncFeedback.circuit_open && (
                <div className="text-amber-800">
                  {t("symbols.syncCircuitWarning")}
                </div>
              )}
            </div>
            <button
              type="button"
              onClick={onDismissSyncFeedback}
              className="rounded p-1 hover:bg-sky-100"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
          {syncFeedback.results.length > 0 && (
            <ul className="ck-scrollbar mt-2 max-h-56 space-y-1 overflow-y-auto border-t border-sky-200/70 pt-2 pr-1">
              {syncFeedback.results.map((item, idx) =>
                renderSyncResultItem(item, idx),
              )}
            </ul>
          )}
        </div>
      )}

      {syncError && (
        <div className="flex items-center justify-between gap-2 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-xs text-rose-700">
          <span>{syncError}</span>
          <button
            type="button"
            onClick={onDismissSyncError}
            className="rounded p-1 hover:bg-rose-100"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {trackedResym &&
        (() => {
          const st = trackedResym.status;
          const isBusyMode = trackedResym.mode === "active_busy";
          const busyChecking = isBusyMode && st === null;
          const busyParsing =
            isBusyMode && st !== null && st.pending + st.parsing > 0;
          const busyTriaging =
            isBusyMode &&
            st !== null &&
            st.pending + st.parsing === 0 &&
            st.triaging > 0;
          const busyNowIdle =
            isBusyMode &&
            st !== null &&
            st.pending + st.parsing + st.triaging === 0;
          const queuedDone = !isBusyMode && Boolean(st?.done);
          const toneClass = queuedDone
            ? "border-emerald-200 bg-emerald-50 text-emerald-950"
            : busyChecking || busyTriaging || busyNowIdle
              ? "border-amber-200 bg-amber-50 text-amber-950"
              : "border-indigo-200 bg-indigo-50/80 text-indigo-950";

          return (
            <div className={`rounded-xl border px-4 py-3 text-xs ${toneClass}`}>
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="space-y-1">
                  <div className="flex items-center gap-2 font-semibold">
                    {queuedDone ? (
                      <CheckCircle2 className="h-4 w-4 text-emerald-600" />
                    ) : busyNowIdle ? (
                      <AlertTriangle className="h-4 w-4 text-amber-600" />
                    ) : (
                      <Loader2
                        className={`h-4 w-4 animate-spin ${
                          busyChecking || busyTriaging
                            ? "text-amber-600"
                            : "text-indigo-600"
                        }`}
                      />
                    )}
                    <span>
                      {!isBusyMode
                        ? queuedDone && st
                          ? t("symbols.resymCompletedTitle", {
                              completed: st.completed,
                              total: st.total,
                            })
                          : t("symbols.resymProgressTitle")
                        : busyChecking || !st
                          ? t("symbols.resymActiveSkippedNotice", {
                              count: trackedResym.crashIds.length,
                            })
                          : busyParsing
                            ? t("symbols.resymBusyParsingTitle", {
                                total: st.total,
                              })
                            : busyTriaging
                              ? t("symbols.resymBusyTriagingTitle", {
                                  triaging: st.triaging,
                                })
                              : t("symbols.resymBusyIdleTitle", {
                                  total: st.total,
                                })}
                      {queuedDone &&
                        st &&
                        st.failed > 0 &&
                        t("symbols.resymCompletedFailedSuffix", {
                          failed: st.failed,
                        })}
                      {queuedDone &&
                        st &&
                        st.triaging > 0 &&
                        t("symbols.resymCompletedTriagingSuffix", {
                          triaging: st.triaging,
                        })}
                    </span>
                  </div>
                  {st && ((!isBusyMode && !st.done) || busyParsing) && (
                    <div>
                      {t("symbols.resymProgressCount", {
                        completed: st.completed,
                        total: st.total,
                        pending: st.pending,
                        parsing: st.parsing,
                      })}
                    </div>
                  )}
                  {busyTriaging && (
                    <div>{t("symbols.resymBusyTriagingHint")}</div>
                  )}
                  {!isBusyMode && (trackedResym.skippedActiveCount ?? 0) > 0 && (
                    <div className="text-amber-800">
                      {t("symbols.resymPartialSkippedNote", {
                        count: trackedResym.skippedActiveCount ?? 0,
                      })}
                    </div>
                  )}
                  <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
                    <span>{t("symbols.resymIdsLabel")}</span>
                    {trackedResym.crashIds.slice(0, 6).map((cid, i) => (
                      <span key={cid}>
                        {i > 0 ? ", " : ""}
                        <Link
                          to={`/crashes/${encodeURIComponent(cid)}`}
                          className="font-mono underline"
                        >
                          {cid.slice(0, 8)}
                        </Link>
                      </span>
                    ))}
                    {trackedResym.crashIds.length > 6 && (
                      <span>
                        {t("symbols.moreCrashes", {
                          count: trackedResym.crashIds.length - 6,
                        })}
                      </span>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  {busyNowIdle && (
                    <button
                      type="button"
                      disabled={isResymPending}
                      onClick={() =>
                        onTriggerResym({
                          project_id: trackedResym.projectId,
                          debug_id: trackedResym.debugId,
                          crash_ids: trackedResym.crashIds,
                        })
                      }
                      className="inline-flex items-center gap-1 rounded-md bg-indigo-600 px-2.5 py-1 text-[11px] font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
                    >
                      <RefreshCw className="h-3 w-3" />
                      {t("symbols.resymRetryNowBtn")}
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={onDismissTrackedResym}
                    className="rounded p-1 hover:bg-black/5"
                  >
                    <X className="h-4 w-4" />
                  </button>
                </div>
              </div>
              {st &&
                ((!isBusyMode && !st.done) || busyParsing) &&
                st.total > 0 && (
                  <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-indigo-200/80">
                    <div
                      className="h-full rounded-full bg-indigo-600 transition-all duration-300"
                      style={{
                        width: `${Math.max(
                          10,
                          Math.min(
                            100,
                            Math.round((st.completed / st.total) * 100),
                          ),
                        )}%`,
                      }}
                    />
                  </div>
                )}
            </div>
          );
        })()}

      {resymFeedback && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-900">
          <div>
            {(resymFeedback.skipped_active_count ?? 0) > 0
              ? t("symbols.syncSummaryResymSkippedActive", {
                  count: resymFeedback.skipped_active_count ?? 0,
                })
              : t("symbols.resymNoneEligible")}
          </div>
          <button
            type="button"
            onClick={onDismissResymFeedback}
            className="rounded p-1 hover:bg-amber-100"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {isResymError && (
        <div className="flex items-center justify-between gap-2 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-xs text-rose-700">
          <span>
            {resymError instanceof ApiError
              ? resymError.message
              : t("symbols.resymFailed")}
          </span>
          <button
            type="button"
            onClick={onResetResymError}
            className="rounded p-1 hover:bg-rose-100"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}
    </div>
  );
}
