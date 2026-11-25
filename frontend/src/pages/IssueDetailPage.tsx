import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, X } from "lucide-react";
import {
  ApiError,
  getIssue,
  splitIssueCluster,
  transitionIssue,
  updateIssue,
} from "../api/client";
import type {
  CrashClusterRead,
  IssueSplitRequest,
  IssueStatus,
  IssueUpdateRequest,
} from "../api/types";
import {
  IssueStatusHistoryCard,
  IssueTransitionModal,
  type IssueTransitionModalSubmitPayload,
  type TransitionActionConfig,
} from "../components/IssueStatusWorkflow";
import { useProjectContext } from "../components/Layout";
import { useAppI18n } from "../i18n";
import { AssociatedClustersCard } from "./issueDetail/AssociatedClustersCard";
import { IssueDetailHeader } from "./issueDetail/IssueDetailHeader";
import {
  IssueKnowledgeBaseForm,
  type SavedIssueSync,
  type TransitionFieldSync,
} from "./issueDetail/IssueKnowledgeBaseForm";
import { RecentCrashesCard } from "./issueDetail/RecentCrashesCard";
import { SplitClusterModal } from "./issueDetail/SplitClusterModal";

export function IssueDetailPage() {
  const { t, formatIssueStatus } = useAppI18n();
  const { issueId } = useParams<{ issueId: string }>();
  const { projects } = useProjectContext();
  const queryClient = useQueryClient();

  const numericIssueId = Number(issueId);
  const isValidId =
    Boolean(issueId) &&
    Number.isInteger(numericIssueId) &&
    numericIssueId > 0;

  const issueQuery = useQuery({
    queryKey: ["issue", numericIssueId],
    queryFn: () => getIssue(numericIssueId),
    enabled: isValidId,
  });

  const issue = issueQuery.data;

  // Form sync & feedback banners
  const [saveBanner, setSaveBanner] = useState<string | null>(null);
  const [savedIssueSync, setSavedIssueSync] = useState<SavedIssueSync | null>(
    null,
  );
  const [transitionSync, setTransitionSync] =
    useState<TransitionFieldSync | null>(null);

  // Split / Reassign Cluster Modal State
  const [splitTargetCluster, setSplitTargetCluster] =
    useState<CrashClusterRead | null>(null);
  const [splitBanner, setSplitBanner] = useState<{
    targetIssueId: number;
    targetTitle: string;
  } | null>(null);

  // Status workflow transition state
  const [transitionModalTarget, setTransitionModalTarget] =
    useState<IssueStatus | null>(null);
  const [transitionBanner, setTransitionBanner] = useState<string | null>(null);
  const [transitionError, setTransitionError] = useState<string | null>(null);

  const projectLabel = useMemo(() => {
    if (!issue) {
      return null;
    }
    const found = projects.find((p) => p.id === issue.project_id);
    return found ? `${found.name} (${found.key})` : issue.project_id;
  }, [issue, projects]);

  const updateMutation = useMutation({
    mutationFn: (payload: IssueUpdateRequest) =>
      updateIssue(numericIssueId, payload),
    onSuccess: (updated) => {
      setSavedIssueSync((prev) => ({
        issue: updated,
        version: (prev?.version ?? 0) + 1,
      }));
      setSaveBanner(t("issueDetail.savedBanner", { id: updated.id }));
      void queryClient.invalidateQueries({ queryKey: ["issue", numericIssueId] });
      void queryClient.invalidateQueries({ queryKey: ["issues"] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  const pinMutation = useMutation({
    mutationFn: (isPinned: boolean) =>
      updateIssue(numericIssueId, { is_pinned: isPinned }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["issue", numericIssueId] });
      void queryClient.invalidateQueries({ queryKey: ["issues"] });
    },
  });

  const splitMutation = useMutation({
    mutationFn: (payload: IssueSplitRequest) =>
      splitIssueCluster(numericIssueId, payload),
    onSuccess: (res) => {
      setSplitTargetCluster(null);
      setSplitBanner({
        targetIssueId: res.target_issue.id,
        targetTitle: res.target_issue.title,
      });
      void queryClient.invalidateQueries({ queryKey: ["issue", numericIssueId] });
      void queryClient.invalidateQueries({ queryKey: ["issues"] });
      void queryClient.invalidateQueries({ queryKey: ["clusters"] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  const transitionMutation = useMutation({
    mutationFn: (payload: IssueTransitionModalSubmitPayload) =>
      transitionIssue(numericIssueId, payload),
    onMutate: () => {
      setTransitionError(null);
    },
    onSuccess: (res) => {
      setTransitionModalTarget(null);
      setTransitionSync((prev) => ({
        status: res.issue.status,
        fixedInVersion: res.issue.fixed_in_version ?? "",
        version: (prev?.version ?? 0) + 1,
      }));
      setTransitionBanner(
        t("issueWorkflow.transitionSuccess", {
          id: res.issue.id,
          to: formatIssueStatus(res.issue.status),
        }),
      );
      void queryClient.invalidateQueries({ queryKey: ["issue", numericIssueId] });
      void queryClient.invalidateQueries({ queryKey: ["issues"] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
    onError: (err) => {
      if (transitionModalTarget === null) {
        setTransitionError(
          err instanceof ApiError
            ? err.message
            : t("issueWorkflow.transitionFailed"),
        );
      }
    },
  });

  const updateResetRef = useRef(updateMutation.reset);
  updateResetRef.current = updateMutation.reset;
  const splitResetRef = useRef(splitMutation.reset);
  splitResetRef.current = splitMutation.reset;
  const pinResetRef = useRef(pinMutation.reset);
  pinResetRef.current = pinMutation.reset;
  const transitionResetRef = useRef(transitionMutation.reset);
  transitionResetRef.current = transitionMutation.reset;

  useEffect(() => {
    setSaveBanner(null);
    setSavedIssueSync(null);
    setTransitionSync(null);
    setSplitBanner(null);
    setTransitionBanner(null);
    setTransitionError(null);
    setTransitionModalTarget(null);
    setSplitTargetCluster(null);
    updateResetRef.current();
    splitResetRef.current();
    pinResetRef.current();
    transitionResetRef.current();
  }, [numericIssueId]);

  const handleSaveForm = (payload: IssueUpdateRequest) => {
    setSaveBanner(null);
    updateMutation.mutate(payload);
  };

  const handleDetailTransitionAction = (action: TransitionActionConfig) => {
    setTransitionError(null);
    setTransitionBanner(null);
    if (!action.requiresModal) {
      transitionMutation.mutate({ to_status: action.toStatus });
      return;
    }
    transitionMutation.reset();
    setTransitionModalTarget(action.toStatus);
  };

  const openSplitModal = (cluster: CrashClusterRead) => {
    splitMutation.reset();
    setSplitTargetCluster(cluster);
  };

  if (!isValidId) {
    return (
      <div className="rounded-xl border border-rose-200 bg-white p-6 shadow-sm">
        <h1 className="text-base font-semibold text-slate-900">
          {t("issueDetail.invalidIdTitle")}
        </h1>
        <p className="mt-1 text-sm text-slate-600">
          {t("issueDetail.invalidIdDescBefore")}
          <code className="font-mono">{issueId}</code>
          {t("issueDetail.invalidIdDescAfter")}
        </p>
        <Link
          to="/issues"
          className="mt-4 inline-flex items-center gap-1.5 text-sm font-medium text-indigo-600 hover:underline"
        >
          <ArrowLeft className="h-4 w-4" />
          {t("issueDetail.backToIssues")}
        </Link>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <IssueDetailHeader
        numericIssueId={numericIssueId}
        issue={issue}
        projectLabel={projectLabel}
        isTransitionPending={transitionMutation.isPending}
        isPinPending={pinMutation.isPending}
        onTransitionAction={handleDetailTransitionAction}
        onTogglePin={(nextPinned) => pinMutation.mutate(nextPinned)}
      />

      {/* Query Loading / Error */}
      {issueQuery.isPending && (
        <div className="h-48 animate-pulse rounded-xl border border-slate-200 bg-white p-6" />
      )}

      {issueQuery.isError && (
        <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          <span>
            {issueQuery.error instanceof ApiError
              ? issueQuery.error.message
              : t("issueDetail.loadFailed")}
          </span>
          <button
            type="button"
            onClick={() => void issueQuery.refetch()}
            className="rounded border border-rose-300 bg-white px-2.5 py-1 text-xs font-medium text-rose-700"
          >
            {t("common.retry")}
          </button>
        </div>
      )}

      {splitBanner && (
        <div className="flex items-center justify-between rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-2.5 text-sm text-emerald-800">
          <span>
            {t("issueDetail.clusterMovedPrefix")}
            <Link
              to={`/issues/${splitBanner.targetIssueId}`}
              className="font-semibold underline"
            >
              {t("issueDetail.clusterMovedIssue", {
                id: splitBanner.targetIssueId,
                title: splitBanner.targetTitle,
              })}
            </Link>
            {t("issueDetail.clusterMovedSuffix")}
          </span>
          <button
            type="button"
            onClick={() => setSplitBanner(null)}
            className="rounded p-1 hover:bg-emerald-100"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {transitionBanner && (
        <div className="flex items-center justify-between rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-2.5 text-sm text-emerald-800">
          <span>{transitionBanner}</span>
          <button
            type="button"
            onClick={() => setTransitionBanner(null)}
            className="rounded p-1 hover:bg-emerald-100"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {transitionError && (
        <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-4 py-2.5 text-sm text-rose-800">
          <span>{transitionError}</span>
          <button
            type="button"
            onClick={() => setTransitionError(null)}
            className="rounded p-1 hover:bg-rose-100"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {issue && (
        <div className="grid gap-6 lg:grid-cols-12">
          {/* Left Column: Knowledge Base Editor + Status History */}
          <div className="space-y-6 lg:col-span-6">
            <IssueKnowledgeBaseForm
              key={issue.id}
              issue={issue}
              savedIssueSync={savedIssueSync}
              transitionSync={transitionSync}
              saveBanner={saveBanner}
              isPending={updateMutation.isPending}
              errorMessage={
                updateMutation.isError
                  ? updateMutation.error instanceof ApiError
                    ? updateMutation.error.message
                    : t("issueDetail.saveFailed")
                  : null
              }
              onSubmit={handleSaveForm}
            />

            <IssueStatusHistoryCard events={issue.status_events ?? []} />
            <IssueTransitionModal
              isOpen={transitionModalTarget !== null}
              mode="single"
              issue={issue}
              initialTargetStatus={transitionModalTarget ?? "KNOWN"}
              isPending={transitionMutation.isPending}
              errorMessage={
                transitionMutation.isError
                  ? transitionMutation.error instanceof ApiError
                    ? transitionMutation.error.message
                    : t("issueWorkflow.transitionFailed")
                  : null
              }
              onClose={() => setTransitionModalTarget(null)}
              onSubmit={(payload) => transitionMutation.mutate(payload)}
            />
          </div>

          {/* Right Column: Associated Clusters + Recent Crashes */}
          <div className="space-y-6 lg:col-span-6">
            <AssociatedClustersCard
              clusters={issue.clusters}
              onOpenSplitModal={openSplitModal}
            />
            <RecentCrashesCard
              issueId={issue.id}
              recentCrashes={issue.recent_crashes}
            />
          </div>
        </div>
      )}

      {splitTargetCluster && issue && (
        <SplitClusterModal
          key={splitTargetCluster.id}
          cluster={splitTargetCluster}
          projectId={issue.project_id}
          currentIssueId={numericIssueId}
          isPending={splitMutation.isPending}
          errorMessage={
            splitMutation.isError
              ? splitMutation.error instanceof ApiError
                ? splitMutation.error.message
                : t("issueDetail.splitFailed")
              : null
          }
          onClose={() => setSplitTargetCluster(null)}
          onSubmit={(payload) => splitMutation.mutate(payload)}
        />
      )}
    </div>
  );
}

export default IssueDetailPage;
