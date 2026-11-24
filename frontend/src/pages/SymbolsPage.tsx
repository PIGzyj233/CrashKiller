import { useEffect, useMemo, useState } from "react";
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
  Terminal,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import {
  ApiError,
  deleteSymbol,
  getSymSrvBaseUrl,
  listMissingSymbols,
  listSymbols,
  resymbolicate,
  syncPublicSymbols,
} from "../api/client";
import type {
  MissingSymbolQueueItem,
  PublicSymbolSyncItemStatus,
  PublicSymbolSyncRequest,
  PublicSymbolSyncResponse,
  ResymbolicateRequest,
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

const PAGE_SIZE = 25;

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
  const [selectedMissingKeys, setSelectedMissingKeys] = useState<Set<string>>(
    () => new Set(),
  );
  const [resymFeedback, setResymFeedback] = useState<{
    queued_count: number;
    crash_ids: string[];
    debug_id?: string;
  } | null>(null);
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
        limit: 100,
        offset: 0,
      },
    ],
    queryFn: () =>
      listMissingSymbols({
        project_id: selectedProjectId || undefined,
        limit: 100,
        offset: 0,
      }),
  });

  const sortedMissingItems = useMemo(
    () =>
      [...(missingQuery.data ?? [])].sort(
        (a, b) => b.crash_count - a.crash_count,
      ),
    [missingQuery.data],
  );

  const missingRowKey = (item: MissingSymbolQueueItem) =>
    `${item.debug_file.toLowerCase()}::${item.debug_id.toUpperCase()}`;

  const selectableMissingItems = useMemo(
    () =>
      sortedMissingItems.filter(
        (item) =>
          item.debug_file.toLowerCase().endsWith(".pdb") &&
          item.debug_id.length >= 32,
      ),
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

  const resymMutation = useMutation({
    mutationFn: (payload: ResymbolicateRequest) => resymbolicate(payload),
    onMutate: (vars) => {
      setActiveResymDebugId(vars.debug_id ?? "__ALL__");
      setResymFeedback(null);
    },
    onSuccess: (data, vars) => {
      setResymFeedback({
        queued_count: data.queued_count,
        crash_ids: data.crash_ids,
        debug_id: vars.debug_id,
      });
      void queryClient.invalidateQueries({ queryKey: ["missing-symbols"] });
      void queryClient.invalidateQueries({ queryKey: ["crashes"] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
    onSettled: () => {
      setActiveResymDebugId(null);
    },
  });

  const syncMutation = useMutation({
    mutationFn: (vars: { key: string; payload: PublicSymbolSyncRequest }) =>
      syncPublicSymbols(vars.payload),
    onMutate: (vars) => {
      setActiveSyncKey(vars.key);
      setSyncFeedback(null);
    },
    onSuccess: (data) => {
      setSyncFeedback(data);
      setSelectedMissingKeys(new Set());
      void queryClient.invalidateQueries({ queryKey: ["symbols"] });
      void queryClient.invalidateQueries({ queryKey: ["missing-symbols"] });
      void queryClient.invalidateQueries({ queryKey: ["crashes"] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
    onSettled: () => {
      setActiveSyncKey(null);
    },
  });

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
            {sortedMissingItems.length > 0 && (
              <span className="ml-2 rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 font-mono text-xs font-semibold text-amber-700">
                {sortedMissingItems.length}
              </span>
            )}
          </button>
        </nav>
      </div>

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
                disabled={
                  sortedMissingItems.length === 0 || syncMutation.isPending
                }
                onClick={() => {
                  if (selectedMissingItems.length > 0) {
                    syncMutation.mutate({
                      key: "__SELECTED__",
                      payload: {
                        mode: "targets",
                        targets: selectedMissingItems.map((it) => ({
                          module_name: it.module_name,
                          debug_file: it.debug_file,
                          debug_id: it.debug_id,
                          code_file: it.code_file,
                          code_id: it.code_id,
                        })),
                        project_id: selectedProjectId || undefined,
                        force: true,
                        auto_resymbolicate: true,
                      },
                    });
                  } else {
                    syncMutation.mutate({
                      key: "__MISSING_WINDOWS__",
                      payload: {
                        mode: "missing_windows",
                        project_id: selectedProjectId || undefined,
                        force: true,
                        auto_resymbolicate: true,
                        max_targets: 20,
                      },
                    });
                  }
                }}
                className="inline-flex items-center gap-1.5 rounded-md border border-sky-300 bg-sky-50 px-3 py-1.5 text-xs font-medium text-sky-800 hover:bg-sky-100 disabled:opacity-50"
              >
                {syncMutation.isPending &&
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

          {syncFeedback && (
            <div className="rounded-xl border border-sky-200 bg-sky-50/70 px-4 py-3 text-xs text-sky-950">
              <div className="flex items-start justify-between gap-2">
                <div className="space-y-1">
                  <div className="font-semibold">
                    {t("symbols.syncSummaryTitle")}
                  </div>
                  <div>
                    {t("symbols.syncSummaryCounts", {
                      imported: syncFeedback.imported,
                      upgraded: syncFeedback.upgraded,
                      cached: syncFeedback.cached_hit,
                      notFound: syncFeedback.not_found,
                      failed: syncFeedback.failed,
                    })}
                    {syncFeedback.resymbolicate_queued_count > 0 && (
                      <span className="ml-2 font-medium text-emerald-700">
                        · {t("symbols.resymQueuedPrefix")}
                        {syncFeedback.resymbolicate_queued_count}
                        {t("symbols.resymQueuedSuffix")}
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
                <ul className="mt-2 space-y-1 border-t border-sky-200/70 pt-2">
                  {syncFeedback.results.slice(0, 8).map((item) => (
                    <li
                      key={`${item.debug_file}-${item.debug_id}`}
                      className="flex flex-wrap items-center gap-2 font-mono text-[11px]"
                    >
                      <span className="font-semibold text-slate-900">
                        {item.debug_file}
                      </span>
                      <span className="text-slate-500">{item.debug_id}</span>
                      <span
                        className={`rounded-full px-2 py-0.5 font-sans text-[10px] font-medium ${
                          item.status === "imported" ||
                          item.status === "upgraded" ||
                          item.status === "cached_hit"
                            ? "bg-emerald-100 text-emerald-800"
                            : item.status === "not_found"
                              ? "bg-amber-100 text-amber-800"
                              : "bg-rose-100 text-rose-800"
                        }`}
                      >
                        {renderSyncStatusLabel(item.status)}
                      </span>
                      {item.detail && (
                        <span className="font-sans text-slate-600">
                          {item.detail}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {syncMutation.isError && (
            <div className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-xs text-rose-700">
              {syncMutation.error instanceof ApiError
                ? syncMutation.error.message
                : t("symbols.syncFailed")}
            </div>
          )}

          {resymFeedback && (
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-900">
              <div>
                {t("symbols.resymQueuedPrefix")}
                <strong className="font-semibold">
                  {resymFeedback.queued_count}
                </strong>
                {t("symbols.resymQueuedSuffix")}
                {resymFeedback.debug_id
                  ? ` (debug_id: ${resymFeedback.debug_id})`
                  : ""}
                .
                {resymFeedback.crash_ids.length > 0 && (
                  <span className="ml-2 text-xs">
                    {t("symbols.resymIdsLabel")}
                    {resymFeedback.crash_ids.slice(0, 4).map((cid, i) => (
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
                  </span>
                )}
              </div>
              <button
                type="button"
                onClick={() => setResymFeedback(null)}
                className="rounded p-1 hover:bg-emerald-100"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          )}

          {resymMutation.isError && (
            <div className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-xs text-rose-700">
              {resymMutation.error instanceof ApiError
                ? resymMutation.error.message
                : t("symbols.resymFailed")}
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
                        className="h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
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
                      const canMsSync =
                        item.debug_file.toLowerCase().endsWith(".pdb") &&
                        item.debug_id.length >= 32;
                      const isRowPending =
                        resymMutation.isPending &&
                        activeResymDebugId === item.debug_id;
                      const isRowSyncing =
                        syncMutation.isPending && activeSyncKey === rowKey;
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
                              disabled={!canMsSync || syncMutation.isPending}
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
                                  disabled={syncMutation.isPending}
                                  onClick={() =>
                                    syncMutation.mutate({
                                      key: rowKey,
                                      payload: {
                                        mode: "targets",
                                        targets: [
                                          {
                                            module_name: item.module_name,
                                            debug_file: item.debug_file,
                                            debug_id: item.debug_id,
                                            code_file: item.code_file,
                                            code_id: item.code_id,
                                          },
                                        ],
                                        project_id:
                                          selectedProjectId || undefined,
                                        force: true,
                                        auto_resymbolicate: true,
                                      },
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
        onSuccess={() => {
          void queryClient.invalidateQueries({ queryKey: ["symbols"] });
          void queryClient.invalidateQueries({ queryKey: ["missing-symbols"] });
          void queryClient.invalidateQueries({ queryKey: ["crashes"] });
          void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
        }}
      />

      {/* Pull from Microsoft Symbol Server Modal */}
      <PublicSymbolSyncModal
        isOpen={isSyncModalOpen}
        onClose={() => setIsSyncModalOpen(false)}
        onSynced={() => {
          void queryClient.invalidateQueries({ queryKey: ["symbols"] });
          void queryClient.invalidateQueries({ queryKey: ["missing-symbols"] });
          void queryClient.invalidateQueries({ queryKey: ["crashes"] });
          void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
        }}
      />
    </div>
  );
}

export default SymbolsPage;
