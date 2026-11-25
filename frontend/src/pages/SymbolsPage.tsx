import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { CloudDownload, FileCode2, Upload } from "lucide-react";
import {
  deleteSymbol,
  getSymSrvBaseUrl,
  listMissingSymbolsWithTotal,
  listSymbols,
} from "../api/client";
import type { SymbolRead, SymbolScope } from "../api/types";
import { useProjectContext } from "../components/Layout";
import { PublicSymbolSyncModal } from "../components/PublicSymbolSyncModal";
import {
  UploadModal,
  type UploadSymbolTargetHint,
} from "../components/UploadModal";
import { useAppI18n } from "../i18n";
import {
  canAutoSyncMissingWindowsItem,
  canMsSyncMissingItem,
} from "../utils/symbolIdentity";
import { GlobalStatusBanners } from "./symbols/GlobalStatusBanners";
import { missingRowKey, PAGE_SIZE, type SymbolsTab } from "./symbols/helpers";
import { MissingSymbolsTab } from "./symbols/MissingSymbolsTab";
import { ProjectInventoryTab } from "./symbols/ProjectInventoryTab";
import { PublicStoreTab } from "./symbols/PublicStoreTab";
import {
  DeleteSymbolModal,
  SymbolQuickReferenceCard,
} from "./symbols/QuickReferenceAndDeleteModal";
import { useSymbolSyncAndResym } from "./symbols/useSymbolSyncAndResym";

