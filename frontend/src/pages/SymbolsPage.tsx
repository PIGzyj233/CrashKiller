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
  Copy,
  FileCode2,
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
} from "../api/client";
import type {
  MissingSymbolQueueItem,
  ResymbolicateRequest,
  SymbolRead,
} from "../api/types";
import { useProjectContext } from "../components/Layout";
import { UploadModal } from "../components/UploadModal";

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

function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) {
    return value;
  }
  return d.toLocaleString();
}

export function SymbolsPage() {
  const { projects, selectedProject, selectedProjectId } = useProjectContext();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();

  const activeTab: "inventory" | "missing" =
    searchParams.get("tab") === "missing" ? "missing" : "inventory";

  const setActiveTab = (tab: "inventory" | "missing") => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (tab === "missing") {
          next.set("tab", "missing");
        } else {
          next.delete("tab");
        }
        return next;
      },
      { replace: true },
    );
  };

  // Inventory filters & pagination
  const [searchInput, setSearchInput] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  const [appVersionInput, setAppVersionInput] = useState("");
  const [debouncedAppVersion, setDebouncedAppVersion] = useState("");
  const [peUnwindFilter, setPeUnwindFilter] = useState<
    "all" | "true" | "false"
  >("all");
  const [offset, setOffset] = useState(0);

  // Modals & action feedback
  const [isUploadOpen, setIsUploadOpen] = useState(false);
  const [confirmDeleteSymbol, setConfirmDeleteSymbol] =
    useState<SymbolRead | null>(null);
  const [activeResymDebugId, setActiveResymDebugId] = useState<string | null>(
    null,
  );
  const [resymFeedback, setResymFeedback] = useState<{
    queued_count: number;
    crash_ids: string[];
    debug_id?: string;
  } | null>(null);
  const [copiedText, setCopiedText] = useState<string | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedQ(searchInput.trim());
      setDebouncedAppVersion(appVersionInput.trim());
    }, 250);
    return () => window.clearTimeout(timer);
  }, [searchInput, appVersionInput]);

  useEffect(() => {
    setOffset(0);
  }, [selectedProjectId, debouncedQ, debouncedAppVersion, peUnwindFilter]);

  const hasPeUnwindParam =
    peUnwindFilter === "true"
      ? true
      : peUnwindFilter === "false"
        ? false
        : undefined;

  const projectById = useMemo(
    () => new Map(projects.map((p) => [p.id, p])),
    [projects],
  );

  const symbolsQuery = useQuery({
    queryKey: [
      "symbols",
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
        project_id: selectedProjectId || undefined,
        app_version: debouncedAppVersion || undefined,
        q: debouncedQ || undefined,
        has_pe_unwind: hasPeUnwindParam,
        limit: PAGE_SIZE,
        offset,
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

  const deleteMutation = useMutation({
    mutationFn: (symbolId: string) => deleteSymbol(symbolId),
    onSuccess: () => {
      setConfirmDeleteSymbol(null);
      void queryClient.invalidateQueries({ queryKey: ["symbols"] });
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

  const handleCopy = (text: string) => {
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      void navigator.clipboard.writeText(text);
      setCopiedText(text);
      window.setTimeout(() => setCopiedText(null), 1500);
    }
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

  const scopeLabel = selectedProject
    ? `${selectedProject.name} (${selectedProject.key})`
    : selectedProjectId || "All Projects";

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
              Symbols &amp; SymSrv
            </h1>
            <p className="text-sm text-slate-500">
              Breakpad symbol inventory, missing symbols queue, and WinDbg
              symbol server
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2.5">
          <span className="rounded-full border border-slate-200 bg-white px-3 py-1 text-xs font-medium text-slate-700">
            Scope: {scopeLabel}
          </span>
          <button
            type="button"
            onClick={() => setIsUploadOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-indigo-700"
          >
            <Upload className="h-4 w-4" />
            Upload Symbol
          </button>
        </div>
      </div>

      {/* Tabs */}
      <div className="border-b border-slate-200">
        <nav className="-mb-px flex gap-6" aria-label="Symbols tabs">
          <button
            type="button"
            onClick={() => setActiveTab("inventory")}
            className={`border-b-2 pb-3 text-sm font-medium transition-colors ${
              activeTab === "inventory"
                ? "border-indigo-600 text-indigo-600"
                : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-700"
            }`}
          >
            Symbol Inventory
            {typeof symbolsQuery.data?.total === "number" && (
              <span className="ml-2 rounded-full bg-slate-100 px-2 py-0.5 font-mono text-xs text-slate-700">
                {symbolsQuery.data.total}
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
            Missing Symbols Queue
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
                placeholder="Search module, debug file, or debug ID..."
                className="h-9 w-full rounded-md border border-slate-300 pl-9 pr-3 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
              />
            </div>

            <input
              type="text"
              value={appVersionInput}
              onChange={(e) => setAppVersionInput(e.target.value)}
              placeholder="App version (e.g. 1.2.0)"
              aria-label="Filter by app version"
              className="h-9 w-44 rounded-md border border-slate-300 px-3 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />

            <select
              aria-label="Filter by PE unwind status"
              value={peUnwindFilter}
              onChange={(e) =>
                setPeUnwindFilter(e.target.value as "all" | "true" | "false")
              }
              className="h-9 rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-800"
            >
              <option value="all">All Symbols</option>
              <option value="true">Full PE Unwind</option>
              <option value="false">Missing PE Companion</option>
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
                Reset
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
              Refresh
            </button>
          </div>

          {symbolsQuery.isError && (
            <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
              <span>
                {symbolsQuery.error instanceof ApiError
                  ? symbolsQuery.error.message
                  : "Failed to load symbol inventory."}
              </span>
              <button
                type="button"
                onClick={() => void symbolsQuery.refetch()}
                className="rounded border border-rose-300 bg-white px-2.5 py-1 text-xs font-medium text-rose-700"
              >
                Retry
              </button>
            </div>
          )}

          <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-200 text-left text-sm">
                <thead className="bg-slate-50 text-xs font-medium uppercase tracking-wider text-slate-500">
                  <tr>
                    <th className="px-4 py-3">Module / Debug File</th>
                    <th className="px-4 py-3">Debug ID</th>
                    <th className="px-4 py-3">App Version</th>
                    <th className="px-4 py-3">PE Unwind</th>
                    <th className="px-4 py-3">Sizes</th>
                    <th className="px-4 py-3">Last Used</th>
                    <th className="px-4 py-3 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {symbolsQuery.isPending ? (
                    <tr>
                      <td
                        colSpan={7}
                        className="px-4 py-10 text-center text-sm text-slate-500"
                      >
                        Loading symbol inventory...
                      </td>
                    </tr>
                  ) : symbolsList.length === 0 ? (
                    <tr>
                      <td
                        colSpan={7}
                        className="px-4 py-12 text-center text-sm text-slate-500"
                      >
                        No Breakpad symbols found.{" "}
                        <button
                          type="button"
                          onClick={() => setIsUploadOpen(true)}
                          className="font-medium text-indigo-600 hover:underline"
                        >
                          Upload a PDB / EXE / DLL
                        </button>
                      </td>
                    </tr>
                  ) : (
                    symbolsList.map((sym: SymbolRead) => {
                      const project = projectById.get(sym.project_id);
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
                                title="Copy debug_id"
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
                            {sym.app_version}
                          </td>

                          <td className="px-4 py-3">
                            {sym.has_pe_unwind ? (
                              <span className="inline-flex items-center gap-1 rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-700">
                                <CheckCircle2 className="h-3.5 w-3.5" />
                                PE Unwind OK
                              </span>
                            ) : (
                              <span
                                data-testid="missing-pe-unwind-badge"
                                className="inline-flex items-center gap-1 rounded-full border border-amber-300 bg-amber-50 px-2.5 py-0.5 text-xs font-medium text-amber-800"
                              >
                                <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-amber-600" />
                                缺少 PE 伴随文件，x64 栈展开可能受限
                              </span>
                            )}
                          </td>

                          <td className="px-4 py-3 font-mono text-xs text-slate-600">
                            <div>.sym: {formatBytes(sym.sym_size)}</div>
                            <div className="text-[11px] text-slate-400">
                              raw:{" "}
                              {sym.raw_size > 0
                                ? formatBytes(sym.raw_size)
                                : "Not archived"}
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
                              aria-label={`Delete symbol ${sym.debug_file}`}
                              className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-rose-600 hover:border-rose-200 hover:bg-rose-50"
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                              Delete
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
                Showing {fromIdx}–{toIdx} of {symbolsTotal.toLocaleString()}
              </span>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  disabled={offset === 0}
                  onClick={() => setOffset((p) => Math.max(0, p - PAGE_SIZE))}
                  className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1 font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <ChevronLeft className="h-3.5 w-3.5" />
                  Previous
                </button>
                <button
                  type="button"
                  disabled={offset + PAGE_SIZE >= symbolsTotal}
                  onClick={() => setOffset((p) => p + PAGE_SIZE)}
                  className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1 font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  Next
                  <ChevronRight className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* TAB 2: MISSING SYMBOLS QUEUE */}
      {activeTab === "missing" && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white p-3.5 shadow-sm">
            <div className="flex items-center gap-2 text-xs text-slate-600">
              <AlertCircle className="h-4 w-4 text-amber-500" />
              <span>
                Missing symbols ordered by impacted crash count descending.
                Upload the missing PDB + EXE/DLL then click Re-symbolicate.
              </span>
            </div>

            <div className="flex items-center gap-2">
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
                Refresh
              </button>
              <button
                type="button"
                disabled={
                  sortedMissingItems.length === 0 || resymMutation.isPending
                }
                onClick={() =>
                  resymMutation.mutate({
                    project_id: selectedProjectId || undefined,
                  })
                }
                className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
              >
                {resymMutation.isPending && activeResymDebugId === "__ALL__" ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <RefreshCw className="h-3.5 w-3.5" />
                )}
                Re-symbolicate All Missing
              </button>
            </div>
          </div>

          {resymFeedback && (
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-900">
              <div>
                Queued{" "}
                <strong className="font-semibold">
                  {resymFeedback.queued_count}
                </strong>{" "}
                crash(es) for re-symbolication
                {resymFeedback.debug_id
                  ? ` (debug_id: ${resymFeedback.debug_id})`
                  : ""}
                .
                {resymFeedback.crash_ids.length > 0 && (
                  <span className="ml-2 text-xs">
                    IDs:{" "}
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
                : "Failed to queue re-symbolication."}
            </div>
          )}

          {missingQuery.isError && (
            <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
              <span>
                {missingQuery.error instanceof ApiError
                  ? missingQuery.error.message
                  : "Failed to load missing symbols queue."}
              </span>
              <button
                type="button"
                onClick={() => void missingQuery.refetch()}
                className="rounded border border-rose-300 bg-white px-2.5 py-1 text-xs font-medium text-rose-700"
              >
                Retry
              </button>
            </div>
          )}

          <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-200 text-left text-sm">
                <thead className="bg-slate-50 text-xs font-medium uppercase tracking-wider text-slate-500">
                  <tr>
                    <th className="px-4 py-3">Module &amp; Debug File</th>
                    <th className="px-4 py-3">Debug ID</th>
                    <th className="px-4 py-3">Code File / ID</th>
                    <th className="px-4 py-3">Crashes</th>
                    <th className="px-4 py-3">Affected Crashes</th>
                    <th className="px-4 py-3 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {missingQuery.isPending ? (
                    <tr>
                      <td
                        colSpan={6}
                        className="px-4 py-10 text-center text-sm text-slate-500"
                      >
                        Loading missing symbols queue...
                      </td>
                    </tr>
                  ) : sortedMissingItems.length === 0 ? (
                    <tr>
                      <td
                        colSpan={6}
                        className="px-4 py-12 text-center text-sm text-slate-500"
                      >
                        <CheckCircle2 className="mx-auto mb-2 h-6 w-6 text-emerald-500" />
                        No missing symbols detected across parsed crashes.
                      </td>
                    </tr>
                  ) : (
                    sortedMissingItems.map((item: MissingSymbolQueueItem) => {
                      const isRowPending =
                        resymMutation.isPending &&
                        activeResymDebugId === item.debug_id;
                      const visibleCrashIds = item.affected_crash_ids.slice(
                        0,
                        4,
                      );
                      const extraCrashes =
                        item.affected_crash_ids.length - visibleCrashIds.length;

                      return (
                        <tr
                          key={`${item.debug_file}-${item.debug_id}`}
                          className="hover:bg-slate-50"
                        >
                          <td className="px-4 py-3">
                            <div className="font-medium text-slate-900">
                              {item.module_name || item.debug_file}
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
                                title="Copy debug_id"
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
                              Latest: {formatDateTime(item.latest_crash_at)}
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
                                    +{extraCrashes} more
                                  </span>
                                )}
                              </div>
                            )}
                          </td>

                          <td className="px-4 py-3 text-right">
                            <div className="inline-flex items-center gap-2">
                              <button
                                type="button"
                                onClick={() => setIsUploadOpen(true)}
                                className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
                              >
                                <Upload className="h-3.5 w-3.5" />
                                Upload
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
                                Re-symbolicate
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
            WinDbg <code className="font-mono">/symsrv</code> &amp; CI/CD CLI
            Quick Reference
          </h2>
        </div>

        <div className="mt-4 grid gap-4 lg:grid-cols-2">
          <div className="rounded-lg border border-slate-200 bg-slate-50 p-3.5">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-800">
                WinDbg / Visual Studio Symbol Path
              </span>
              <button
                type="button"
                onClick={() => handleCopy(windbgSymPath)}
                className="inline-flex items-center gap-1 rounded border border-slate-200 bg-white px-2 py-0.5 text-[11px] font-medium text-slate-700 hover:bg-slate-100"
              >
                {copiedText === windbgSymPath ? (
                  <>
                    <Check className="h-3 w-3 text-emerald-600" />
                    Copied
                  </>
                ) : (
                  <>
                    <Copy className="h-3 w-3" />
                    Copy
                  </>
                )}
              </button>
            </div>
            <pre className="mt-2 overflow-x-auto rounded bg-slate-950 p-2.5 font-mono text-xs text-slate-100">
              {windbgSymPath}
            </pre>
            <p className="mt-2 text-[11px] text-slate-500">
              Enable <code className="font-mono">archive_raw_symbols</code> in{" "}
              <Link to="/settings" className="text-indigo-600 hover:underline">
                Settings
              </Link>{" "}
              to allow WinDbg and Visual Studio to pull original PDB/EXE/DLL
              files from <code className="font-mono">/symsrv</code>.
            </p>
          </div>

          <div className="rounded-lg border border-slate-200 bg-slate-50 p-3.5">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-800">
                CI/CD Symbol Upload (<code className="font-mono">crashkiller_cli.py</code>)
              </span>
              <button
                type="button"
                onClick={() => handleCopy(cliSnippet)}
                className="inline-flex items-center gap-1 rounded border border-slate-200 bg-white px-2 py-0.5 text-[11px] font-medium text-slate-700 hover:bg-slate-100"
              >
                {copiedText === cliSnippet ? (
                  <>
                    <Check className="h-3 w-3 text-emerald-600" />
                    Copied
                  </>
                ) : (
                  <>
                    <Copy className="h-3 w-3" />
                    Copy
                  </>
                )}
              </button>
            </div>
            <pre className="mt-2 overflow-x-auto rounded bg-slate-950 p-2.5 font-mono text-xs text-slate-100">
              {cliSnippet}
            </pre>
            <p className="mt-2 text-[11px] text-slate-500">
              The CLI automatically pairs each <code className="font-mono">.pdb</code>{" "}
              with its matching <code className="font-mono">.exe/.dll</code> so{" "}
              <code className="font-mono">has_pe_unwind</code> remains enabled
              for x64 CFI unwinding.
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
              Delete Symbol
            </h2>
            <p className="mt-2 text-sm text-slate-600">
              Are you sure you want to delete{" "}
              <code className="font-mono font-semibold text-slate-900">
                {confirmDeleteSymbol.debug_file}
              </code>{" "}
              (<code className="font-mono text-xs">{confirmDeleteSymbol.debug_id}</code>)?
              This removes the indexed <code className="font-mono">.sym</code>{" "}
              and any archived raw PDB/PE binaries.
            </p>

            {deleteMutation.isError && (
              <div className="mt-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                {deleteMutation.error instanceof ApiError
                  ? deleteMutation.error.message
                  : "Failed to delete symbol."}
              </div>
            )}

            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setConfirmDeleteSymbol(null)}
                className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={deleteMutation.isPending}
                onClick={() => deleteMutation.mutate(confirmDeleteSymbol.id)}
                className="inline-flex items-center gap-1.5 rounded-md bg-rose-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-rose-700 disabled:opacity-50"
              >
                <Trash2 className="h-4 w-4" />
                {deleteMutation.isPending ? "Deleting..." : "Confirm Delete"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Upload Symbol Modal */}
      <UploadModal
        isOpen={isUploadOpen}
        onClose={() => setIsUploadOpen(false)}
        mode="symbol"
        projects={projects}
        defaultProjectKey={selectedProject?.key}
        onSuccess={() => {
          void queryClient.invalidateQueries({ queryKey: ["symbols"] });
          void queryClient.invalidateQueries({ queryKey: ["missing-symbols"] });
          void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
        }}
      />
    </div>
  );
}

export default SymbolsPage;
