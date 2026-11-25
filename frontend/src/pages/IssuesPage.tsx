import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowRightLeft,
  Check,
  ChevronLeft,
  ChevronRight,
  Copy,
  GitMerge,
  Layers,
  Pin,
  Plus,
  Search,
  X,
} from "lucide-react";
import {
  ApiError,
  batchTransitionIssues,
  createIssue,
  listClusters,
  listIssues,
  mergeIssues,
  transitionIssue,
  updateIssue,
} from "../api/client";
import type {
  CrashClusterRead,
  IssueRead,
  IssueStatus,
  SignatureFrameItem,
} from "../api/types";
import {
  ISSUE_STATUSES,
  IssueTransitionModal,
  IssueTransitionSplitButton,
  STATUS_BADGE_CLASSES,
  type IssueTransitionModalSubmitPayload,
  type TransitionActionConfig,
} from "../components/IssueStatusWorkflow";
import { useProjectContext } from "../components/Layout";
import { useAppI18n } from "../i18n";

const PAGE_SIZE = 25;

function formatSignatureFrames(
  frames: SignatureFrameItem[],
  fallbackText?: string | null,
): string {
  if (!Array.isArray(frames) || frames.length === 0) {
    return fallbackText || "—";
  }
  const formatted = frames.slice(0, 4).map((frame) => {
    if (typeof frame === "string") {
      return frame;
    }
    const mod = frame.module ? `${frame.module}!` : "";
    const fn = frame.function || "<unknown>";
    return `${mod}${fn}`;
  });
  return formatted.join(" ← ");
}

function parsePatternLines(raw: string): string[] {
  return raw
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
}