export function SymbolsPage() {
  const { t } = useAppI18n();
  const { projects, selectedProject, selectedProjectId } = useProjectContext();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();

  const rawTab = searchParams.get("tab");
  const activeTab: SymbolsTab =
    rawTab === "missing"
      ? "missing"
      : rawTab === "public"
        ? "public"
        : "inventory";

  const setActiveTab = (tab: SymbolsTab) => {
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

  // Modals & local UI state
  const [isUploadOpen, setIsUploadOpen] = useState(false);
  const [uploadDefaultScope, setUploadDefaultScope] =
    useState<SymbolScope>("project");
  const [uploadTargetHint, setUploadTargetHint] =
    useState<UploadSymbolTargetHint | null>(null);
  const [isSyncModalOpen, setIsSyncModalOpen] = useState(false);
  const [confirmDeleteSymbol, setConfirmDeleteSymbol] =
    useState<SymbolRead | null>(null);
  const [selectedMissingKeys, setSelectedMissingKeys] = useState<Set<string>>(
    () => new Set(),
  );
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

  const clearMissingSelection = useCallback(() => {
    setSelectedMissingKeys(new Set());
  }, []);

  const syncAndResym = useSymbolSyncAndResym({
    sortedMissingItems,
    onClearMissingSelection: clearMissingSelection,
  });

  const deleteMutation = useMutation({
    mutationFn: (symbolId: string) => deleteSymbol(symbolId),
    onSuccess: () => {
      setConfirmDeleteSymbol(null);
      void queryClient.invalidateQueries({ queryKey: ["symbols"] });
      void queryClient.invalidateQueries({ queryKey: ["missing-symbols"] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  const handleCopy = (text: string) => {
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      void navigator.clipboard.writeText(text);
      setCopiedText(text);
      window.setTimeout(() => setCopiedText(null), 1500);
    }
  };

  const handleRequestDelete = (sym: SymbolRead) => {
    deleteMutation.reset();
    setConfirmDeleteSymbol(sym);
  };

  const symSrvUrl = getSymSrvBaseUrl();
  const origin =
    typeof window !== "undefined"
      ? window.location.origin
      : "http://localhost:8080";
  const cliProjectKey = selectedProject?.key ?? "<PROJECT_KEY>";
  const windbgSymPath = `SRV*C:\\Symbols*${symSrvUrl}`;
  const cliSnippet = `python cli/crashkiller_cli.py upload-symbols \\\n  --server ${origin} \\\n  --project ${cliProjectKey} \\\n  --api-key <YOUR_PROJECT_API_KEY> \\\n  --dir ./build/Release \\\n  --app-version 1.0.0`;

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
            {missingTotalCount > 0 && (
              <span className="ml-2 rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 font-mono text-xs font-semibold text-amber-700">
                {missingTotalCount}
              </span>
            )}
          </button>
        </nav>
      </div>

      {/* Global Sync & Re-symbolication Progress / Feedback Banners */}
      <GlobalStatusBanners
        isSyncRunning={syncAndResym.isSyncRunning}
        syncProgress={syncAndResym.syncProgress}
        syncCancelledInfo={syncAndResym.syncCancelledInfo}
        syncFeedback={syncAndResym.syncFeedback}
        syncError={syncAndResym.syncError}
        trackedResym={syncAndResym.trackedResym}
        resymFeedback={syncAndResym.resymFeedback}
        isResymPending={syncAndResym.resymMutation.isPending}
        isResymError={syncAndResym.resymMutation.isError}
        resymError={syncAndResym.resymMutation.error}
        onCancelSync={syncAndResym.handleCancelSync}
        onDismissCancelled={() => syncAndResym.setSyncCancelledInfo(null)}
        onDismissSyncFeedback={() => syncAndResym.setSyncFeedback(null)}
        onDismissSyncError={() => syncAndResym.setSyncError(null)}
        onDismissTrackedResym={() => {
          syncAndResym.setTrackedResym(null);
          syncAndResym.setResymFeedback(null);
        }}
        onDismissResymFeedback={() => syncAndResym.setResymFeedback(null)}
        onResetResymError={() => syncAndResym.resymMutation.reset()}
        onTriggerResym={(payload) => syncAndResym.resymMutation.mutate(payload)}
      />

      {/* TAB 1: SYMBOL INVENTORY */}
      {activeTab === "inventory" && (
        <ProjectInventoryTab
          selectedProjectId={selectedProjectId}
          projectById={projectById}
          searchInput={searchInput}
          appVersionInput={appVersionInput}
          peUnwindFilter={peUnwindFilter}
          offset={offset}
          symbolsList={symbolsQuery.data?.items ?? []}
          symbolsTotal={symbolsQuery.data?.total ?? 0}
          isPending={symbolsQuery.isPending}
          isFetching={symbolsQuery.isFetching}
          isError={symbolsQuery.isError}
          error={symbolsQuery.error}
          copiedText={copiedText}
          onSearchChange={setSearchInput}
          onAppVersionChange={setAppVersionInput}
          onPeUnwindFilterChange={setPeUnwindFilter}
          onResetFilters={() => {
            setSearchInput("");
            setAppVersionInput("");
            setPeUnwindFilter("all");
          }}
          onRefetch={() => void symbolsQuery.refetch()}
          onCopy={handleCopy}
          onRequestUpload={() => openUploadModal("project")}
          onRequestDelete={handleRequestDelete}
          onPrevPage={() => setOffset((p) => Math.max(0, p - PAGE_SIZE))}
          onNextPage={() => setOffset((p) => p + PAGE_SIZE)}
        />
      )}

      {/* TAB 2: PUBLIC SYMBOL STORE */}
      {activeTab === "public" && (
        <PublicStoreTab
          pubSearchInput={pubSearchInput}
          pubSourceFilter={pubSourceFilter}
          pubPeUnwindFilter={pubPeUnwindFilter}
          pubOffset={pubOffset}
          publicSymbolsList={publicSymbolsQuery.data?.items ?? []}
          publicSymbolsTotal={publicSymbolsQuery.data?.total ?? 0}
          isPending={publicSymbolsQuery.isPending}
          isFetching={publicSymbolsQuery.isFetching}
          isError={publicSymbolsQuery.isError}
          error={publicSymbolsQuery.error}
          copiedText={copiedText}
          onSearchChange={setPubSearchInput}
          onSourceFilterChange={setPubSourceFilter}
          onPeUnwindFilterChange={setPubPeUnwindFilter}
          onResetFilters={() => {
            setPubSearchInput("");
            setPubSourceFilter("all");
            setPubPeUnwindFilter("all");
          }}
          onRefetch={() => void publicSymbolsQuery.refetch()}
          onCopy={handleCopy}
          onOpenSyncModal={() => setIsSyncModalOpen(true)}
          onOpenUploadModal={() => openUploadModal("public")}
          onRequestDelete={handleRequestDelete}
          onPrevPage={() => setPubOffset((p) => Math.max(0, p - PAGE_SIZE))}
          onNextPage={() => setPubOffset((p) => p + PAGE_SIZE)}
        />
      )}

      {/* TAB 3: MISSING SYMBOLS QUEUE */}
      {activeTab === "missing" && (
        <MissingSymbolsTab
          selectedProjectId={selectedProjectId}
          sortedMissingItems={sortedMissingItems}
          selectableMissingItems={selectableMissingItems}
          selectedMissingItems={selectedMissingItems}
          selectedMissingKeys={selectedMissingKeys}
          uncachedWindowsMissingCount={uncachedWindowsMissingCount}
          missingTotalCount={missingTotalCount}
          isPending={missingQuery.isPending}
          isFetching={missingQuery.isFetching}
          isError={missingQuery.isError}
          error={missingQuery.error}
          isSyncRunning={syncAndResym.isSyncRunning}
          activeSyncKey={syncAndResym.activeSyncKey}
          isResymPending={syncAndResym.resymMutation.isPending}
          activeResymDebugId={syncAndResym.activeResymDebugId}
          copiedText={copiedText}
          onRefetch={() => void missingQuery.refetch()}
          onSelectAllChange={(checked) => {
            if (checked) {
              setSelectedMissingKeys(
                new Set(selectableMissingItems.map((it) => missingRowKey(it))),
              );
            } else {
              setSelectedMissingKeys(new Set());
            }
          }}
          onToggleRowSelection={(rowKey, checked) => {
            setSelectedMissingKeys((prev) => {
              const next = new Set(prev);
              if (checked) {
                next.add(rowKey);
              } else {
                next.delete(rowKey);
              }
              return next;
            });
          }}
          onStartSync={(key, payload) =>
            void syncAndResym.handleStartSync(key, payload)
          }
          onOpenUploadModal={(scope, hint = null) =>
            openUploadModal(scope, hint)
          }
          onTriggerResym={(payload) =>
            syncAndResym.resymMutation.mutate(payload)
          }
          onCopy={handleCopy}
        />
      )}

      {/* WinDbg / SymSrv & CLI Quick-Reference Card */}
      <SymbolQuickReferenceCard
        windbgSymPath={windbgSymPath}
        cliSnippet={cliSnippet}
        copiedText={copiedText}
        onCopy={handleCopy}
      />

      {/* Delete Confirmation Modal */}
      {confirmDeleteSymbol && (
        <DeleteSymbolModal
          symbol={confirmDeleteSymbol}
          isPending={deleteMutation.isPending}
          isError={deleteMutation.isError}
          error={deleteMutation.error}
          onCancel={() => setConfirmDeleteSymbol(null)}
          onConfirm={() => deleteMutation.mutate(confirmDeleteSymbol.id)}
        />
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
          syncAndResym.invalidateSymbolViews();
          if (result.mode === "symbol") {
            syncAndResym.applyAutoResymTracking({
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
          syncAndResym.invalidateSymbolViews();
          if (!syncAndResym.isSyncRunning) {
            syncAndResym.setSyncFeedback(null);
            syncAndResym.setSyncCancelledInfo(null);
            syncAndResym.setSyncError(null);
          }
          syncAndResym.applyAutoResymTracking({
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
