import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  getResymbolicateStatus,
  resymbolicate,
  syncPublicSymbolsStream,
} from "../../api/client";
import type {
  MissingSymbolQueueItem,
  PublicSymbolSyncItemResult,
  PublicSymbolSyncRequest,
  PublicSymbolSyncResponse,
  ResymbolicateRequest,
} from "../../api/types";
import { useAppI18n } from "../../i18n";
import {
  isTrackedResymFinished,
  type CancelledSyncInfo,
  type LiveSyncProgressState,
  type ResymFeedbackState,
  type TrackedResymState,
} from "./helpers";

interface UseSymbolSyncAndResymOptions {
  sortedMissingItems: MissingSymbolQueueItem[];
  onClearMissingSelection: () => void;
}

export function useSymbolSyncAndResym({
  sortedMissingItems,
  onClearMissingSelection,
}: UseSymbolSyncAndResymOptions) {
  const { t } = useAppI18n();
  const queryClient = useQueryClient();

  const [activeResymDebugId, setActiveResymDebugId] = useState<string | null>(
    null,
  );
  const [activeSyncKey, setActiveSyncKey] = useState<string | null>(null);
  const [isSyncRunning, setIsSyncRunning] = useState(false);
  const [syncProgress, setSyncProgress] =
    useState<LiveSyncProgressState | null>(null);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [syncCancelledInfo, setSyncCancelledInfo] =
    useState<CancelledSyncInfo | null>(null);
  const syncAbortRef = useRef<AbortController | null>(null);
  const lastSyncInvalidateRef = useRef<number>(0);

  const [resymFeedback, setResymFeedback] =
    useState<ResymFeedbackState | null>(null);
  const [trackedResym, setTrackedResym] = useState<TrackedResymState | null>(
    null,
  );
  const [syncFeedback, setSyncFeedback] =
    useState<PublicSymbolSyncResponse | null>(null);

  const invalidateSymbolViews = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["symbols"] });
    void queryClient.invalidateQueries({ queryKey: ["missing-symbols"] });
    void queryClient.invalidateQueries({ queryKey: ["crashes"] });
    void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
  }, [queryClient]);

  const startTrackingResym = useCallback(
    (
      crashIds: string[],
      opts?: {
        mode?: "queued" | "active_busy";
        projectId?: string;
        debugId?: string;
        skippedActiveCount?: number;
      },
    ) => {
      const uniqueIds = Array.from(new Set(crashIds.filter(Boolean))).slice(
        0,
        200,
      );
      if (uniqueIds.length === 0) {
        return;
      }
      const mode = opts?.mode ?? "queued";
      setResymFeedback(null);
      setTrackedResym({
        mode,
        projectId: opts?.projectId,
        crashIds: uniqueIds,
        debugId: opts?.debugId,
        skippedActiveCount: opts?.skippedActiveCount,
        status:
          mode === "active_busy"
            ? null
            : {
                total: uniqueIds.length,
                pending: uniqueIds.length,
                parsing: 0,
                triaging: 0,
                analyzed: 0,
                failed: 0,
                completed: 0,
                in_progress: uniqueIds.length,
                done: false,
              },
      });
    },
    [],
  );

  const clearFinishedResymFeedback = useCallback(() => {
    setTrackedResym((prev) =>
      prev && isTrackedResymFinished(prev) ? null : prev,
    );
    setResymFeedback(null);
  }, []);

  const applyAutoResymTracking = useCallback(
    (opts: {
      crashIds?: string[];
      activeCrashIds?: string[];
      skippedActiveCount?: number;
      projectId?: string;
      debugId?: string;
    }) => {
      const queuedIds = opts.crashIds ?? [];
      const activeIds = opts.activeCrashIds ?? [];
      const skippedActive = opts.skippedActiveCount ?? 0;

      if (queuedIds.length > 0) {
        startTrackingResym(queuedIds, {
          mode: "queued",
          projectId: opts.projectId,
          debugId: opts.debugId,
          skippedActiveCount: skippedActive,
        });
      } else if (skippedActive > 0) {
        if (activeIds.length > 0) {
          startTrackingResym(activeIds, {
            mode: "active_busy",
            projectId: opts.projectId,
            debugId: opts.debugId,
            skippedActiveCount: skippedActive,
          });
        } else {
          clearFinishedResymFeedback();
          setResymFeedback({
            queued_count: 0,
            crash_ids: [],
            debug_id: opts.debugId,
            skipped_active_count: skippedActive,
          });
        }
      } else {
        clearFinishedResymFeedback();
      }
    },
    [clearFinishedResymFeedback, startTrackingResym],
  );

  useEffect(() => {
    return () => {
      syncAbortRef.current?.abort();
    };
  }, []);

  const trackedCrashIdsKey = trackedResym?.crashIds.join(",") ?? "";
  const trackedResymMode = trackedResym?.mode ?? "queued";
  const trackedResymPollFinished = trackedResym
    ? isTrackedResymFinished(trackedResym)
    : true;

  useEffect(() => {
    if (!trackedCrashIdsKey || trackedResymPollFinished) {
      return;
    }
    const crashIds = trackedCrashIdsKey.split(",").filter(Boolean);
    if (crashIds.length === 0) {
      return;
    }
    let cancelled = false;
    let lastSig = "";
    let timerId: number | null = null;

    const poll = async () => {
      try {
        const st = await getResymbolicateStatus(crashIds);
        if (cancelled) return;
        const pollDone =
          trackedResymMode === "active_busy"
            ? st.pending + st.parsing + st.triaging === 0
            : st.done;
        setTrackedResym((prev) =>
          prev &&
          prev.crashIds.join(",") === trackedCrashIdsKey &&
          prev.mode === trackedResymMode
            ? {
                ...prev,
                status: st,
              }
            : prev,
        );
        const sig = `${st.completed}:${st.pending}:${st.parsing}:${st.triaging}:${st.failed}`;
        if (sig !== lastSig || pollDone) {
          lastSig = sig;
          invalidateSymbolViews();
        }
        if (!pollDone && !cancelled) {
          timerId = window.setTimeout(() => {
            void poll();
          }, 1500);
        }
      } catch {
        if (!cancelled) {
          timerId = window.setTimeout(() => {
            void poll();
          }, 3000);
        }
      }
    };

    const initialDelayMs = trackedResymMode === "active_busy" ? 0 : 400;
    timerId = window.setTimeout(() => {
      void poll();
    }, initialDelayMs);

    return () => {
      cancelled = true;
      if (timerId !== null) {
        window.clearTimeout(timerId);
      }
    };
  }, [
    trackedCrashIdsKey,
    trackedResymMode,
    trackedResymPollFinished,
    invalidateSymbolViews,
  ]);

  const resymMutation = useMutation({
    mutationFn: (payload: ResymbolicateRequest) => resymbolicate(payload),
    onMutate: (vars) => {
      setActiveResymDebugId(vars.debug_id ?? "__ALL__");
      clearFinishedResymFeedback();
    },
    onSuccess: (data, vars) => {
      if (data.crash_ids.length > 0) {
        startTrackingResym(data.crash_ids, {
          mode: "queued",
          projectId: vars.project_id,
          debugId: vars.debug_id,
          skippedActiveCount: data.skipped_active_count,
        });
      } else if ((data.skipped_active_count ?? 0) > 0) {
        const busyIds =
          data.active_crash_ids && data.active_crash_ids.length > 0
            ? data.active_crash_ids
            : vars.crash_ids && vars.crash_ids.length > 0
              ? vars.crash_ids
              : Array.from(
                  new Set(
                    sortedMissingItems.flatMap((it) => it.affected_crash_ids),
                  ),
                );
        if (busyIds.length > 0) {
          startTrackingResym(busyIds, {
            mode: "active_busy",
            projectId: vars.project_id,
            debugId: vars.debug_id,
            skippedActiveCount: data.skipped_active_count,
          });
        } else {
          setResymFeedback({
            queued_count: 0,
            crash_ids: [],
            debug_id: vars.debug_id,
            skipped_active_count: data.skipped_active_count,
          });
        }
      } else {
        setResymFeedback({
          queued_count: data.queued_count,
          crash_ids: data.crash_ids,
          debug_id: vars.debug_id,
          skipped_active_count: data.skipped_active_count,
        });
      }
      invalidateSymbolViews();
    },
    onSettled: () => {
      setActiveResymDebugId(null);
    },
  });

  const handleStartSync = async (
    key: string,
    payload: PublicSymbolSyncRequest,
  ) => {
    if (isSyncRunning) return;
    syncAbortRef.current?.abort();
    const controller = new AbortController();
    syncAbortRef.current = controller;
    lastSyncInvalidateRef.current = Date.now();

    setActiveSyncKey(key);
    setIsSyncRunning(true);
    setSyncError(null);
    setSyncFeedback(null);
    setSyncCancelledInfo(null);
    clearFinishedResymFeedback();
    if (resymMutation.isError) {
      resymMutation.reset();
    }
    const initialTotal =
      payload.mode === "targets" ? (payload.targets?.length ?? 0) : 0;
    setSyncProgress({
      total: initialTotal,
      completed: 0,
      alreadyCached: 0,
      imported: 0,
      upgraded: 0,
      cachedHit: 0,
      notFound: 0,
      skipped: 0,
      failed: 0,
      circuitOpen: false,
      activeModules: [],
      results: [],
    });

    let latestCompleted = 0;
    let latestTotal = initialTotal;
    let latestImported = 0;
    let latestUpgraded = 0;
    let latestResults: PublicSymbolSyncItemResult[] = [];

    try {
      const summary = await syncPublicSymbolsStream(payload, {
        signal: controller.signal,
        onEvent: (evt) => {
          if (evt.type === "start") {
            latestTotal = evt.total;
            latestCompleted = evt.completed;
            latestImported = evt.imported;
            latestUpgraded = evt.upgraded;
            latestResults = [];
            setSyncProgress({
              total: evt.total,
              completed: evt.completed,
              alreadyCached: evt.already_cached,
              imported: evt.imported,
              upgraded: evt.upgraded,
              cachedHit: evt.cached_hit,
              notFound: evt.not_found,
              skipped: evt.skipped,
              failed: evt.failed,
              circuitOpen: Boolean(evt.circuit_open),
              activeModules: evt.active_modules ?? [],
              results: [],
            });
          } else if (evt.type === "item_start") {
            latestTotal = evt.total;
            latestCompleted = evt.completed;
            latestImported = evt.imported;
            latestUpgraded = evt.upgraded;
            setSyncProgress((prev) => ({
              total: evt.total,
              completed: evt.completed,
              alreadyCached: evt.already_cached,
              imported: evt.imported,
              upgraded: evt.upgraded,
              cachedHit: evt.cached_hit,
              notFound: evt.not_found,
              skipped: evt.skipped,
              failed: evt.failed,
              circuitOpen: Boolean(evt.circuit_open),
              activeModules: evt.active_modules ?? [evt.current.debug_file],
              results: prev?.results ?? [],
            }));
          } else if (evt.type === "progress") {
            latestTotal = evt.total;
            latestCompleted = evt.completed;
            latestImported = evt.imported;
            latestUpgraded = evt.upgraded;
            latestResults = [evt.item, ...latestResults];
            setSyncProgress((prev) => ({
              total: evt.total,
              completed: evt.completed,
              alreadyCached: evt.already_cached,
              imported: evt.imported,
              upgraded: evt.upgraded,
              cachedHit: evt.cached_hit,
              notFound: evt.not_found,
              skipped: evt.skipped,
              failed: evt.failed,
              circuitOpen: Boolean(evt.circuit_open),
              activeModules: evt.active_modules ?? [],
              results: [evt.item, ...(prev?.results ?? [])],
            }));
            const now = Date.now();
            if (
              (evt.item.status === "imported" ||
                evt.item.status === "upgraded") &&
              now - lastSyncInvalidateRef.current >= 2000
            ) {
              lastSyncInvalidateRef.current = now;
              void queryClient.invalidateQueries({ queryKey: ["symbols"] });
              void queryClient.invalidateQueries({
                queryKey: ["missing-symbols"],
              });
            }
          }
        },
      });

      setSyncFeedback(summary);
      setSyncProgress(null);
      onClearMissingSelection();
      invalidateSymbolViews();
      applyAutoResymTracking({
        crashIds: summary.resymbolicate_crash_ids,
        activeCrashIds: summary.resymbolicate_active_crash_ids,
        skippedActiveCount: summary.resymbolicate_skipped_active_count,
        projectId: payload.project_id,
      });
    } catch (err) {
      if (
        controller.signal.aborted ||
        (err instanceof DOMException && err.name === "AbortError")
      ) {
        setSyncCancelledInfo({
          completed: latestCompleted,
          total: latestTotal,
          imported: latestImported,
          upgraded: latestUpgraded,
          projectId: payload.project_id,
          results: latestResults,
        });
        setSyncProgress(null);
        invalidateSymbolViews();
      } else {
        setSyncError(
          err instanceof ApiError ? err.message : t("symbols.syncFailed"),
        );
        setSyncProgress(null);
        invalidateSymbolViews();
      }
    } finally {
      if (syncAbortRef.current === controller) {
        syncAbortRef.current = null;
      }
      setIsSyncRunning(false);
      setActiveSyncKey(null);
    }
  };

  const handleCancelSync = () => {
    syncAbortRef.current?.abort();
  };

  return {
    activeResymDebugId,
    activeSyncKey,
    isSyncRunning,
    syncProgress,
    syncError,
    syncCancelledInfo,
    syncFeedback,
    trackedResym,
    resymFeedback,
    resymMutation,
    setSyncError,
    setSyncCancelledInfo,
    setSyncFeedback,
    setTrackedResym,
    setResymFeedback,
    invalidateSymbolViews,
    applyAutoResymTracking,
    handleStartSync,
    handleCancelSync,
  };
}
