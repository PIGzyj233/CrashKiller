import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  AlertTriangle,
  Bug,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clock,
  Loader2,
  Pin,
  RefreshCw,
  RotateCcw,
  Search,
  Sparkles,
  Upload,
  X,
} from "lucide-react";
import { ApiError, listCrashes, pinCrash } from "../api/client";
import type { AIVerdict, CrashStatus, CrashSummary } from "../api/types";
import { useProjectContext } from "../components/Layout";
import { UploadModal } from "../components/UploadModal";

const PAGE_SIZE = 25;

const CRASH_STATUSES: readonly CrashStatus[] = [
  "PENDING",
  "PARSING",
  "TRIAGING",
  "ANALYZED",
  "FAILED",
];

const AI_VERDICTS: readonly AIVerdict[] = [
  "KNOWN_ISSUE",
  "NEW_ISSUE",
  "REGRESSION",
  "NEEDS_REVIEW",
];

const STATUS_BADGE_CLASSES: Record<CrashStatus, string> = {
  PENDING: "border-slate-200 bg-slate-100 text-slate-700",
  PARSING: "border-sky-200 bg-sky-50 text-sky-700",
  TRIAGING: "border-indigo-200 bg-indigo-50 text-indigo-700",
  ANALYZED: "border-emerald-200 bg-emerald-50 text-emerald-700",
  FAILED: "border-rose-200 bg-rose-50 text-rose-700",
};

const VERDICT_BADGE_CLASSES: Record<AIVerdict, string> = {
  KNOWN_ISSUE: "border-emerald-200 bg-emerald-50 text-emerald-700",
  NEW_ISSUE: "border-sky-200 bg-sky-50 text-sky-700",
  REGRESSION: "border-rose-200 bg-rose-50 text-rose-700",
  NEEDS_REVIEW: "border-amber-200 bg-amber-50 text-amber-700",
};

