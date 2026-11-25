import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  AlertCircle,
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CloudDownload,
  Copy,
  FileCode2,
  Globe,
  Loader2,
  RefreshCw,
  RotateCcw,
  Search,
  Square,
  Terminal,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import {
  ApiError,
  deleteSymbol,
  getResymbolicateStatus,
  getSymSrvBaseUrl,
  listMissingSymbolsWithTotal,
  listSymbols,
  resymbolicate,
  syncPublicSymbolsStream,
} from "../api/client";
import type {
  MissingSymbolQueueItem,
  PublicSymbolSyncItemResult,
  PublicSymbolSyncItemStatus,
  PublicSymbolSyncRequest,
  PublicSymbolSyncResponse,
  ResymbolicateRequest,
  ResymbolicateStatusResponse,
  SymbolRead,
  SymbolScope,
} from "../api/types";
import { useProjectContext } from "../components/Layout";
import { PublicSymbolSyncModal } from "../components/PublicSymbolSyncModal";
import {
  UploadModal,
  type UploadSymbolTargetHint,
} from "../components/UploadModal";
import { useAppI18n } from "../i18n";
import {
  buildMsSyncTarget,
  canAutoSyncMissingWindowsItem,
  canMsSyncMissingItem,
} from "../utils/symbolIdentity";

const PAGE_SIZE = 25;

interface LiveSyncProgressState {
  total: number;
  completed: number;
  alreadyCached: number;
  imported: number;
  upgraded: number;
  cachedHit: number;
  notFound: number;
  skipped: number;
  failed: number;
  circuitOpen: boolean;
  activeModules: string[];
  results: PublicSymbolSyncItemResult[];
}

interface CancelledSyncInfo {
  completed: number;
  total: number;
  imported: number;
  upgraded: number;
  projectId?: string;
  results: PublicSymbolSyncItemResult[];
}

interface TrackedResymState {
  mode: "queued" | "active_busy";
  projectId?: string;
  crashIds: string[];
  debugId?: string;
  status: ResymbolicateStatusResponse | null;
  skippedActiveCount?: number;
}

function isTrackedResymFinished(state: TrackedResymState): boolean {
  if (!state.status) {
    return false;
  }
  return state.mode === "active_busy"
    ? state.status.pending + state.status.parsing + state.status.triaging === 0
    : state.status.done;
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) {
    return "—";
  }
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

