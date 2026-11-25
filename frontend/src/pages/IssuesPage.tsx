import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { Layers, Plus, X } from "lucide-react";
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
import type { IssueRead, IssueStatus } from "../api/types";
import {
  IssueTransitionModal,
  type IssueTransitionModalSubmitPayload,
  type TransitionActionConfig,
} from "../components/IssueStatusWorkflow";
import { useProjectContext } from "../components/Layout";
import { useAppI18n } from "../i18n";
import { ClustersTabPanel } from "./issues/ClustersTabPanel";
import { CreateIssueModal } from "./issues/CreateIssueModal";
import { IssuesTabPanel } from "./issues/IssuesTabPanel";
import { MergeIssuesModal } from "./issues/MergeIssuesModal";
import { PAGE_SIZE } from "./issues/utils";

export function IssuesPage() {
  const { t, formatIssueStatus } = useAppI18n();
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
  const [createResetNonce, setCreateResetNonce] = useState(0);

  // Feedback / transition state
  const [pinningIssueId, setPinningIssueId] = useState<number | null>(null);
  const [transitioningIssueId, setTransitioningIssueId] = useState<
    number | null
  >(null);
  const [transitionModal, setTransitionModal] = useState<
    | { mode: "single"; issue: IssueRead; targetStatus: IssueStatus }
    | { mode: "batch"; targetStatus: IssueStatus }
    | null
  >(null);
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
      setCreateResetNonce((n) => n + 1);
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

  const openCreateModal = () => {
    createMutation.reset();
    setIsCreateModalOpen(true);
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

  const scopeLabel = selectedProject
    ? `${selectedProject.name} (${selectedProject.key})`
    : selectedProjectId || t("nav.allProjects");

  const clustersList = clustersQuery.data?.items ?? [];
  const clustersTotal = clustersQuery.data?.total ?? 0;

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

      {/* Feedback Banners */}
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

      {activeTab === "issues" && (
        <IssuesTabPanel
          searchInput={searchInput}
          onSearchInputChange={setSearchInput}
          statusFilter={statusFilter}
          onStatusFilterChange={setStatusFilter}
          componentInput={componentInput}
          onComponentInputChange={setComponentInput}
          pinnedOnly={pinnedOnly}
          onTogglePinnedOnly={() => setPinnedOnly((prev) => !prev)}
          onResetFilters={() => {
            setSearchInput("");
            setStatusFilter("");
            setComponentInput("");
            setPinnedOnly(false);
          }}
          selectedProjectId={selectedProjectId}
          projectById={projectById}
          issuesList={issuesList}
          issuesTotal={issuesTotal}
          issuesPage={issuesPage}
          onPageChange={setIssuesPage}
          isPending={issuesQuery.isPending}
          isError={issuesQuery.isError}
          error={issuesQuery.error}
          onRetry={() => void issuesQuery.refetch()}
          selectedIssuesMap={selectedIssuesMap}
          selectedIssueIds={selectedIssueIds}
          hasCrossProjectSelection={hasCrossProjectSelection}
          allOnPageSelected={allOnPageSelected}
          onToggleSelectAllOnPage={toggleSelectAllOnPage}
          onToggleSelectIssue={toggleSelectIssue}
          onClearSelection={() => setSelectedIssuesMap(new Map())}
          onOpenBatchTransitionModal={openBatchTransitionModal}
          onOpenMergeModal={openMergeModal}
          isTransitionPending={transitionMutation.isPending}
          transitioningIssueId={transitioningIssueId}
          onRowTransitionAction={handleRowTransitionAction}
          isPinPending={pinMutation.isPending}
          pinningIssueId={pinningIssueId}
          onTogglePin={(issue) =>
            pinMutation.mutate({
              id: issue.id,
              isPinned: !issue.is_pinned,
            })
          }
        />
      )}

      {activeTab === "clusters" && (
        <ClustersTabPanel
          unassignedOnly={unassignedOnly}
          onUnassignedOnlyChange={setUnassignedOnly}
          selectedProjectId={selectedProjectId}
          projectById={projectById}
          clustersList={clustersList}
          clustersTotal={clustersTotal}
          clustersPage={clustersPage}
          onPageChange={setClustersPage}
          isPending={clustersQuery.isPending}
          isError={clustersQuery.isError}
          error={clustersQuery.error}
          onRetry={() => void clustersQuery.refetch()}
        />
      )}

      <MergeIssuesModal
        isOpen={isMergeModalOpen}
        selectedIssues={selectedIssues}
        mergeTargetId={mergeTargetId}
        hasCrossProjectSelection={hasCrossProjectSelection}
        isPending={mergeMutation.isPending}
        errorMessage={
          mergeMutation.isError
            ? mergeMutation.error instanceof ApiError
              ? mergeMutation.error.message
              : t("issues.mergeFailed")
            : null
        }
        onSelectTarget={setMergeTargetId}
        onClose={() => setIsMergeModalOpen(false)}
        onConfirm={handleConfirmMerge}
      />

      <CreateIssueModal
        isOpen={isCreateModalOpen}
        projects={projects}
        selectedProjectId={selectedProjectId}
        resetNonce={createResetNonce}
        isPending={createMutation.isPending}
        errorMessage={
          createMutation.isError
            ? createMutation.error instanceof ApiError
              ? createMutation.error.message
              : t("issues.createFailed")
            : null
        }
        onClose={() => setIsCreateModalOpen(false)}
        onSubmit={(payload) => createMutation.mutate(payload)}
      />

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