function isValidCrashStatus(val: string | null): val is CrashStatus {
  return val !== null && (CRASH_STATUSES as readonly string[]).includes(val);
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

function formatConfidence(confidence: number | null): string | null {
  if (confidence === null || !Number.isFinite(confidence)) {
    return null;
  }
  const pct = confidence <= 1 ? confidence * 100 : confidence;
  return `${Math.round(pct)}%`;
}

export function CrashesPage() {
  const { projects, selectedProject, selectedProjectId } = useProjectContext();
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // URL-backed filters (so Dashboard / Issue / Cluster links work directly)
  const rawUrlStatus = searchParams.get("status");
  const statusFilter: "" | CrashStatus = isValidCrashStatus(rawUrlStatus)
    ? rawUrlStatus
    : "";
  const rawIssueId = searchParams.get("issue_id");
  const parsedIssueId =
    rawIssueId && Number.isInteger(Number(rawIssueId)) && Number(rawIssueId) > 0
      ? Number(rawIssueId)
      : undefined;
  const clusterIdFilter = searchParams.get("cluster_id")?.trim() || undefined;

  // Local filters
  const [verdictFilter, setVerdictFilter] = useState<"" | AIVerdict>("");
  const [appVersionInput, setAppVersionInput] = useState("");
  const [debouncedAppVersion, setDebouncedAppVersion] = useState("");
  const [verifiedFilter, setVerifiedFilter] = useState<
    "all" | "verified" | "unverified"
  >("all");
  const [pinnedFilter, setPinnedFilter] = useState<
    "all" | "pinned" | "unpinned"
  >("all");
  const [searchInput, setSearchInput] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [pinningCrashId, setPinningCrashId] = useState<string | null>(null);

  // UploadModal state + strict nav ref to avoid onSuccess -> onClose race
  const [manualUploadOpen, setManualUploadOpen] = useState(false);
  const isUploadModalOpen =
    manualUploadOpen || searchParams.get("upload") === "1";
  const pendingCrashNavRef = useRef<string | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedQ(searchInput.trim());
      setDebouncedAppVersion(appVersionInput.trim());
    }, 300);
    return () => window.clearTimeout(timer);
  }, [searchInput, appVersionInput]);

  useEffect(() => {
    setOffset(0);
  }, [
    selectedProjectId,
    statusFilter,
    parsedIssueId,
    clusterIdFilter,
    verdictFilter,
    debouncedAppVersion,
    verifiedFilter,
    pinnedFilter,
    debouncedQ,
  ]);

  const projectById = useMemo(
    () => new Map(projects.map((p) => [p.id, p])),
    [projects],
  );

  const isPinnedParam =
    pinnedFilter === "pinned"
      ? true
      : pinnedFilter === "unpinned"
        ? false
        : undefined;
  const isVerifiedParam =
    verifiedFilter === "verified"
      ? true
      : verifiedFilter === "unverified"
        ? false
        : undefined;

  const crashesQuery = useQuery({
    queryKey: [
      "crashes",
      {
        project_id: selectedProjectId || "",
        status: statusFilter,
        ai_verdict: verdictFilter,
        issue_id: parsedIssueId ?? null,
        cluster_id: clusterIdFilter ?? null,
        app_version: debouncedAppVersion,
        is_pinned: isPinnedParam ?? null,
        is_human_verified: isVerifiedParam ?? null,
        q: debouncedQ,
        limit: PAGE_SIZE,
        offset,
      },
    ],
    queryFn: () =>
      listCrashes({
        project_id: selectedProjectId || undefined,
        status: statusFilter || undefined,
        ai_verdict: verdictFilter || undefined,
        issue_id: parsedIssueId,
        cluster_id: clusterIdFilter,
        app_version: debouncedAppVersion || undefined,
        is_pinned: isPinnedParam,
        is_human_verified: isVerifiedParam,
        q: debouncedQ || undefined,
        limit: PAGE_SIZE,
        offset,
      }),
    placeholderData: keepPreviousData,
    refetchInterval: (query) => {
      const items = query.state.data?.items ?? [];
      const hasInFlight = items.some(
        (c) =>
          c.status === "PENDING" ||
          c.status === "PARSING" ||
          c.status === "TRIAGING",
      );
      return hasInFlight ? 4000 : false;
    },
  });

  const pinMutation = useMutation({
    mutationFn: ({ id, isPinned }: { id: string; isPinned: boolean }) =>
      pinCrash(id, isPinned),
    onMutate: ({ id }) => {
      setPinningCrashId(id);
    },
    onSuccess: (res) => {
      void queryClient.invalidateQueries({ queryKey: ["crashes"] });
      void queryClient.invalidateQueries({ queryKey: ["crash", res.crash_id] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
    onSettled: () => {
      setPinningCrashId(null);
    },
  });

  const setUrlParam = (key: string, value: string | undefined) => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value) {
          next.set(key, value);
        } else {
          next.delete(key);
        }
        return next;
      },
      { replace: true },
    );
  };

  const handleResetFilters = () => {
    setVerdictFilter("");
    setAppVersionInput("");
    setVerifiedFilter("all");
    setPinnedFilter("all");
    setSearchInput("");
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete("status");
        next.delete("issue_id");
        next.delete("cluster_id");
        return next;
      },
      { replace: true },
    );
  };

  const handleCloseUpload = () => {
    setManualUploadOpen(false);
    if (pendingCrashNavRef.current) {
      const crashId = pendingCrashNavRef.current;
      pendingCrashNavRef.current = null;
      navigate(`/crashes/${encodeURIComponent(crashId)}`);
      return;
    }
    if (searchParams.has("upload")) {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.delete("upload");
          return next;
        },
        { replace: true },
      );
    }
  };

  const crashes = crashesQuery.data?.items ?? [];
  const total = crashesQuery.data?.total ?? 0;
  const fromIdx = total === 0 ? 0 : Math.min(offset + 1, total);
  const toIdx = Math.min(total, offset + PAGE_SIZE);

  const hasAnyFilter = Boolean(
    statusFilter ||
      verdictFilter ||
      appVersionInput ||
      verifiedFilter !== "all" ||
      pinnedFilter !== "all" ||
      searchInput ||
      parsedIssueId ||
      clusterIdFilter,
  );

  const scopeLabel = selectedProject
    ? `${selectedProject.name} (${selectedProject.key})`
    : selectedProjectId || "All Projects";

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-indigo-50 text-indigo-600">
            <Bug className="h-5 w-5" />
          </div>
          <div>
            <h1 className="text-xl font-semibold text-slate-900">Crashes</h1>
            <p className="text-sm text-slate-500">
              Uploaded minidumps, pipeline status, AI verdicts, and human
              verification
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2.5">
          <span className="rounded-full border border-slate-200 bg-white px-3 py-1 text-xs font-medium text-slate-700">
            Scope: {scopeLabel}
          </span>
          <button
            type="button"
            onClick={() => void crashesQuery.refetch()}
            disabled={crashesQuery.isFetching}
            className="inline-flex items-center gap-1.5 rounded-md border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 shadow-sm hover:bg-slate-50 disabled:opacity-60"
          >
            <RefreshCw
              className={`h-3.5 w-3.5 ${
                crashesQuery.isFetching ? "animate-spin" : ""
              }`}
            />
            Refresh
          </button>
          <button
            type="button"
            onClick={() => setManualUploadOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-indigo-700"
          >
            <Upload className="h-4 w-4" />
            Upload Crash
          </button>
        </div>
      </div>

      {/* Filters Toolbar */}
      <div className="space-y-3 rounded-xl border border-slate-200 bg-white p-3.5 shadow-sm">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-6">
          <div className="relative lg:col-span-2">
            <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
            <input
              type="text"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              placeholder="Search crash reason, address, OS, build..."
              className="h-9 w-full rounded-md border border-slate-300 pl-9 pr-3 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />
          </div>

          <select
            aria-label="Filter by crash status"
            value={statusFilter}
            onChange={(e) =>
              setUrlParam("status", e.target.value || undefined)
            }
            className="h-9 rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-800 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
          >
            <option value="">All Statuses</option>
            {CRASH_STATUSES.map((st) => (
              <option key={st} value={st}>
                {st}
              </option>
            ))}
          </select>

          <select
            aria-label="Filter by AI verdict"
            value={verdictFilter}
            onChange={(e) =>
              setVerdictFilter(e.target.value as "" | AIVerdict)
            }
            className="h-9 rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-800 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
          >
            <option value="">All AI Verdicts</option>
            {AI_VERDICTS.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>

          <input
            type="text"
            value={appVersionInput}
            onChange={(e) => setAppVersionInput(e.target.value)}
            placeholder="App version..."
            aria-label="Filter by app version"
            className="h-9 rounded-md border border-slate-300 px-3 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
          />

          <div className="flex gap-2">
            <select
              aria-label="Filter by human verification"
              value={verifiedFilter}
              onChange={(e) =>
                setVerifiedFilter(
                  e.target.value as "all" | "verified" | "unverified",
                )
              }
              className="h-9 flex-1 rounded-md border border-slate-300 bg-white px-2 text-xs text-slate-800"
            >
              <option value="all">Verification: All</option>
              <option value="verified">Human Verified</option>
              <option value="unverified">Unverified</option>
            </select>

            <select
              aria-label="Filter by pin status"
              value={pinnedFilter}
              onChange={(e) =>
                setPinnedFilter(
                  e.target.value as "all" | "pinned" | "unpinned",
                )
              }
              className="h-9 flex-1 rounded-md border border-slate-300 bg-white px-2 text-xs text-slate-800"
            >
              <option value="all">Pin: All</option>
              <option value="pinned">Pinned</option>
              <option value="unpinned">Unpinned</option>
            </select>
          </div>
        </div>

        {/* Active URL Scope Chips + Reset */}
        {(parsedIssueId !== undefined || clusterIdFilter || hasAnyFilter) && (
          <div className="flex flex-wrap items-center gap-2 pt-1 text-xs">
            {parsedIssueId !== undefined && (
              <span className="inline-flex items-center gap-1 rounded-full border border-indigo-200 bg-indigo-50 px-2.5 py-0.5 font-medium text-indigo-700">
                Issue #{parsedIssueId}
                <button
                  type="button"
                  onClick={() => setUrlParam("issue_id", undefined)}
                  className="rounded-full p-0.5 hover:bg-indigo-100"
                  aria-label="Clear issue filter"
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            )}
            {clusterIdFilter && (
              <span className="inline-flex items-center gap-1 rounded-full border border-indigo-200 bg-indigo-50 px-2.5 py-0.5 font-mono font-medium text-indigo-700">
                Cluster {clusterIdFilter.slice(0, 8)}
                <button
                  type="button"
                  onClick={() => setUrlParam("cluster_id", undefined)}
                  className="rounded-full p-0.5 hover:bg-indigo-100"
                  aria-label="Clear cluster filter"
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            )}
            {hasAnyFilter && (
              <button
                type="button"
                onClick={handleResetFilters}
                className="inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium text-slate-600 hover:bg-slate-100 hover:text-slate-900"
              >
                <RotateCcw className="h-3 w-3" />
                Reset Filters
              </button>
            )}
          </div>
        )}
      </div>

      {/* Error Banner */}
      {crashesQuery.isError && (
        <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          <div className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 text-rose-600" />
            <span>
              {crashesQuery.error instanceof ApiError
                ? crashesQuery.error.message
                : "Failed to load crashes."}
            </span>
          </div>
          <button
            type="button"
            onClick={() => void crashesQuery.refetch()}
            className="rounded border border-rose-300 bg-white px-2.5 py-1 text-xs font-medium text-rose-700"
          >
            Retry
          </button>
        </div>
      )}

      {pinMutation.isError && (
        <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          <div className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 text-rose-600" />
            <span>
              {pinMutation.error instanceof ApiError
                ? pinMutation.error.message
                : "Failed to update crash pin status."}
            </span>
          </div>
          <button
            type="button"
            onClick={() => pinMutation.reset()}
            className="rounded p-1 text-rose-700 hover:bg-rose-100"
            aria-label="Dismiss pin error"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {/* Crashes Table */}
      <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-200 text-left text-sm">
            <thead className="bg-slate-50 text-xs font-medium uppercase tracking-wider text-slate-500">
              <tr>
                <th className="px-4 py-3">Crash ID</th>
                <th className="px-4 py-3">Version / OS</th>
                <th className="px-4 py-3">Reason / Issue</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3">AI Verdict</th>
                <th className="px-4 py-3">Dump Size</th>
                <th className="px-4 py-3">Crash Time</th>
                <th className="px-4 py-3 text-right">Pin</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {crashesQuery.isPending ? (
                <tr>
                  <td
                    colSpan={8}
                    className="px-4 py-10 text-center text-sm text-slate-500"
                  >
                    Loading crashes...
                  </td>
                </tr>
              ) : crashes.length === 0 ? (
                <tr>
                  <td
                    colSpan={8}
                    className="px-4 py-12 text-center text-sm text-slate-500"
                  >
                    No crash reports match the current filters.
                  </td>
                </tr>
              ) : (
                crashes.map((crash: CrashSummary) => {
                  const project = projectById.get(crash.project_id);
                  const confLabel = formatConfidence(crash.ai_confidence);
                  const isInFlight =
                    crash.status === "PENDING" ||
                    crash.status === "PARSING" ||
                    crash.status === "TRIAGING";

                  return (
                    <tr key={crash.id} className="hover:bg-slate-50">
                      <td className="px-4 py-3">
                        <Link
                          to={`/crashes/${encodeURIComponent(crash.id)}`}
                          className="font-mono text-xs font-semibold text-indigo-600 hover:underline"
                        >
                          {crash.id.slice(0, 8)}
                        </Link>
                        {!selectedProjectId && project && (
                          <div className="mt-1">
                            <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-600">
                              {project.key}
                            </span>
                          </div>
                        )}
                      </td>

                      <td className="px-4 py-3 text-xs">
                        <div className="font-mono font-medium text-slate-800">
                          {crash.app_version}
                          {crash.build_id ? ` (${crash.build_id})` : ""}
                        </div>
                        <div className="mt-0.5 text-[11px] text-slate-500">
                          {crash.os_version || "Unknown OS"}
                        </div>
                      </td>

                      <td className="max-w-xs px-4 py-3 text-xs">
                        <div className="truncate font-mono text-slate-800">
                          {crash.crash_reason || "Pending symbolication..."}
                        </div>
                        <div className="mt-0.5 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
                          {crash.crash_address && (
                            <span className="font-mono">
                              @ {crash.crash_address}
                            </span>
                          )}
                          {crash.issue_id !== null && (
                            <Link
                              to={`/issues/${crash.issue_id}`}
                              className="font-medium text-indigo-600 hover:underline"
                            >
                              Issue #{crash.issue_id}
                            </Link>
                          )}
                        </div>
                      </td>

                      <td className="px-4 py-3">
                        <span
                          className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-medium ${
                            STATUS_BADGE_CLASSES[crash.status]
                          }`}
                        >
                          {isInFlight ? (
                            <Loader2 className="h-3 w-3 animate-spin" />
                          ) : crash.status === "ANALYZED" ? (
                            <CheckCircle2 className="h-3 w-3" />
                          ) : (
                            <Clock className="h-3 w-3" />
                          )}
                          {crash.status}
                        </span>
                      </td>

                      <td className="px-4 py-3 text-xs">
                        <div className="flex flex-wrap items-center gap-1.5">
                          {crash.ai_verdict ? (
                            <span
                              className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium ${
                                VERDICT_BADGE_CLASSES[crash.ai_verdict]
                              }`}
                            >
                              <Sparkles className="h-3 w-3" />
                              {crash.ai_verdict}
                              {confLabel ? ` (${confLabel})` : ""}
                            </span>
                          ) : (
                            <span className="text-slate-400">—</span>
                          )}

                          {crash.is_human_verified && (
                            <span className="inline-flex items-center gap-1 rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700">
                              <CheckCircle2 className="h-3 w-3" />
                              Verified
                            </span>
                          )}
                        </div>
                      </td>

                      <td className="px-4 py-3 font-mono text-xs text-slate-600">
                        <div>{formatBytes(crash.dump_compressed_size)}</div>
                        {crash.dump_raw_size > 0 && (
                          <div className="text-[11px] text-slate-400">
                            raw {formatBytes(crash.dump_raw_size)}
                          </div>
                        )}
                      </td>

                      <td className="px-4 py-3 text-xs text-slate-600">
                        {formatDateTime(crash.crash_time || crash.created_at)}
                      </td>

                      <td className="px-4 py-3 text-right">
                        <button
                          type="button"
                          disabled={
                            pinMutation.isPending &&
                            pinningCrashId === crash.id
                          }
                          onClick={() =>
                            pinMutation.mutate({
                              id: crash.id,
                              isPinned: !crash.is_pinned,
                            })
                          }
                          aria-label={
                            crash.is_pinned
                              ? `Unpin crash ${crash.id}`
                              : `Pin crash ${crash.id}`
                          }
                          className={`inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs font-medium transition-colors ${
                            crash.is_pinned
                              ? "border-amber-300 bg-amber-50 text-amber-800 hover:bg-amber-100"
                              : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
                          }`}
                        >
                          <Pin className="h-3.5 w-3.5" />
                          {crash.is_pinned ? "Pinned" : "Pin"}
                        </button>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>

        {/* Pagination Footer */}
        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 bg-slate-50 px-4 py-3 text-xs text-slate-600">
          <span>
            Showing {fromIdx}–{toIdx} of {total.toLocaleString()}
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={offset === 0}
              onClick={() => setOffset((prev) => Math.max(0, prev - PAGE_SIZE))}
              className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1 font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
              Previous
            </button>
            <button
              type="button"
              disabled={offset + PAGE_SIZE >= total}
              onClick={() => setOffset((prev) => prev + PAGE_SIZE)}
              className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1 font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Next
              <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      </div>

      {/* UploadModal */}
      <UploadModal
        isOpen={isUploadModalOpen}
        onClose={handleCloseUpload}
        mode="crash"
        projects={projects}
        defaultProjectKey={selectedProject?.key}
        onSuccess={(result) => {
          if (result.mode === "crash") {
            pendingCrashNavRef.current = result.crashId;
          }
          void queryClient.invalidateQueries({ queryKey: ["crashes"] });
          void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
          void queryClient.invalidateQueries({ queryKey: ["symbols"] });
        }}
      />
    </div>
  );
}

export default CrashesPage;