export function SymbolsPage() {
  const { t, formatDateTime, formatNumber } = useAppI18n();
  const { projects, selectedProject, selectedProjectId } = useProjectContext();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();

  const rawTab = searchParams.get("tab");
  const activeTab: "inventory" | "public" | "missing" =
    rawTab === "missing"
      ? "missing"
      : rawTab === "public"
        ? "public"
        : "inventory";

  const setActiveTab = (tab: "inventory" | "public" | "missing") => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (tab === "missing" || tab === "public") {
          next.set("tab", tab);
        } else {
          next.delete("tab");
        }
        return next;
      },
      { replace: true },
    );
  };

  // Project inventory filters & pagination
  const [searchInput, setSearchInput] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  const [appVersionInput, setAppVersionInput] = useState("");
  const [debouncedAppVersion, setDebouncedAppVersion] = useState("");
  const [peUnwindFilter, setPeUnwindFilter] = useState<
    "all" | "true" | "false"
  >("all");
  const [offset, setOffset] = useState(0);

  // Public symbol store filters & pagination
  const [pubSearchInput, setPubSearchInput] = useState("");
  const [debouncedPubQ, setDebouncedPubQ] = useState("");
  const [pubSourceFilter, setPubSourceFilter] = useState<
    "all" | "msdl" | "upload" | "public_cache"
  >("all");
  const [pubPeUnwindFilter, setPubPeUnwindFilter] = useState<
    "all" | "true" | "false"
  >("all");
  const [pubOffset, setPubOffset] = useState(0);

  // Modals & action feedback
  const [isUploadOpen, setIsUploadOpen] = useState(false);
  const [uploadDefaultScope, setUploadDefaultScope] =
    useState<SymbolScope>("project");
  const [uploadTargetHint, setUploadTargetHint] =
    useState<UploadSymbolTargetHint | null>(null);
  const [isSyncModalOpen, setIsSyncModalOpen] = useState(false);
  const [confirmDeleteSymbol, setConfirmDeleteSymbol] =
    useState<SymbolRead | null>(null);
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

  const [selectedMissingKeys, setSelectedMissingKeys] = useState<Set<string>>(
    () => new Set(),
  );
  const [resymFeedback, setResymFeedback] = useState<{
    queued_count: number;
    crash_ids: string[];
    debug_id?: string;
    skipped_active_count?: number;
  } | null>(null);
  const [trackedResym, setTrackedResym] = useState<TrackedResymState | null>(
    null,
  );
  const [syncFeedback, setSyncFeedback] =
    useState<PublicSymbolSyncResponse | null>(null);
  const [copiedText, setCopiedText] = useState<string | null>(null);

  const openUploadModal = (
    scope: SymbolScope,
    hint: UploadSymbolTargetHint | null = null,
  ) => {
    setUploadDefaultScope(scope);
    setUploadTargetHint(hint);
    setIsUploadOpen(true);
  };

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedQ(searchInput.trim());
      setDebouncedAppVersion(appVersionInput.trim());
      setDebouncedPubQ(pubSearchInput.trim());
    }, 250);
    return () => window.clearTimeout(timer);
  }, [searchInput, appVersionInput, pubSearchInput]);

  useEffect(() => {
    setOffset(0);
  }, [selectedProjectId, debouncedQ, debouncedAppVersion, peUnwindFilter]);

  useEffect(() => {
    setPubOffset(0);
  }, [debouncedPubQ, pubSourceFilter, pubPeUnwindFilter]);

  const hasPeUnwindParam =
    peUnwindFilter === "true"
      ? true
      : peUnwindFilter === "false"
        ? false
        : undefined;

  const pubHasPeUnwindParam =
    pubPeUnwindFilter === "true"
      ? true
      : pubPeUnwindFilter === "false"
        ? false
        : undefined;

  const projectById = useMemo(
    () => new Map(projects.map((p) => [p.id, p])),
    [projects],
  );

  const symbolsQuery = useQuery({
    queryKey: [
      "symbols",
      "project",
      {
        project_id: selectedProjectId || "",
        app_version: debouncedAppVersion,
        q: debouncedQ,
        has_pe_unwind: hasPeUnwindParam ?? null,
        limit: PAGE_SIZE,
        offset,
      },
    ],
    queryFn: () =>
      listSymbols({
        scope: "project",
        project_id: selectedProjectId || undefined,
        app_version: debouncedAppVersion || undefined,
        q: debouncedQ || undefined,
        has_pe_unwind: hasPeUnwindParam,
        limit: PAGE_SIZE,
        offset,
      }),
    placeholderData: keepPreviousData,
  });

  const publicSymbolsQuery = useQuery({
    queryKey: [
      "symbols",
      "public",
      {
        q: debouncedPubQ,
        source: pubSourceFilter,
        has_pe_unwind: pubHasPeUnwindParam ?? null,
        limit: PAGE_SIZE,
        offset: pubOffset,
      },
    ],
    queryFn: () =>
      listSymbols({
        scope: "public",
        q: debouncedPubQ || undefined,
        source: pubSourceFilter === "all" ? undefined : pubSourceFilter,
        has_pe_unwind: pubHasPeUnwindParam,
        limit: PAGE_SIZE,
        offset: pubOffset,
      }),
    placeholderData: keepPreviousData,
  });

  const missingQuery = useQuery({
    queryKey: [
      "missing-symbols",
      {
        project_id: selectedProjectId || "",
        limit: 500,
        offset: 0,
      },
    ],
    queryFn: () =>
      listMissingSymbolsWithTotal({
        project_id: selectedProjectId || undefined,
        limit: 500,
        offset: 0,
      }),
  });

  const sortedMissingItems = useMemo(
    () =>
      [...(missingQuery.data?.items ?? [])].sort(
        (a, b) => b.crash_count - a.crash_count,
      ),
    [missingQuery.data?.items],
  );
  const missingTotalCount =
    missingQuery.data?.total ?? sortedMissingItems.length;

  const missingRowKey = (item: MissingSymbolQueueItem) =>
    `${item.debug_file.toLowerCase()}::${item.debug_id.toUpperCase()}`;

  const selectableMissingItems = useMemo(
    () => sortedMissingItems.filter((item) => canMsSyncMissingItem(item)),
    [sortedMissingItems],
  );

  const uncachedWindowsMissingCount = useMemo(
    () =>
      sortedMissingItems.filter((item) => canAutoSyncMissingWindowsItem(item))
        .length,
    [sortedMissingItems],
  );

  const selectedMissingItems = useMemo(
    () =>
      selectableMissingItems.filter((item) =>
        selectedMissingKeys.has(missingRowKey(item)),
      ),
    [selectableMissingItems, selectedMissingKeys],
  );

  useEffect(() => {
    setSelectedMissingKeys((prev) => {
      if (prev.size === 0) return prev;
      const validKeys = new Set(
        selectableMissingItems.map((i) => missingRowKey(i)),
      );
      const next = new Set(Array.from(prev).filter((k) => validKeys.has(k)));
      return next.size === prev.size ? prev : next;
    });
  }, [selectableMissingItems]);

  const deleteMutation = useMutation({
    mutationFn: (symbolId: string) => deleteSymbol(symbolId),
    onSuccess: () => {
      setConfirmDeleteSymbol(null);
      void queryClient.invalidateQueries({ queryKey: ["symbols"] });
      void queryClient.invalidateQueries({ queryKey: ["missing-symbols"] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

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
      setSelectedMissingKeys(new Set());
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

  const handleCopy = (text: string) => {
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      void navigator.clipboard.writeText(text);
      setCopiedText(text);
      window.setTimeout(() => setCopiedText(null), 1500);
    }
  };

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

  const renderSourceBadge = (source?: string) => {
    if (source === "msdl") {
      return (
        <span className="inline-flex items-center rounded-full border border-sky-200 bg-sky-50 px-2 py-0.5 text-[11px] font-medium text-sky-700">
          {t("symbols.publicSourceMsdl")}
        </span>
      );
    }
    if (source === "upload") {
      return (
        <span className="inline-flex items-center rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-[11px] font-medium text-indigo-700">
          {t("symbols.publicSourceUpload")}
        </span>
      );
    }
    return (
      <span className="inline-flex items-center rounded-full border border-slate-200 bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-700">
        {t("symbols.publicSourceCache")}
      </span>
    );
  };

  const symSrvUrl = getSymSrvBaseUrl();
  const origin =
    typeof window !== "undefined"
      ? window.location.origin
      : "http://localhost:8080";
  const cliProjectKey = selectedProject?.key ?? "<PROJECT_KEY>";
  const windbgSymPath = `SRV*C:\\Symbols*${symSrvUrl}`;
  const cliSnippet = `python cli/crashkiller_cli.py upload-symbols \\\n  --server ${origin} \\\n  --project ${cliProjectKey} \\\n  --api-key <YOUR_PROJECT_API_KEY> \\\n  --dir ./build/Release \\\n  --app-version 1.0.0`;

  const symbolsList = symbolsQuery.data?.items ?? [];
  const symbolsTotal = symbolsQuery.data?.total ?? 0;
  const fromIdx = symbolsTotal === 0 ? 0 : offset + 1;
  const toIdx = Math.min(symbolsTotal, offset + PAGE_SIZE);

  const publicSymbolsList = publicSymbolsQuery.data?.items ?? [];
  const publicSymbolsTotal = publicSymbolsQuery.data?.total ?? 0;
  const pubFromIdx = publicSymbolsTotal === 0 ? 0 : pubOffset + 1;
  const pubToIdx = Math.min(publicSymbolsTotal, pubOffset + PAGE_SIZE);

  const scopeLabel = selectedProject
    ? `${selectedProject.name} (${selectedProject.key})`
    : selectedProjectId || t("nav.allProjects");

  const hasGlobalStatusBanner = Boolean(
    (isSyncRunning && syncProgress) ||
      (!isSyncRunning && syncCancelledInfo) ||
      syncFeedback ||
      syncError ||
      trackedResym ||
      resymFeedback ||
      resymMutation.isError,
  );

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-indigo-50 text-indigo-600">
            <FileCode2 className="h-5 w-5" />
          </div>
          <div>
            <h1 className="text-xl font-semibold text-slate-900">
              {t("symbols.title")}
            </h1>
            <p className="text-sm text-slate-500">{t("symbols.subtitle")}</p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2.5">
          {activeTab !== "public" && (
            <span className="rounded-full border border-slate-200 bg-white px-3 py-1 text-xs font-medium text-slate-700">
              {t("symbols.scope", { scope: scopeLabel })}
            </span>
          )}
          <button
            type="button"
            onClick={() => setIsSyncModalOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 shadow-sm hover:bg-slate-50"
          >
            <CloudDownload className="h-4 w-4 text-indigo-600" />
            {t("symbols.pullFromMsdl")}
          </button>
          <button
            type="button"
            onClick={() =>
              openUploadModal(activeTab === "public" ? "public" : "project")
            }
            className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-indigo-700"
          >
            <Upload className="h-4 w-4" />
            {activeTab === "public"
              ? t("symbols.uploadPublicSymbol")
              : t("symbols.uploadSymbol")}
          </button>
        </div>
      </div>

      {/* Tabs */}
      <div className="border-b border-slate-200">
        <nav className="-mb-px flex gap-6" aria-label={t("symbols.tabsAria")}>
          <button
            type="button"
            onClick={() => setActiveTab("inventory")}
            className={`border-b-2 pb-3 text-sm font-medium transition-colors ${
              activeTab === "inventory"
                ? "border-indigo-600 text-indigo-600"
                : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-700"
            }`}
          >
            {t("symbols.tabInventory")}
            {typeof symbolsQuery.data?.total === "number" && (
              <span className="ml-2 rounded-full bg-slate-100 px-2 py-0.5 font-mono text-xs text-slate-700">
                {symbolsQuery.data.total}
              </span>
            )}
          </button>

          <button
            type="button"
            onClick={() => setActiveTab("public")}
            className={`border-b-2 pb-3 text-sm font-medium transition-colors ${
              activeTab === "public"
                ? "border-indigo-600 text-indigo-600"
                : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-700"
            }`}
          >
            {t("symbols.tabPublicStore")}
            {typeof publicSymbolsQuery.data?.total === "number" && (
              <span className="ml-2 rounded-full border border-indigo-100 bg-indigo-50 px-2 py-0.5 font-mono text-xs text-indigo-700">
                {publicSymbolsQuery.data.total}
              </span>
            )}
          </button>

          <button
            type="button"
            onClick={() => setActiveTab("missing")}
            className={`border-b-2 pb-3 text-sm font-medium transition-colors ${
              activeTab === "missing"
                ? "border-indigo-600 text-indigo-600"
                : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-700"
            }`}
          >
            {t("symbols.tabMissingQueue")}
            {missingTotalCount > 0 && (
              <span className="ml-2 rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 font-mono text-xs font-semibold text-amber-700">
                {missingTotalCount}
              </span>
            )}
          </button>
        </nav>
      </div>

      {/* Global Sync & Re-symbolication Progress / Feedback Banners (visible across all tabs) */}
      {hasGlobalStatusBanner && (
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
                  onClick={handleCancelSync}
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
                  {syncCancelledInfo.imported + syncCancelledInfo.upgraded >
                    0 && (
                    <button
                      type="button"
                      disabled={resymMutation.isPending}
                      onClick={() =>
                        resymMutation.mutate({
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
                    onClick={() => setSyncCancelledInfo(null)}
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
                    {(syncFeedback.resymbolicate_skipped_active_count ?? 0) >
                      0 && (
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
                  onClick={() => setSyncFeedback(null)}
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
                onClick={() => setSyncError(null)}
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
                <div
                  className={`rounded-xl border px-4 py-3 text-xs ${toneClass}`}
                >
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
                      {!isBusyMode &&
                        (trackedResym.skippedActiveCount ?? 0) > 0 && (
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
                          disabled={resymMutation.isPending}
                          onClick={() =>
                            resymMutation.mutate({
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
                        onClick={() => {
                          setTrackedResym(null);
                          setResymFeedback(null);
                        }}
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
                onClick={() => setResymFeedback(null)}
                className="rounded p-1 hover:bg-amber-100"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          )}

          {resymMutation.isError && (
            <div className="flex items-center justify-between gap-2 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-xs text-rose-700">
              <span>
                {resymMutation.error instanceof ApiError
                  ? resymMutation.error.message
                  : t("symbols.resymFailed")}
              </span>
              <button
                type="button"
                onClick={() => resymMutation.reset()}
                className="rounded p-1 hover:bg-rose-100"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          )}
        </div>
      )}

      {/* TAB 1: SYMBOL INVENTORY */}
      {activeTab === "inventory" && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-3 rounded-xl border border-slate-200 bg-white p-3.5 shadow-sm">
            <div className="relative min-w-[220px] flex-1">
              <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
              <input
                type="text"
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                placeholder={t("symbols.searchPlaceholder")}
                className="h-9 w-full rounded-md border border-slate-300 pl-9 pr-3 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
              />
            </div>

            <input
              type="text"
              value={appVersionInput}
              onChange={(e) => setAppVersionInput(e.target.value)}
              placeholder={t("symbols.appVersionPlaceholder")}
              aria-label={t("symbols.filterAppVersionAria")}
              className="h-9 w-44 rounded-md border border-slate-300 px-3 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />

            <select
              aria-label={t("symbols.filterPeUnwindAria")}
              value={peUnwindFilter}
              onChange={(e) =>
                setPeUnwindFilter(e.target.value as "all" | "true" | "false")
              }
              className="h-9 rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-800"
            >
              <option value="all">{t("symbols.peAll")}</option>
              <option value="true">{t("symbols.peFull")}</option>
              <option value="false">{t("symbols.peMissing")}</option>
            </select>

            {(searchInput || appVersionInput || peUnwindFilter !== "all") && (
              <button
                type="button"
                onClick={() => {
                  setSearchInput("");
                  setAppVersionInput("");
                  setPeUnwindFilter("all");
                }}
                className="inline-flex h-9 items-center gap-1 rounded-md px-2.5 text-xs font-medium text-slate-600 hover:bg-slate-100"
              >
                <RotateCcw className="h-3.5 w-3.5" />
                {t("common.reset")}
              </button>
            )}

            <button
              type="button"
              onClick={() => void symbolsQuery.refetch()}
              disabled={symbolsQuery.isFetching}
              className="inline-flex h-9 items-center gap-1.5 rounded-md border border-slate-200 bg-white px-3 text-xs font-medium text-slate-700 hover:bg-slate-50"
            >
              <RefreshCw
                className={`h-3.5 w-3.5 ${
                  symbolsQuery.isFetching ? "animate-spin" : ""
                }`}
              />
              {t("common.refresh")}
            </button>
          </div>

          {symbolsQuery.isError && (
            <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
              <span>
                {symbolsQuery.error instanceof ApiError
                  ? symbolsQuery.error.message
                  : t("symbols.loadInventoryFailed")}
              </span>
              <button
                type="button"
                onClick={() => void symbolsQuery.refetch()}
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
                    <th className="px-4 py-3">
                      {t("symbols.colModuleDebugFile")}
                    </th>
                    <th className="px-4 py-3">{t("symbols.colDebugId")}</th>
                    <th className="px-4 py-3">{t("symbols.colAppVersion")}</th>
                    <th className="px-4 py-3">{t("symbols.colPeUnwind")}</th>
                    <th className="px-4 py-3">{t("symbols.colSizes")}</th>
                    <th className="px-4 py-3">{t("symbols.colLastUsed")}</th>
                    <th className="px-4 py-3 text-right">
                      {t("symbols.colActions")}
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {symbolsQuery.isPending ? (
                    <tr>
                      <td
                        colSpan={7}
                        className="px-4 py-10 text-center text-sm text-slate-500"
                      >
                        {t("symbols.loadingInventory")}
                      </td>
                    </tr>
                  ) : symbolsList.length === 0 ? (
                    <tr>
                      <td
                        colSpan={7}
                        className="px-4 py-12 text-center text-sm text-slate-500"
                      >
                        {t("symbols.emptyInventoryPrefix")}
                        <button
                          type="button"
                          onClick={() => openUploadModal("project")}
                          className="font-medium text-indigo-600 hover:underline"
                        >
                          {t("symbols.emptyInventoryAction")}
                        </button>
                      </td>
                    </tr>
                  ) : (
                    symbolsList.map((sym: SymbolRead) => {
                      const project = sym.project_id
                        ? projectById.get(sym.project_id)
                        : undefined;
                      return (
                        <tr key={sym.id} className="hover:bg-slate-50">
                          <td className="px-4 py-3">
                            <div className="flex items-center gap-2">
                              <span className="font-medium text-slate-900">
                                {sym.module_name}
                              </span>
                              {!selectedProjectId && project && (
                                <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-600">
                                  {project.key}
                                </span>
                              )}
                            </div>
                            <div className="font-mono text-xs text-slate-500">
                              {sym.debug_file}
                              {sym.code_file ? ` · ${sym.code_file}` : ""}
                            </div>
                          </td>

                          <td className="px-4 py-3">
                            <div className="flex items-center gap-1.5">
                              <code className="font-mono text-xs text-slate-800">
                                {sym.debug_id}
                              </code>
                              <button
                                type="button"
                                onClick={() => handleCopy(sym.debug_id)}
                                className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
                                title={t("symbols.copyDebugId")}
                              >
                                {copiedText === sym.debug_id ? (
                                  <Check className="h-3.5 w-3.5 text-emerald-600" />
                                ) : (
                                  <Copy className="h-3.5 w-3.5" />
                                )}
                              </button>
                            </div>
                          </td>

                          <td className="px-4 py-3 font-mono text-xs text-slate-700">
                            {sym.app_version ?? "—"}
                          </td>

                          <td className="px-4 py-3">
                            {sym.has_pe_unwind ? (
                              <span className="inline-flex items-center gap-1 rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-700">
                                <CheckCircle2 className="h-3.5 w-3.5" />
                                {t("symbols.peUnwindOk")}
                              </span>
                            ) : (
                              <span
                                data-testid="missing-pe-unwind-badge"
                                className="inline-flex items-center gap-1 rounded-full border border-amber-300 bg-amber-50 px-2.5 py-0.5 text-xs font-medium text-amber-800"
                              >
                                <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-amber-600" />
                                {t("symbols.peUnwindMissing")}
                              </span>
                            )}
                          </td>

                          <td className="px-4 py-3 font-mono text-xs text-slate-600">
                            <div>.sym: {formatBytes(sym.sym_size)}</div>
                            <div className="text-[11px] text-slate-400">
                              raw:{" "}
                              {sym.raw_size > 0
                                ? formatBytes(sym.raw_size)
                                : t("symbols.rawNotArchived")}
                            </div>
                          </td>

                          <td className="px-4 py-3 text-xs text-slate-600">
                            {formatDateTime(sym.last_used_at ?? sym.created_at)}
                          </td>

                          <td className="px-4 py-3 text-right">
                            <button
                              type="button"
                              onClick={() => {
                                deleteMutation.reset();
                                setConfirmDeleteSymbol(sym);
                              }}
                              aria-label={t("symbols.deleteSymbolAria", {
                                file: sym.debug_file,
                              })}
                              className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-rose-600 hover:border-rose-200 hover:bg-rose-50"
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                              {t("common.delete")}
                            </button>
                          </td>
                        </tr>
                      );
                    })
                  )}
                </tbody>
              </table>
            </div>

            <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 bg-slate-50 px-4 py-3 text-xs text-slate-600">
              <span>
                {t("common.showingRange", {
                  from: fromIdx,
                  to: toIdx,
                  total: formatNumber(symbolsTotal),
                })}
              </span>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  disabled={offset === 0}
                  onClick={() => setOffset((p) => Math.max(0, p - PAGE_SIZE))}
                  className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1 font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <ChevronLeft className="h-3.5 w-3.5" />
                  {t("common.previous")}
                </button>
                <button
                  type="button"
                  disabled={offset + PAGE_SIZE >= symbolsTotal}
                  onClick={() => setOffset((p) => p + PAGE_SIZE)}
                  className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1 font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {t("common.next")}
                  <ChevronRight className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* TAB 2: PUBLIC SYMBOL STORE (GLOBAL, NO PROJECT / NO VERSION) */}
      {activeTab === "public" && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-indigo-200 bg-indigo-50/60 px-4 py-3 text-xs text-indigo-900">
            <div className="flex items-center gap-2">
              <Globe className="h-4 w-4 shrink-0 text-indigo-600" />
              <span>{t("symbols.publicScopeBanner")}</span>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={() => setIsSyncModalOpen(true)}
                className="inline-flex items-center gap-1.5 rounded-md border border-indigo-300 bg-white px-3 py-1.5 text-xs font-medium text-indigo-700 hover:bg-indigo-50"
              >
                <CloudDownload className="h-3.5 w-3.5" />
                {t("symbols.pullFromMsdl")}
              </button>
              <button
                type="button"
                onClick={() => openUploadModal("public")}
                className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-700"
              >
                <Upload className="h-3.5 w-3.5" />
                {t("symbols.uploadPublicSymbol")}
              </button>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-3 rounded-xl border border-slate-200 bg-white p-3.5 shadow-sm">
            <div className="relative min-w-[220px] flex-1">
              <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
              <input
                type="text"
                value={pubSearchInput}
                onChange={(e) => setPubSearchInput(e.target.value)}
                placeholder={t("symbols.searchPlaceholder")}
                className="h-9 w-full rounded-md border border-slate-300 pl-9 pr-3 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
              />
            </div>

            <select
              aria-label={t("symbols.filterSourceAria")}
              value={pubSourceFilter}
              onChange={(e) =>
                setPubSourceFilter(
                  e.target.value as "all" | "msdl" | "upload" | "public_cache",
                )
              }
              className="h-9 rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-800"
            >
              <option value="all">{t("symbols.publicSourceAll")}</option>
              <option value="msdl">{t("symbols.publicSourceMsdl")}</option>
              <option value="upload">{t("symbols.publicSourceUpload")}</option>
              <option value="public_cache">
                {t("symbols.publicSourceCache")}
              </option>
            </select>

            <select
              aria-label={t("symbols.filterPeUnwindAria")}
              value={pubPeUnwindFilter}
              onChange={(e) =>
                setPubPeUnwindFilter(e.target.value as "all" | "true" | "false")
              }
              className="h-9 rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-800"
            >
              <option value="all">{t("symbols.peAll")}</option>
              <option value="true">{t("symbols.peFull")}</option>
              <option value="false">{t("symbols.peMissing")}</option>
            </select>

            {(pubSearchInput ||
              pubSourceFilter !== "all" ||
              pubPeUnwindFilter !== "all") && (
              <button
                type="button"
                onClick={() => {
                  setPubSearchInput("");
                  setPubSourceFilter("all");
                  setPubPeUnwindFilter("all");
                }}
                className="inline-flex h-9 items-center gap-1 rounded-md px-2.5 text-xs font-medium text-slate-600 hover:bg-slate-100"
              >
                <RotateCcw className="h-3.5 w-3.5" />
                {t("common.reset")}
              </button>
            )}

            <button
              type="button"
              onClick={() => void publicSymbolsQuery.refetch()}
              disabled={publicSymbolsQuery.isFetching}
              className="inline-flex h-9 items-center gap-1.5 rounded-md border border-slate-200 bg-white px-3 text-xs font-medium text-slate-700 hover:bg-slate-50"
            >
              <RefreshCw
                className={`h-3.5 w-3.5 ${
                  publicSymbolsQuery.isFetching ? "animate-spin" : ""
                }`}
              />
              {t("common.refresh")}
            </button>
          </div>

          {publicSymbolsQuery.isError && (
            <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
              <span>
                {publicSymbolsQuery.error instanceof ApiError
                  ? publicSymbolsQuery.error.message
                  : t("symbols.loadInventoryFailed")}
              </span>
              <button
                type="button"
                onClick={() => void publicSymbolsQuery.refetch()}
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
                    <th className="px-4 py-3">
                      {t("symbols.colModuleDebugFile")}
                    </th>
                    <th className="px-4 py-3">{t("symbols.colDebugId")}</th>
                    <th className="px-4 py-3">{t("symbols.colSource")}</th>
                    <th className="px-4 py-3">{t("symbols.colPeUnwind")}</th>
                    <th className="px-4 py-3">{t("symbols.colSizes")}</th>
                    <th className="px-4 py-3">{t("symbols.colLastUsed")}</th>
                    <th className="px-4 py-3 text-right">
                      {t("symbols.colActions")}
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {publicSymbolsQuery.isPending ? (
                    <tr>
                      <td
                        colSpan={7}
                        className="px-4 py-10 text-center text-sm text-slate-500"
                      >
                        {t("symbols.loadingPublicStore")}
                      </td>
                    </tr>
                  ) : publicSymbolsList.length === 0 ? (
                    <tr>
                      <td
                        colSpan={7}
                        className="px-4 py-12 text-center text-sm text-slate-500"
                      >
                        {t("symbols.emptyPublicPrefix")}
                        <button
                          type="button"
                          onClick={() => openUploadModal("public")}
                          className="font-medium text-indigo-600 hover:underline"
                        >
                          {t("symbols.emptyPublicAction")}
                        </button>
                        {t("symbols.emptyPublicOr")}
                        <button
                          type="button"
                          onClick={() => setIsSyncModalOpen(true)}
                          className="font-medium text-indigo-600 hover:underline"
                        >
                          {t("symbols.emptyPublicPullAction")}
                        </button>
                      </td>
                    </tr>
                  ) : (
                    publicSymbolsList.map((sym: SymbolRead) => (
                      <tr key={sym.id} className="hover:bg-slate-50">
                        <td className="px-4 py-3">
                          <div className="font-medium text-slate-900">
                            {sym.module_name}
                          </div>
                          <div className="font-mono text-xs text-slate-500">
                            {sym.debug_file}
                            {sym.code_file ? ` · ${sym.code_file}` : ""}
                          </div>
                        </td>

                        <td className="px-4 py-3">
                          <div className="flex items-center gap-1.5">
                            <code className="font-mono text-xs text-slate-800">
                              {sym.debug_id}
                            </code>
                            <button
                              type="button"
                              onClick={() => handleCopy(sym.debug_id)}
                              className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
                              title={t("symbols.copyDebugId")}
                            >
                              {copiedText === sym.debug_id ? (
                                <Check className="h-3.5 w-3.5 text-emerald-600" />
                              ) : (
                                <Copy className="h-3.5 w-3.5" />
                              )}
                            </button>
                          </div>
                        </td>

                        <td className="px-4 py-3">
                          {renderSourceBadge(sym.source)}
                        </td>

                        <td className="px-4 py-3">
                          {sym.has_pe_unwind ? (
                            <span className="inline-flex items-center gap-1 rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-700">
                              <CheckCircle2 className="h-3.5 w-3.5" />
                              {t("symbols.peUnwindOk")}
                            </span>
                          ) : (
                            <span className="inline-flex items-center gap-1 rounded-full border border-amber-300 bg-amber-50 px-2.5 py-0.5 text-xs font-medium text-amber-800">
                              <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-amber-600" />
                              {t("symbols.peUnwindMissing")}
                            </span>
                          )}
                        </td>

                        <td className="px-4 py-3 font-mono text-xs text-slate-600">
                          <div>.sym: {formatBytes(sym.sym_size)}</div>
                          <div className="text-[11px] text-slate-400">
                            raw:{" "}
                            {sym.raw_size > 0
                              ? formatBytes(sym.raw_size)
                              : t("symbols.rawNotArchived")}
                          </div>
                        </td>

                        <td className="px-4 py-3 text-xs text-slate-600">
                          {formatDateTime(sym.last_used_at ?? sym.created_at)}
                        </td>

                        <td className="px-4 py-3 text-right">
                          <button
                            type="button"
                            onClick={() => {
                              deleteMutation.reset();
                              setConfirmDeleteSymbol(sym);
                            }}
                            aria-label={t("symbols.deleteSymbolAria", {
                              file: sym.debug_file,
                            })}
                            className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-rose-600 hover:border-rose-200 hover:bg-rose-50"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                            {t("common.delete")}
                          </button>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>

            <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 bg-slate-50 px-4 py-3 text-xs text-slate-600">
              <span>
                {t("common.showingRange", {
                  from: pubFromIdx,
                  to: pubToIdx,
                  total: formatNumber(publicSymbolsTotal),
                })}
              </span>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  disabled={pubOffset === 0}
                  onClick={() => setPubOffset((p) => Math.max(0, p - PAGE_SIZE))}
                  className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1 font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <ChevronLeft className="h-3.5 w-3.5" />
                  {t("common.previous")}
                </button>
                <button
                  type="button"
                  disabled={pubOffset + PAGE_SIZE >= publicSymbolsTotal}
                  onClick={() => setPubOffset((p) => p + PAGE_SIZE)}
                  className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1 font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {t("common.next")}
                  <ChevronRight className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* TAB 3: MISSING SYMBOLS QUEUE */}
      {activeTab === "missing" && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white p-3.5 shadow-sm">
            <div className="flex items-center gap-2 text-xs text-slate-600">
              <AlertCircle className="h-4 w-4 shrink-0 text-amber-500" />
              <span>{t("symbols.missingHint")}</span>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={() => void missingQuery.refetch()}
                disabled={missingQuery.isFetching}
                className="inline-flex items-center gap-1.5 rounded-md border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
              >
                <RefreshCw
                  className={`h-3.5 w-3.5 ${
                    missingQuery.isFetching ? "animate-spin" : ""
                  }`}
                />
                {t("common.refresh")}
              </button>

              <button
                type="button"
                disabled={sortedMissingItems.length === 0 || isSyncRunning}
                onClick={() => {
                  if (selectedMissingItems.length > 0) {
                    void handleStartSync("__SELECTED__", {
                      mode: "targets",
                      targets: selectedMissingItems.map((it) =>
                        buildMsSyncTarget(it),
                      ),
                      project_id: selectedProjectId || undefined,
                      force: true,
                      auto_resymbolicate: true,
                    });
                  } else {
                    void handleStartSync("__MISSING_WINDOWS__", {
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
                onClick={() => openUploadModal("public")}
                className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
              >
                <Upload className="h-3.5 w-3.5" />
                {t("symbols.uploadPublicSymbol")}
              </button>

              <button
                type="button"
                disabled={
                  sortedMissingItems.length === 0 || resymMutation.isPending
                }
                onClick={() =>
                  resymMutation.mutate({
                    project_id: selectedProjectId || undefined,
                    missing_only: true,
                  })
                }
                className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
              >
                {resymMutation.isPending && activeResymDebugId === "__ALL__" ? (
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

          {missingQuery.isError && (
            <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
              <span>
                {missingQuery.error instanceof ApiError
                  ? missingQuery.error.message
                  : t("symbols.loadMissingFailed")}
              </span>
              <button
                type="button"
                onClick={() => void missingQuery.refetch()}
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
                        onChange={(e) => {
                          if (e.target.checked) {
                            setSelectedMissingKeys(
                              new Set(
                                selectableMissingItems.map((it) =>
                                  missingRowKey(it),
                                ),
                              ),
                            );
                          } else {
                            setSelectedMissingKeys(new Set());
                          }
                        }}
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
                  {missingQuery.isPending ? (
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
                        resymMutation.isPending &&
                        activeResymDebugId === item.debug_id;
                      const isRowSyncing =
                        isSyncRunning && activeSyncKey === rowKey;
                      const visibleCrashIds = item.affected_crash_ids.slice(
                        0,
                        4,
                      );
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
                              onChange={(e) => {
                                setSelectedMissingKeys((prev) => {
                                  const next = new Set(prev);
                                  if (e.target.checked) {
                                    next.add(rowKey);
                                  } else {
                                    next.delete(rowKey);
                                  }
                                  return next;
                                });
                              }}
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
                                onClick={() => handleCopy(item.debug_id)}
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
                                    void handleStartSync(rowKey, {
                                      mode: "targets",
                                      targets: [buildMsSyncTarget(item)],
                                      project_id:
                                        selectedProjectId || undefined,
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
                                  openUploadModal(
                                    item.is_windows_system
                                      ? "public"
                                      : "project",
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
                                disabled={resymMutation.isPending}
                                onClick={() =>
                                  resymMutation.mutate({
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
      )}

      {/* WinDbg / SymSrv & CLI Quick-Reference Card */}
      <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex items-center gap-2">
          <Terminal className="h-4 w-4 text-indigo-600" />
          <h2 className="text-sm font-semibold text-slate-900">
            {t("symbols.quickRefTitleBefore")}
            <code className="font-mono">/symsrv</code>
            {t("symbols.quickRefTitleAfter")}
          </h2>
        </div>

        <div className="mt-4 grid gap-4 lg:grid-cols-2">
          <div className="rounded-lg border border-slate-200 bg-slate-50 p-3.5">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-800">
                {t("symbols.windbgPathTitle")}
              </span>
              <button
                type="button"
                onClick={() => handleCopy(windbgSymPath)}
                className="inline-flex items-center gap-1 rounded border border-slate-200 bg-white px-2 py-0.5 text-[11px] font-medium text-slate-700 hover:bg-slate-100"
              >
                {copiedText === windbgSymPath ? (
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
            <pre className="mt-2 overflow-x-auto rounded bg-slate-950 p-2.5 font-mono text-xs text-slate-100">
              {windbgSymPath}
            </pre>
            <p className="mt-2 text-[11px] text-slate-500">
              {t("symbols.windbgHintBefore")}
              <Link to="/admin" className="text-indigo-600 hover:underline">
                {t("symbols.windbgHintLink")}
              </Link>
              {t("symbols.windbgHintMiddle")}
              <code className="font-mono">archive_raw_symbols</code>
              {t("symbols.windbgHintAfter")}
            </p>
          </div>

          <div className="rounded-lg border border-slate-200 bg-slate-50 p-3.5">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-800">
                {t("symbols.cliTitleBefore")}
                <code className="font-mono">crashkiller_cli.py</code>
                {t("symbols.cliTitleAfter")}
              </span>
              <button
                type="button"
                onClick={() => handleCopy(cliSnippet)}
                className="inline-flex items-center gap-1 rounded border border-slate-200 bg-white px-2 py-0.5 text-[11px] font-medium text-slate-700 hover:bg-slate-100"
              >
                {copiedText === cliSnippet ? (
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
            <pre className="mt-2 overflow-x-auto rounded bg-slate-950 p-2.5 font-mono text-xs text-slate-100">
              {cliSnippet}
            </pre>
            <p className="mt-2 text-[11px] text-slate-500">
              {t("symbols.cliHintBefore")}
              <code className="font-mono">.pdb</code>
              {t("symbols.cliHintMid1")}
              <code className="font-mono">.exe/.dll</code>
              {t("symbols.cliHintMid2")}
              <code className="font-mono">has_pe_unwind</code>
              {t("symbols.cliHintAfter")}
            </p>
          </div>
        </div>
      </div>

      {/* Delete Confirmation Modal */}
      {confirmDeleteSymbol && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4"
          role="dialog"
          aria-modal="true"
          aria-labelledby="delete-symbol-modal-title"
        >
          <div className="w-full max-w-md rounded-xl border border-slate-200 bg-white p-6 shadow-xl">
            <h2
              id="delete-symbol-modal-title"
              className="text-base font-semibold text-slate-900"
            >
              {t("symbols.deleteModalTitle")}
            </h2>
            <p className="mt-2 text-sm text-slate-600">
              {t("symbols.deleteModalDescBefore")}
              <code className="font-mono font-semibold text-slate-900">
                {confirmDeleteSymbol.debug_file}
              </code>{" "}
              (<code className="font-mono text-xs">{confirmDeleteSymbol.debug_id}</code>)
              {t("symbols.deleteModalDescAfter")}
            </p>

            {deleteMutation.isError && (
              <div className="mt-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                {deleteMutation.error instanceof ApiError
                  ? deleteMutation.error.message
                  : t("symbols.deleteFailed")}
              </div>
            )}

            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setConfirmDeleteSymbol(null)}
                className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
              >
                {t("common.cancel")}
              </button>
              <button
                type="button"
                disabled={deleteMutation.isPending}
                onClick={() => deleteMutation.mutate(confirmDeleteSymbol.id)}
                className="inline-flex items-center gap-1.5 rounded-md bg-rose-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-rose-700 disabled:opacity-50"
              >
                <Trash2 className="h-4 w-4" />
                {deleteMutation.isPending
                  ? t("common.deleting")
                  : t("symbols.confirmDelete")}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Upload Symbol Modal */}
      <UploadModal
        isOpen={isUploadOpen}
        onClose={() => {
          setIsUploadOpen(false);
          setUploadTargetHint(null);
        }}
        mode="symbol"
        projects={projects}
        defaultProjectKey={selectedProject?.key}
        defaultSymbolScope={uploadDefaultScope}
        symbolTargetHint={uploadTargetHint}
        onSuccess={(result) => {
          invalidateSymbolViews();
          if (result.mode === "symbol") {
            applyAutoResymTracking({
              crashIds: result.resymbolicateCrashIds,
              activeCrashIds: result.resymbolicateActiveCrashIds,
              skippedActiveCount: result.resymbolicateSkippedActiveCount,
            });
          }
        }}
      />

      {/* Pull from Microsoft Symbol Server Modal */}
      <PublicSymbolSyncModal
        isOpen={isSyncModalOpen}
        onClose={() => setIsSyncModalOpen(false)}
        onSynced={(res) => {
          invalidateSymbolViews();
          if (!isSyncRunning) {
            setSyncFeedback(null);
            setSyncCancelledInfo(null);
            setSyncError(null);
          }
          applyAutoResymTracking({
            crashIds: res.resymbolicate_crash_ids,
            activeCrashIds: res.resymbolicate_active_crash_ids,
            skippedActiveCount: res.resymbolicate_skipped_active_count,
          });
        }}
      />
    </div>
  );
}

export default SymbolsPage;