export function IssuesPage() {
  const { t, formatDateTime, formatNumber, formatIssueStatus } = useAppI18n();
  const { projects, selectedProject, selectedProjectId } = useProjectContext();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();

  const activeTab: "issues" | "clusters" =
    searchParams.get("tab") === "clusters" ? "clusters" : "issues";

  const setActiveTab = (tab: "issues" | "clusters") => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (tab === "clusters") {
          next.set("tab", "clusters");
        } else {
          next.delete("tab");
        }
        return next;
      },
      { replace: true },
    );
  };

  // Issue filters
  const [searchInput, setSearchInput] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  const [statusFilter, setStatusFilter] = useState<"" | IssueStatus>("");
  const [componentInput, setComponentInput] = useState("");
  const [debouncedComponent, setDebouncedComponent] = useState("");
  const [pinnedOnly, setPinnedOnly] = useState(false);
  const [issuesPage, setIssuesPage] = useState(0);

  // Cluster filters
  const [unassignedOnly, setUnassignedOnly] = useState(false);
  const [clustersPage, setClustersPage] = useState(0);

  // Multi-select merge state (keyed by issue id to preserve objects across pages)
  const [selectedIssuesMap, setSelectedIssuesMap] = useState<
    Map<number, IssueRead>
  >(() => new Map());
  const [isMergeModalOpen, setIsMergeModalOpen] = useState(false);
  const [mergeTargetId, setMergeTargetId] = useState<number | null>(null);

  // Create issue modal state
  const [isCreateModalOpen, setIsCreateModalOpen] = useState(false);
  const [newProjectId, setNewProjectId] = useState("");
  const [newTitle, setNewTitle] = useState("");
  const [newStatus, setNewStatus] = useState<IssueStatus>("NEW");
  const [newComponent, setNewComponent] = useState("");
  const [newFixedInVersion, setNewFixedInVersion] = useState("");
  const [newRootCause, setNewRootCause] = useState("");
  const [newLogPatternsText, setNewLogPatternsText] = useState("");
  const [newIsAmbiguous, setNewIsAmbiguous] = useState(false);
  const [newIsPinned, setNewIsPinned] = useState(false);

  // Feedback / copy / transition state
  const [pinningIssueId, setPinningIssueId] = useState<number | null>(null);
  const [transitioningIssueId, setTransitioningIssueId] = useState<
    number | null
  >(null);
  const [transitionModal, setTransitionModal] = useState<
    | { mode: "single"; issue: IssueRead; targetStatus: IssueStatus }
    | { mode: "batch"; targetStatus: IssueStatus }
    | null
  >(null);
  const [copiedHash, setCopiedHash] = useState<string | null>(null);
  const [actionFeedback, setActionFeedback] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedQ(searchInput.trim());
      setDebouncedComponent(componentInput.trim());
    }, 250);
    return () => window.clearTimeout(timer);
  }, [searchInput, componentInput]);

  useEffect(() => {
    setIssuesPage(0);
    setSelectedIssuesMap(new Map());
  }, [
    selectedProjectId,
    debouncedQ,
    statusFilter,
    debouncedComponent,
    pinnedOnly,
  ]);

  useEffect(() => {
    if (
      isCreateModalOpen &&
      !selectedProjectId &&
      !newProjectId &&
      projects.length > 0
    ) {
      setNewProjectId(projects[0].id);
    }
  }, [isCreateModalOpen, selectedProjectId, newProjectId, projects]);

  useEffect(() => {
    setClustersPage(0);
  }, [selectedProjectId, unassignedOnly]);

  const projectById = useMemo(
    () => new Map(projects.map((p) => [p.id, p])),
    [projects],
  );

  const issuesQuery = useQuery({
    queryKey: [
      "issues",
      {
        project_id: selectedProjectId || "",
        status: statusFilter,
        component: debouncedComponent,
        pinnedOnly,
        q: debouncedQ,
        page: issuesPage,
        limit: PAGE_SIZE,
      },
    ],
    queryFn: () =>
      listIssues({
        project_id: selectedProjectId || undefined,
        status: statusFilter || undefined,
        component: debouncedComponent || undefined,
        is_pinned: pinnedOnly ? true : undefined,
        q: debouncedQ || undefined,
        limit: PAGE_SIZE,
        offset: issuesPage * PAGE_SIZE,
      }),
    placeholderData: keepPreviousData,
    enabled: activeTab === "issues",
  });

  const clustersQuery = useQuery({
    queryKey: [
      "clusters",
      {
        project_id: selectedProjectId || "",
        unassignedOnly,
        page: clustersPage,
        limit: PAGE_SIZE,
      },
    ],
    queryFn: () =>
      listClusters({
        project_id: selectedProjectId || undefined,
        unassigned_only: unassignedOnly ? true : undefined,
        limit: PAGE_SIZE,
        offset: clustersPage * PAGE_SIZE,
      }),
    placeholderData: keepPreviousData,
    enabled: activeTab === "clusters",
  });

  const pinMutation = useMutation({
    mutationFn: ({ id, isPinned }: { id: number; isPinned: boolean }) =>
      updateIssue(id, { is_pinned: isPinned }),
    onMutate: ({ id }) => {
      setPinningIssueId(id);
    },
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: ["issues"] });
      void queryClient.invalidateQueries({ queryKey: ["issue", updated.id] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
    onSettled: () => {
      setPinningIssueId(null);
    },
  });

  const mergeMutation = useMutation({
    mutationFn: mergeIssues,
    onSuccess: (merged) => {
      setSelectedIssuesMap(new Map());
      setIsMergeModalOpen(false);
      setMergeTargetId(null);
      setActionFeedback(
        t("issues.mergedFeedback", { id: merged.id, title: merged.title }),
      );
      void queryClient.invalidateQueries({ queryKey: ["issues"] });
      void queryClient.invalidateQueries({ queryKey: ["clusters"] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  const createMutation = useMutation({
    mutationFn: createIssue,
    onSuccess: (created) => {
      setIsCreateModalOpen(false);
      setNewTitle("");
      setNewStatus("NEW");
      setNewComponent("");
      setNewFixedInVersion("");
      setNewRootCause("");
      setNewLogPatternsText("");
      setNewIsAmbiguous(false);
      setNewIsPinned(false);
      setActionError(null);
      setActionFeedback(
        t("issues.createdFeedback", { id: created.id, title: created.title }),
      );
      void queryClient.invalidateQueries({ queryKey: ["issues"] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  const transitionMutation = useMutation({
    mutationFn: ({
      id,
      payload,
    }: {
      id: number;
      payload: IssueTransitionModalSubmitPayload;
    }) => transitionIssue(id, payload),
    onMutate: ({ id }) => {
      setTransitioningIssueId(id);
      setActionError(null);
    },
    onSuccess: (res) => {
      setTransitionModal(null);
      setSelectedIssuesMap((prev) => {
        if (!prev.has(res.issue.id)) {
          return prev;
        }
        const next = new Map(prev);
        next.set(res.issue.id, res.issue);
        return next;
      });
      setActionFeedback(
        t("issues.transitionFeedback", {
          id: res.issue.id,
          from: formatIssueStatus(res.event.from_status ?? res.issue.status),
          to: formatIssueStatus(res.issue.status),
        }),
      );
      void queryClient.invalidateQueries({ queryKey: ["issues"] });
      void queryClient.invalidateQueries({ queryKey: ["issue", res.issue.id] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
    onError: (err) => {
      if (!transitionModal) {
        setActionError(
          err instanceof ApiError ? err.message : t("issues.transitionFailed"),
        );
      }
    },
    onSettled: () => {
      setTransitioningIssueId(null);
    },
  });

  const batchTransitionMutation = useMutation({
    mutationFn: batchTransitionIssues,
    onSuccess: (res, vars) => {
      setTransitionModal(null);
      setSelectedIssuesMap(new Map());
      setActionError(null);
      setActionFeedback(
        res.skipped_count > 0
          ? t("issues.batchTransitionWithSkippedFeedback", {
              updated: res.updated_count,
              skipped: res.skipped_count,
              to: formatIssueStatus(vars.to_status),
            })
          : t("issues.batchTransitionFeedback", {
              updated: res.updated_count,
              to: formatIssueStatus(vars.to_status),
            }),
      );
      void queryClient.invalidateQueries({ queryKey: ["issues"] });
      void queryClient.invalidateQueries({ queryKey: ["issue"] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  const issuesList = useMemo(
    () => issuesQuery.data?.items ?? [],
    [issuesQuery.data?.items],
  );
  const issuesTotal = issuesQuery.data?.total ?? 0;

  const selectedIssues = useMemo(
    () => Array.from(selectedIssuesMap.values()),
    [selectedIssuesMap],
  );
  const selectedIssueIds = useMemo(
    () => Array.from(selectedIssuesMap.keys()),
    [selectedIssuesMap],
  );

  const selectedProjectIdsSet = useMemo(
    () => new Set(selectedIssues.map((i) => i.project_id)),
    [selectedIssues],
  );
  const hasCrossProjectSelection = selectedProjectIdsSet.size > 1;

  const allOnPageSelected =
    issuesList.length > 0 &&
    issuesList.every((issue) => selectedIssuesMap.has(issue.id));

  const toggleSelectAllOnPage = () => {
    setSelectedIssuesMap((prev) => {
      const next = new Map(prev);
      if (allOnPageSelected) {
        for (const issue of issuesList) {
          next.delete(issue.id);
        }
      } else {
        for (const issue of issuesList) {
          next.set(issue.id, issue);
        }
      }
      return next;
    });
  };

  const toggleSelectIssue = (issue: IssueRead) => {
    setSelectedIssuesMap((prev) => {
      const next = new Map(prev);
      if (next.has(issue.id)) {
        next.delete(issue.id);
      } else {
        next.set(issue.id, issue);
      }
      return next;
    });
  };

  const openMergeModal = () => {
    if (selectedIssues.length < 2 || hasCrossProjectSelection) {
      return;
    }
    setMergeTargetId(selectedIssues[0].id);
    mergeMutation.reset();
    setIsMergeModalOpen(true);
  };

  const handleConfirmMerge = () => {
    if (
      mergeTargetId === null ||
      hasCrossProjectSelection ||
      selectedIssueIds.length < 2
    ) {
      return;
    }
    const sourceIssueIds = selectedIssueIds.filter(
      (id) => id !== mergeTargetId,
    );
    mergeMutation.mutate({
      target_issue_id: mergeTargetId,
      source_issue_ids: sourceIssueIds,
    });
  };

  const effectiveCreateProjectId =
    selectedProjectId || newProjectId || projects[0]?.id || "";

  const openCreateModal = () => {
    setNewProjectId(selectedProjectId || projects[0]?.id || "");
    createMutation.reset();
    setIsCreateModalOpen(true);
  };

  const handleCreateSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!effectiveCreateProjectId || !newTitle.trim()) {
      return;
    }
    createMutation.mutate({
      project_id: effectiveCreateProjectId,
      title: newTitle.trim(),
      status: newStatus,
      component: newComponent.trim() || null,
      fixed_in_version: newFixedInVersion.trim() || null,
      root_cause: newRootCause.trim() || null,
      log_patterns_json: parsePatternLines(newLogPatternsText),
      is_ambiguous_stack: newIsAmbiguous,
      is_pinned: newIsPinned,
    });
  };

  const handleRowTransitionAction = (
    issue: IssueRead,
    action: TransitionActionConfig,
  ) => {
    setActionError(null);
    if (!action.requiresModal) {
      transitionMutation.mutate({
        id: issue.id,
        payload: { to_status: action.toStatus },
      });
      return;
    }
    transitionMutation.reset();
    setTransitionModal({
      mode: "single",
      issue,
      targetStatus: action.toStatus,
    });
  };

  const openBatchTransitionModal = () => {
    if (selectedIssues.length === 0) {
      return;
    }
    const allNew = selectedIssues.every((item) => item.status === "NEW");
    const defaultTarget: IssueStatus = allNew ? "KNOWN" : "FIXED";
    batchTransitionMutation.reset();
    setActionError(null);
    setTransitionModal({
      mode: "batch",
      targetStatus: defaultTarget,
    });
  };

  const handleTransitionModalSubmit = (
    payload: IssueTransitionModalSubmitPayload,
  ) => {
    if (!transitionModal) {
      return;
    }
    if (transitionModal.mode === "single") {
      transitionMutation.mutate({
        id: transitionModal.issue.id,
        payload,
      });
    } else {
      batchTransitionMutation.mutate({
        issue_ids: selectedIssueIds,
        ...payload,
      });
    }
  };

  const handleCopyHash = (hash: string) => {
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      void navigator.clipboard.writeText(hash);
      setCopiedHash(hash);
      window.setTimeout(() => setCopiedHash(null), 1500);
    }
  };

  const scopeLabel = selectedProject
    ? `${selectedProject.name} (${selectedProject.key})`
    : selectedProjectId || t("nav.allProjects");

  const issuesFrom = issuesTotal === 0 ? 0 : issuesPage * PAGE_SIZE + 1;
  const issuesTo = Math.min(issuesTotal, (issuesPage + 1) * PAGE_SIZE);

  const clustersList = clustersQuery.data?.items ?? [];
  const clustersTotal = clustersQuery.data?.total ?? 0;
  const clustersFrom = clustersTotal === 0 ? 0 : clustersPage * PAGE_SIZE + 1;
  const clustersTo = Math.min(clustersTotal, (clustersPage + 1) * PAGE_SIZE);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-indigo-50 text-indigo-600">
            <Layers className="h-5 w-5" />
          </div>
          <div>
            <h1 className="text-xl font-semibold text-slate-900">
              {t("issues.title")}
            </h1>
            <p className="text-sm text-slate-500">{t("issues.subtitle")}</p>
          </div>
        </div>

        <div className="flex items-center gap-2.5">
          <span className="rounded-full border border-slate-200 bg-white px-3 py-1 text-xs font-medium text-slate-700">
            {t("issues.scope", { scope: scopeLabel })}
          </span>
          <button
            type="button"
            onClick={openCreateModal}
            className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-indigo-700"
          >
            <Plus className="h-4 w-4" />
            {t("issues.newIssue")}
          </button>
        </div>
      </div>

      {/* Feedback Banner */}
      {actionFeedback && (
        <div className="flex items-center justify-between rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-2.5 text-sm text-emerald-800">
          <span>{actionFeedback}</span>
          <button
            type="button"
            onClick={() => setActionFeedback(null)}
            className="rounded p-1 text-emerald-700 hover:bg-emerald-100"
            aria-label={t("issues.dismissNotification")}
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {actionError && (
        <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-4 py-2.5 text-sm text-rose-800">
          <span>{actionError}</span>
          <button
            type="button"
            onClick={() => setActionError(null)}
            className="rounded p-1 text-rose-700 hover:bg-rose-100"
            aria-label={t("issues.dismissNotification")}
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {/* Tabs */}
      <div className="border-b border-slate-200">
        <nav className="-mb-px flex gap-6" aria-label={t("issues.tabsAria")}>
          <button
            type="button"
            onClick={() => setActiveTab("issues")}
            className={`border-b-2 pb-3 text-sm font-medium transition-colors ${
              activeTab === "issues"
                ? "border-indigo-600 text-indigo-600"
                : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-700"
            }`}
          >
            {t("issues.tabKnownIssues")}
            {typeof issuesQuery.data?.total === "number" && (
              <span className="ml-2 rounded-full bg-slate-100 px-2 py-0.5 font-mono text-xs text-slate-700">
                {issuesQuery.data.total}
              </span>
            )}
          </button>

          <button
            type="button"
            onClick={() => setActiveTab("clusters")}
            className={`border-b-2 pb-3 text-sm font-medium transition-colors ${
              activeTab === "clusters"
                ? "border-indigo-600 text-indigo-600"
                : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-700"
            }`}
          >
            {t("issues.tabCrashClusters")}
            {typeof clustersQuery.data?.total === "number" && (
              <span className="ml-2 rounded-full bg-slate-100 px-2 py-0.5 font-mono text-xs text-slate-700">
                {clustersQuery.data.total}
              </span>
            )}
          </button>
        </nav>
      </div>

      {/* ISSUES TAB */}
      {activeTab === "issues" && (
        <div className="space-y-4">
          {/* Filter Bar */}
          <div className="flex flex-wrap items-center gap-3 rounded-xl border border-slate-200 bg-white p-3.5 shadow-sm">
            <div className="relative min-w-[220px] flex-1">
              <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
              <input
                type="text"
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                placeholder={t("issues.searchPlaceholder")}
                className="h-9 w-full rounded-md border border-slate-300 pl-9 pr-3 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
              />
            </div>

            <select
              aria-label={t("issues.filterStatusAria")}
              value={statusFilter}
              onChange={(e) =>
                setStatusFilter(e.target.value as "" | IssueStatus)
              }
              className="h-9 rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-800 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            >
              <option value="">{t("issues.allStatuses")}</option>
              {ISSUE_STATUSES.map((st) => (
                <option key={st} value={st}>
                  {formatIssueStatus(st)}
                </option>
              ))}
            </select>

            <input
              type="text"
              value={componentInput}
              onChange={(e) => setComponentInput(e.target.value)}
              placeholder={t("issues.componentPlaceholder")}
              aria-label={t("issues.filterComponentAria")}
              className="h-9 w-40 rounded-md border border-slate-300 px-3 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />

            <button
              type="button"
              onClick={() => setPinnedOnly((prev) => !prev)}
              className={`inline-flex h-9 items-center gap-1.5 rounded-md border px-3 text-sm font-medium transition-colors ${
                pinnedOnly
                  ? "border-amber-300 bg-amber-50 text-amber-800"
                  : "border-slate-300 bg-white text-slate-700 hover:bg-slate-50"
              }`}
            >
              <Pin className="h-3.5 w-3.5" />
              {t("issues.pinnedOnly")}
            </button>

            {(searchInput || statusFilter || componentInput || pinnedOnly) && (
              <button
                type="button"
                onClick={() => {
                  setSearchInput("");
                  setStatusFilter("");
                  setComponentInput("");
                  setPinnedOnly(false);
                }}
                className="h-9 rounded-md px-2.5 text-xs font-medium text-slate-600 hover:bg-slate-100 hover:text-slate-900"
              >
                {t("common.reset")}
              </button>
            )}
          </div>

          {/* Multi-select Batch Action Bar */}
          {selectedIssueIds.length >= 1 && (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-indigo-200 bg-indigo-50 px-4 py-2.5 text-sm">
              <div className="flex flex-wrap items-center gap-2 text-indigo-900">
                <ArrowRightLeft className="h-4 w-4 text-indigo-600" />
                <span className="font-medium">
                  {t("issues.selectedCount", {
                    count: selectedIssueIds.length,
                  })}
                </span>
                {selectedIssueIds.length >= 2 && hasCrossProjectSelection && (
                  <span className="rounded bg-rose-100 px-2 py-0.5 text-xs font-medium text-rose-700">
                    {t("issues.crossProjectWarning")}
                  </span>
                )}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => setSelectedIssuesMap(new Map())}
                  className="rounded-md border border-indigo-200 bg-white px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
                >
                  {t("common.clear")}
                </button>
                <button
                  type="button"
                  onClick={openBatchTransitionModal}
                  className="inline-flex items-center gap-1.5 rounded-md border border-indigo-300 bg-white px-3 py-1 text-xs font-medium text-indigo-700 hover:bg-indigo-100/60"
                >
                  <ArrowRightLeft className="h-3.5 w-3.5" />
                  {t("issues.batchTransitionButton")}
                </button>
                {selectedIssueIds.length >= 2 && (
                  <button
                    type="button"
                    disabled={hasCrossProjectSelection}
                    onClick={openMergeModal}
                    className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1 text-xs font-medium text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    <GitMerge className="h-3.5 w-3.5" />
                    {t("issues.mergeButton", {
                      count: selectedIssueIds.length,
                    })}
                  </button>
                )}
              </div>
            </div>
          )}

          {/* Issues Error */}
          {issuesQuery.isError && (
            <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
              <span>
                {issuesQuery.error instanceof Error
                  ? issuesQuery.error.message
                  : t("issues.loadIssuesFailed")}
              </span>
              <button
                type="button"
                onClick={() => void issuesQuery.refetch()}
                className="rounded border border-rose-300 bg-white px-2.5 py-1 text-xs font-medium text-rose-700"
              >
                {t("common.retry")}
              </button>
            </div>
          )}

          {/* Issues Table */}
          <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-200 text-left text-sm">
                <thead className="bg-slate-50 text-xs font-medium uppercase tracking-wider text-slate-500">
                  <tr>
                    <th className="w-10 px-4 py-3">
                      <input
                        type="checkbox"
                        aria-label={t("issues.selectAllAria")}
                        checked={allOnPageSelected}
                        onChange={toggleSelectAllOnPage}
                        className="h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
                      />
                    </th>
                    <th className="px-4 py-3">{t("issues.colIssue")}</th>
                    <th className="whitespace-nowrap px-4 py-3">
                      {t("issues.colStatus")}
                    </th>
                    <th className="whitespace-nowrap px-4 py-3">
                      {t("issues.colComponent")}
                    </th>
                    <th className="whitespace-nowrap px-4 py-3">
                      {t("issues.colCrashes")}
                    </th>
                    <th className="whitespace-nowrap px-4 py-3">
                      {t("issues.colLastSeen")}
                    </th>
                    <th className="whitespace-nowrap px-4 py-3 text-right">
                      {t("issues.colActions")}
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {issuesQuery.isPending ? (
                    <tr>
                      <td
                        colSpan={7}
                        className="px-4 py-10 text-center text-sm text-slate-500"
                      >
                        {t("issues.loadingIssues")}
                      </td>
                    </tr>
                  ) : issuesList.length === 0 ? (
                    <tr>
                      <td
                        colSpan={7}
                        className="px-4 py-12 text-center text-sm text-slate-500"
                      >
                        {t("issues.emptyIssues")}
                      </td>
                    </tr>
                  ) : (
                    issuesList.map((issue: IssueRead) => {
                      const project = projectById.get(issue.project_id);
                      const isSelected = selectedIssuesMap.has(issue.id);
                      return (
                        <tr
                          key={issue.id}
                          className={`hover:bg-slate-50 ${
                            isSelected ? "bg-indigo-50/40" : ""
                          }`}
                        >
                          <td className="px-4 py-3">
                            <input
                              type="checkbox"
                              aria-label={t("issues.selectIssueAria", {
                                id: issue.id,
                              })}
                              checked={isSelected}
                              onChange={() => toggleSelectIssue(issue)}
                              className="h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
                            />
                          </td>
                          <td className="px-4 py-3">
                            <div className="flex flex-wrap items-center gap-2">
                              <Link
                                to={`/issues/${issue.id}`}
                                className="font-medium text-slate-900 hover:text-indigo-600 hover:underline"
                              >
                                <span className="mr-1.5 font-mono text-xs text-slate-500">
                                  #{issue.id}
                                </span>
                                {issue.title}
                              </Link>
                              {!selectedProjectId && project && (
                                <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-600">
                                  {project.key}
                                </span>
                              )}
                              {issue.is_ambiguous_stack && (
                                <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-700">
                                  {t("issues.ambiguousStackBadge")}
                                </span>
                              )}
                              {issue.fixed_in_version && (
                                <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 font-mono text-[11px] text-emerald-700">
                                  {t("issues.fixedVersionBadge", {
                                    version: issue.fixed_in_version,
                                  })}
                                </span>
                              )}
                            </div>
                            {issue.root_cause && (
                              <p className="mt-0.5 line-clamp-1 text-xs text-slate-500">
                                {issue.root_cause}
                              </p>
                            )}
                          </td>
                          <td className="whitespace-nowrap px-4 py-3">
                            <span
                              className={`inline-flex whitespace-nowrap rounded-full border px-2.5 py-0.5 text-xs font-medium ${
                                STATUS_BADGE_CLASSES[issue.status]
                              }`}
                            >
                              {formatIssueStatus(issue.status)}
                            </span>
                          </td>
                          <td className="px-4 py-3 text-xs text-slate-600">
                            {issue.component || "—"}
                          </td>
                          <td className="whitespace-nowrap px-4 py-3 font-mono text-xs font-semibold text-slate-800">
                            {formatNumber(issue.crash_count)}
                          </td>
                          <td
                            className="whitespace-nowrap px-4 py-3 text-xs text-slate-600"
                            title={t("issues.firstSeenTitle", {
                              time: formatDateTime(issue.first_seen_at),
                            })}
                          >
                            {formatDateTime(issue.last_seen_at)}
                          </td>
                          <td className="whitespace-nowrap px-4 py-3 text-right">
                            <div className="inline-flex items-center justify-end gap-1.5">
                              <IssueTransitionSplitButton
                                currentStatus={issue.status}
                                issueId={issue.id}
                                size="sm"
                                isPending={
                                  transitionMutation.isPending &&
                                  transitioningIssueId === issue.id
                                }
                                onSelectAction={(action) =>
                                  handleRowTransitionAction(issue, action)
                                }
                              />
                              <button
                                type="button"
                                disabled={
                                  pinMutation.isPending &&
                                  pinningIssueId === issue.id
                                }
                                onClick={() =>
                                  pinMutation.mutate({
                                    id: issue.id,
                                    isPinned: !issue.is_pinned,
                                  })
                                }
                                title={
                                  issue.is_pinned
                                    ? t("issues.unpinTitle")
                                    : t("issues.pinTitle")
                                }
                                aria-label={
                                  issue.is_pinned
                                    ? t("issues.unpinAria", { id: issue.id })
                                    : t("issues.pinAria", { id: issue.id })
                                }
                                className={`inline-flex items-center gap-1 whitespace-nowrap rounded-md border px-2 py-1 text-xs font-medium transition-colors ${
                                  issue.is_pinned
                                    ? "border-amber-300 bg-amber-50 text-amber-800 hover:bg-amber-100"
                                    : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
                                }`}
                              >
                                <Pin className="h-3.5 w-3.5" />
                                {issue.is_pinned
                                  ? t("issues.pinned")
                                  : t("issues.pin")}
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

            {/* Pagination Footer */}
            <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 bg-slate-50 px-4 py-3 text-xs text-slate-600">
              <span>
                {t("common.showingRange", {
                  from: issuesFrom,
                  to: issuesTo,
                  total: formatNumber(issuesTotal),
                })}
              </span>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  disabled={issuesPage === 0}
                  onClick={() => setIssuesPage((p) => Math.max(0, p - 1))}
                  className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1 font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <ChevronLeft className="h-3.5 w-3.5" />
                  {t("common.previous")}
                </button>
                <button
                  type="button"
                  disabled={(issuesPage + 1) * PAGE_SIZE >= issuesTotal}
                  onClick={() => setIssuesPage((p) => p + 1)}
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

      {/* CLUSTERS TAB */}
      {activeTab === "clusters" && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white p-3.5 shadow-sm">
            <label className="inline-flex cursor-pointer items-center gap-2 text-sm text-slate-700">
              <input
                type="checkbox"
                checked={unassignedOnly}
                onChange={(e) => setUnassignedOnly(e.target.checked)}
                className="h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
              />
              {t("issues.unassignedOnly")}
            </label>
            <span className="text-xs text-slate-500">
              {t("issues.clustersHint")}
            </span>
          </div>

          {clustersQuery.isError && (
            <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
              <span>
                {clustersQuery.error instanceof Error
                  ? clustersQuery.error.message
                  : t("issues.loadClustersFailed")}
              </span>
              <button
                type="button"
                onClick={() => void clustersQuery.refetch()}
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
                      {t("issues.colSignatureHash")}
                    </th>
                    <th className="px-4 py-3">{t("issues.colException")}</th>
                    <th className="px-4 py-3">
                      {t("issues.colSignatureFrames")}
                    </th>
                    <th className="px-4 py-3">{t("issues.colCrashes")}</th>
                    <th className="px-4 py-3">
                      {t("issues.colAssignedIssue")}
                    </th>
                    <th className="px-4 py-3 text-right">
                      {t("issues.colExplore")}
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {clustersQuery.isPending ? (
                    <tr>
                      <td
                        colSpan={6}
                        className="px-4 py-10 text-center text-sm text-slate-500"
                      >
                        {t("issues.loadingClusters")}
                      </td>
                    </tr>
                  ) : clustersList.length === 0 ? (
                    <tr>
                      <td
                        colSpan={6}
                        className="px-4 py-12 text-center text-sm text-slate-500"
                      >
                        {t("issues.emptyClusters")}
                      </td>
                    </tr>
                  ) : (
                    clustersList.map((cluster: CrashClusterRead) => {
                      const project = projectById.get(cluster.project_id);
                      return (
                        <tr key={cluster.id} className="hover:bg-slate-50">
                          <td className="px-4 py-3">
                            <div className="flex items-center gap-1.5">
                              <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs text-slate-800">
                                {cluster.signature_hash.slice(0, 12)}
                              </code>
                              <button
                                type="button"
                                onClick={() =>
                                  handleCopyHash(cluster.signature_hash)
                                }
                                className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
                                title={t("issues.copyHashTitle")}
                              >
                                {copiedHash === cluster.signature_hash ? (
                                  <Check className="h-3.5 w-3.5 text-emerald-600" />
                                ) : (
                                  <Copy className="h-3.5 w-3.5" />
                                )}
                              </button>
                            </div>
                            {!selectedProjectId && project && (
                              <div className="mt-1 font-mono text-[11px] text-slate-500">
                                {project.key}
                              </div>
                            )}
                          </td>
                          <td className="px-4 py-3 font-mono text-xs text-slate-700">
                            {cluster.exception_type || "—"}
                          </td>
                          <td className="max-w-md px-4 py-3 font-mono text-xs text-slate-600">
                            <div className="truncate">
                              {formatSignatureFrames(
                                cluster.signature_frames_json,
                                cluster.signature_text,
                              )}
                            </div>
                          </td>
                          <td className="px-4 py-3 font-mono text-xs font-semibold text-slate-800">
                            {formatNumber(cluster.crash_count)}
                          </td>
                          <td className="px-4 py-3 text-xs">
                            {cluster.issue_id !== null ? (
                              <Link
                                to={`/issues/${cluster.issue_id}`}
                                className="font-medium text-indigo-600 hover:underline"
                              >
                                {t("issues.issueRef", { id: cluster.issue_id })}
                              </Link>
                            ) : (
                              <span className="inline-flex rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-700">
                                {t("issues.unassignedBadge")}
                              </span>
                            )}
                          </td>
                          <td className="px-4 py-3 text-right text-xs">
                            <Link
                              to={`/crashes?cluster_id=${encodeURIComponent(
                                cluster.id,
                              )}`}
                              className="font-medium text-indigo-600 hover:underline"
                            >
                              {t("issues.viewCrashes")}
                            </Link>
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
                  from: clustersFrom,
                  to: clustersTo,
                  total: formatNumber(clustersTotal),
                })}
              </span>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  disabled={clustersPage === 0}
                  onClick={() => setClustersPage((p) => Math.max(0, p - 1))}
                  className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1 font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <ChevronLeft className="h-3.5 w-3.5" />
                  {t("common.previous")}
                </button>
                <button
                  type="button"
                  disabled={(clustersPage + 1) * PAGE_SIZE >= clustersTotal}
                  onClick={() => setClustersPage((p) => p + 1)}
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

      {/* Merge Issues Modal */}
      {isMergeModalOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4"
          role="dialog"
          aria-modal="true"
          aria-labelledby="merge-modal-title"
        >
          <div className="w-full max-w-lg rounded-xl border border-slate-200 bg-white p-6 shadow-xl">
            <div className="flex items-center justify-between">
              <h2
                id="merge-modal-title"
                className="text-base font-semibold text-slate-900"
              >
                {t("issues.mergeModalTitle")}
              </h2>
              <button
                type="button"
                onClick={() => setIsMergeModalOpen(false)}
                className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <p className="mt-1 text-xs text-slate-500">
              {t("issues.mergeModalDesc")}
            </p>

            {mergeMutation.isError && (
              <div className="mt-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                {mergeMutation.error instanceof ApiError
                  ? mergeMutation.error.message
                  : t("issues.mergeFailed")}
              </div>
            )}

            <div className="mt-4 max-h-64 space-y-2 overflow-y-auto">
              {selectedIssues.map((issue) => (
                <label
                  key={issue.id}
                  className={`flex cursor-pointer items-start gap-3 rounded-lg border p-3 text-sm ${
                    mergeTargetId === issue.id
                      ? "border-indigo-500 bg-indigo-50/50"
                      : "border-slate-200 hover:bg-slate-50"
                  }`}
                >
                  <input
                    type="radio"
                    name="merge-target-issue"
                    checked={mergeTargetId === issue.id}
                    onChange={() => setMergeTargetId(issue.id)}
                    className="mt-0.5 text-indigo-600 focus:ring-indigo-500"
                  />
                  <div className="min-w-0 flex-1">
                    <div className="font-medium text-slate-900">
                      #{issue.id} — {issue.title}
                    </div>
                    <div className="mt-0.5 text-xs text-slate-500">
                      {t("issues.mergeItemMeta", {
                        status: formatIssueStatus(issue.status),
                        count: issue.crash_count,
                      })}
                    </div>
                  </div>
                </label>
              ))}
            </div>

            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setIsMergeModalOpen(false)}
                className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
              >
                {t("common.cancel")}
              </button>
              <button
                type="button"
                disabled={
                  mergeTargetId === null ||
                  hasCrossProjectSelection ||
                  mergeMutation.isPending
                }
                onClick={handleConfirmMerge}
                className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
              >
                <GitMerge className="h-4 w-4" />
                {mergeMutation.isPending
                  ? t("issues.merging")
                  : t("issues.confirmMerge")}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Create Issue Modal */}
      {isCreateModalOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4"
          role="dialog"
          aria-modal="true"
          aria-labelledby="create-issue-modal-title"
        >
          <div className="w-full max-w-xl rounded-xl border border-slate-200 bg-white p-6 shadow-xl">
            <div className="flex items-center justify-between">
              <h2
                id="create-issue-modal-title"
                className="text-base font-semibold text-slate-900"
              >
                {t("issues.createModalTitle")}
              </h2>
              <button
                type="button"
                onClick={() => setIsCreateModalOpen(false)}
                className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <form onSubmit={handleCreateSubmit} className="mt-4 space-y-4">
              {createMutation.isError && (
                <div className="flex items-center gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                  <AlertTriangle className="h-4 w-4 shrink-0" />
                  <span>
                    {createMutation.error instanceof ApiError
                      ? createMutation.error.message
                      : t("issues.createFailed")}
                  </span>
                </div>
              )}

              <div>
                <label className="block text-xs font-medium text-slate-700">
                  {t("issues.projectLabel")}
                </label>
                <select
                  required
                  disabled={Boolean(selectedProjectId)}
                  value={effectiveCreateProjectId}
                  onChange={(e) => setNewProjectId(e.target.value)}
                  className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900 disabled:bg-slate-100"
                >
                  {projects.length === 0 && (
                    <option value="">{t("issues.noProjectsAvailable")}</option>
                  )}
                  {projects.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name} ({p.key})
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-700">
                  {t("issues.titleLabel")}{" "}
                  <span className="text-rose-500">*</span>
                </label>
                <input
                  type="text"
                  required
                  value={newTitle}
                  onChange={(e) => setNewTitle(e.target.value)}
                  placeholder={t("issues.titlePlaceholder")}
                  className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 text-sm text-slate-900"
                />
              </div>

              <div className="grid gap-3 sm:grid-cols-3">
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    {t("issues.statusLabel")}
                  </label>
                  <select
                    value={newStatus}
                    onChange={(e) =>
                      setNewStatus(e.target.value as IssueStatus)
                    }
                    className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-900"
                  >
                    {ISSUE_STATUSES.map((st) => (
                      <option key={st} value={st}>
                        {formatIssueStatus(st)}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    {t("issues.componentLabel")}
                  </label>
                  <input
                    type="text"
                    value={newComponent}
                    onChange={(e) => setNewComponent(e.target.value)}
                    placeholder={t("issues.componentExamplePlaceholder")}
                    className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 text-sm text-slate-900"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    {t("issues.fixedInVersionLabel")}
                  </label>
                  <input
                    type="text"
                    value={newFixedInVersion}
                    onChange={(e) => setNewFixedInVersion(e.target.value)}
                    placeholder={t("issues.fixedInVersionPlaceholder")}
                    className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 font-mono text-sm text-slate-900"
                  />
                </div>
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-700">
                  {t("issues.rootCauseLabel")}
                </label>
                <textarea
                  rows={3}
                  value={newRootCause}
                  onChange={(e) => setNewRootCause(e.target.value)}
                  placeholder={t("issues.rootCausePlaceholder")}
                  className="mt-1 w-full rounded-md border border-slate-300 p-2.5 text-sm text-slate-900"
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-700">
                  {t("issues.logPatternsLabel")}
                </label>
                <textarea
                  rows={2}
                  value={newLogPatternsText}
                  onChange={(e) => setNewLogPatternsText(e.target.value)}
                  placeholder="FATAL:.*DeviceLost"
                  className="mt-1 w-full rounded-md border border-slate-300 p-2.5 font-mono text-xs text-slate-900"
                />
              </div>

              <div className="flex flex-wrap gap-4">
                <label className="inline-flex items-center gap-2 text-xs text-slate-700">
                  <input
                    type="checkbox"
                    checked={newIsAmbiguous}
                    onChange={(e) => setNewIsAmbiguous(e.target.checked)}
                    className="h-4 w-4 rounded border-slate-300 text-indigo-600"
                  />
                  {t("issues.ambiguousStackCheckbox")}
                </label>
                <label className="inline-flex items-center gap-2 text-xs text-slate-700">
                  <input
                    type="checkbox"
                    checked={newIsPinned}
                    onChange={(e) => setNewIsPinned(e.target.checked)}
                    className="h-4 w-4 rounded border-slate-300 text-indigo-600"
                  />
                  {t("issues.pinIssueCheckbox")}
                </label>
              </div>

              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setIsCreateModalOpen(false)}
                  className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
                >
                  {t("common.cancel")}
                </button>
                <button
                  type="submit"
                  disabled={
                    createMutation.isPending ||
                    !effectiveCreateProjectId ||
                    !newTitle.trim()
                  }
                  className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
                >
                  {createMutation.isPending
                    ? t("common.creating")
                    : t("issues.createIssueButton")}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      <IssueTransitionModal
        isOpen={transitionModal !== null}
        mode={transitionModal?.mode ?? "single"}
        issue={transitionModal?.mode === "single" ? transitionModal.issue : null}
        selectedIssues={selectedIssues}
        initialTargetStatus={transitionModal?.targetStatus ?? "KNOWN"}
        isPending={
          transitionModal?.mode === "batch"
            ? batchTransitionMutation.isPending
            : transitionMutation.isPending
        }
        errorMessage={
          transitionModal?.mode === "batch"
            ? batchTransitionMutation.isError
              ? batchTransitionMutation.error instanceof ApiError
                ? batchTransitionMutation.error.message
                : t("issues.transitionFailed")
              : null
            : transitionMutation.isError
              ? transitionMutation.error instanceof ApiError
                ? transitionMutation.error.message
                : t("issues.transitionFailed")
              : null
        }
        onClose={() => setTransitionModal(null)}
        onSubmit={handleTransitionModalSubmit}
      />
    </div>
  );
}

export default IssuesPage;
