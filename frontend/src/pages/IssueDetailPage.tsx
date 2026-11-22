import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  GitBranch,
  Layers,
  Pin,
  Plus,
  Save,
  Split,
  X,
} from "lucide-react";
import {
  ApiError,
  getIssue,
  listIssues,
  splitIssueCluster,
  updateIssue,
} from "../api/client";
import type {
  CrashClusterRead,
  CrashSummary,
  IssueDetailRead,
  IssueRead,
  IssueStatus,
  SignatureFrameItem,
} from "../api/types";
import { useProjectContext } from "../components/Layout";

const ISSUE_STATUSES: readonly IssueStatus[] = [
  "NEW",
  "KNOWN",
  "FIXED",
  "WONTFIX",
  "REGRESSION",
];

const STATUS_BADGE_CLASSES: Record<IssueStatus, string> = {
  NEW: "border-sky-200 bg-sky-50 text-sky-700",
  KNOWN: "border-indigo-200 bg-indigo-50 text-indigo-700",
  FIXED: "border-emerald-200 bg-emerald-50 text-emerald-700",
  WONTFIX: "border-slate-200 bg-slate-100 text-slate-700",
  REGRESSION: "border-rose-200 bg-rose-50 text-rose-700",
};

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

function formatSignatureFrames(
  frames: SignatureFrameItem[],
  fallbackText?: string | null,
): string {
  if (!Array.isArray(frames) || frames.length === 0) {
    return fallbackText || "—";
  }
  return frames
    .slice(0, 5)
    .map((f) =>
      typeof f === "string"
        ? f
        : `${f.module ? `${f.module}!` : ""}${f.function || "<unknown>"}`,
    )
    .join(" ← ");
}

function formatConfidence(confidence: number | null): string | null {
  if (confidence === null || !Number.isFinite(confidence)) {
    return null;
  }
  const pct = confidence <= 1 ? confidence * 100 : confidence;
  return `${Math.round(pct)}%`;
}

function isValidRegex(pattern: string): boolean {
  try {
    new RegExp(pattern);
    return true;
  } catch {
    return false;
  }
}

export function IssueDetailPage() {
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

  // Draft Knowledge Base Form State
  const [title, setTitle] = useState("");
  const [status, setStatus] = useState<IssueStatus>("NEW");
  const [component, setComponent] = useState("");
  const [fixedInVersion, setFixedInVersion] = useState("");
  const [rootCause, setRootCause] = useState("");
  const [isAmbiguousStack, setIsAmbiguousStack] = useState(false);
  const [logPatterns, setLogPatterns] = useState<string[]>([]);
  const [newPatternInput, setNewPatternInput] = useState("");
  const [saveBanner, setSaveBanner] = useState<string | null>(null);

  // Split / Reassign Cluster Modal State
  const [splitTargetCluster, setSplitTargetCluster] =
    useState<CrashClusterRead | null>(null);
  const [splitMode, setSplitMode] = useState<"new" | "existing">("new");
  const [splitNewTitle, setSplitNewTitle] = useState("");
  const [splitTargetIssueId, setSplitTargetIssueId] = useState<string>("");
  const [splitBanner, setSplitBanner] = useState<{
    targetIssueId: number;
    targetTitle: string;
  } | null>(null);

  const syncedIssueIdRef = useRef<number | null>(null);

  const syncFormFromIssue = (source: IssueDetailRead | IssueRead) => {
    setTitle(source.title);
    setStatus(source.status);
    setComponent(source.component ?? "");
    setFixedInVersion(source.fixed_in_version ?? "");
    setRootCause(source.root_cause ?? "");
    setIsAmbiguousStack(source.is_ambiguous_stack);
    setLogPatterns(
      Array.isArray(source.log_patterns_json) ? source.log_patterns_json : [],
    );
  };

  const projectIssuesQuery = useQuery({
    queryKey: ["issues", "split-targets", issue?.project_id ?? ""],
    queryFn: () =>
      listIssues({
        project_id: issue?.project_id,
        limit: 100,
        offset: 0,
      }),
    enabled: Boolean(splitTargetCluster && issue?.project_id),
  });

  const otherProjectIssues = useMemo(
    () =>
      (projectIssuesQuery.data?.items ?? []).filter(
        (item) => item.id !== numericIssueId,
      ),
    [projectIssuesQuery.data?.items, numericIssueId],
  );

  const projectLabel = useMemo(() => {
    if (!issue) {
      return null;
    }
    const found = projects.find((p) => p.id === issue.project_id);
    return found ? `${found.name} (${found.key})` : issue.project_id;
  }, [issue, projects]);

  const updateMutation = useMutation({
    mutationFn: (payload: Parameters<typeof updateIssue>[1]) =>
      updateIssue(numericIssueId, payload),
    onSuccess: (updated) => {
      syncedIssueIdRef.current = updated.id;
      syncFormFromIssue(updated);
      setSaveBanner(`Saved issue #${updated.id}.`);
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
    mutationFn: (payload: Parameters<typeof splitIssueCluster>[1]) =>
      splitIssueCluster(numericIssueId, payload),
    onSuccess: (res) => {
      setSplitTargetCluster(null);
      setSplitNewTitle("");
      setSplitTargetIssueId("");
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

  const updateResetRef = useRef(updateMutation.reset);
  updateResetRef.current = updateMutation.reset;
  const splitResetRef = useRef(splitMutation.reset);
  splitResetRef.current = splitMutation.reset;
  const pinResetRef = useRef(pinMutation.reset);
  pinResetRef.current = pinMutation.reset;

  useEffect(() => {
    syncedIssueIdRef.current = null;
    setSaveBanner(null);
    setSplitBanner(null);
    setNewPatternInput("");
    setSplitTargetCluster(null);
    setSplitNewTitle("");
    setSplitTargetIssueId("");
    updateResetRef.current();
    splitResetRef.current();
    pinResetRef.current();
  }, [numericIssueId]);

  useEffect(() => {
    if (!issue || issue.id !== numericIssueId) {
      return;
    }
    if (syncedIssueIdRef.current !== issue.id) {
      syncedIssueIdRef.current = issue.id;
      syncFormFromIssue(issue);
    }
  }, [issue, numericIssueId]);

  const handleAddPattern = () => {
    const trimmed = newPatternInput.trim();
    if (!trimmed) {
      return;
    }
    if (!logPatterns.includes(trimmed)) {
      setLogPatterns((prev) => [...prev, trimmed]);
    }
    setNewPatternInput("");
  };

  const handleRemovePattern = (pattern: string) => {
    setLogPatterns((prev) => prev.filter((item) => item !== pattern));
  };

  const handleSaveForm = (e: FormEvent) => {
    e.preventDefault();
    if (!title.trim()) {
      return;
    }
    setSaveBanner(null);
    updateMutation.mutate({
      title: title.trim(),
      status,
      component: component.trim() || null,
      fixed_in_version: fixedInVersion.trim() || null,
      root_cause: rootCause.trim() || null,
      is_ambiguous_stack: isAmbiguousStack,
      log_patterns_json: logPatterns,
    });
  };

  const openSplitModal = (cluster: CrashClusterRead) => {
    splitMutation.reset();
    setSplitTargetCluster(cluster);
    setSplitMode("new");
    setSplitNewTitle("");
    setSplitTargetIssueId("");
  };

  const handleSplitSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!splitTargetCluster) {
      return;
    }
    if (splitMode === "new") {
      if (!splitNewTitle.trim()) {
        return;
      }
      splitMutation.mutate({
        cluster_id: splitTargetCluster.id,
        new_issue_title: splitNewTitle.trim(),
      });
    } else {
      const parsedTarget = Number(splitTargetIssueId);
      if (!Number.isInteger(parsedTarget) || parsedTarget <= 0) {
        return;
      }
      splitMutation.mutate({
        cluster_id: splitTargetCluster.id,
        target_issue_id: parsedTarget,
      });
    }
  };

  if (!isValidId) {
    return (
      <div className="rounded-xl border border-rose-200 bg-white p-6 shadow-sm">
        <h1 className="text-base font-semibold text-slate-900">
          Invalid Issue ID
        </h1>
        <p className="mt-1 text-sm text-slate-600">
          The issue identifier <code className="font-mono">{issueId}</code> is
          not a valid positive integer.
        </p>
        <Link
          to="/issues"
          className="mt-4 inline-flex items-center gap-1.5 text-sm font-medium text-indigo-600 hover:underline"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to Issues
        </Link>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="space-y-1.5">
          <div className="flex items-center gap-2 text-xs text-slate-500">
            <Link
              to="/issues"
              className="inline-flex items-center gap-1 font-medium text-indigo-600 hover:underline"
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              Issues
            </Link>
            <span>/</span>
            <span className="font-mono text-slate-700">#{numericIssueId}</span>
          </div>

          <div className="flex flex-wrap items-center gap-2.5">
            <Layers className="h-5 w-5 text-indigo-600" />
            <h1 className="text-xl font-semibold text-slate-900">
              {issue ? issue.title : `Issue #${numericIssueId}`}
            </h1>
            {issue && (
              <span
                className={`inline-flex rounded-full border px-2.5 py-0.5 text-xs font-medium ${
                  STATUS_BADGE_CLASSES[issue.status]
                }`}
              >
                {issue.status}
              </span>
            )}
          </div>

          {issue && (
            <div className="flex flex-wrap items-center gap-3 text-xs text-slate-500">
              <span>Project: {projectLabel}</span>
              <span>|</span>
              <span className="font-mono font-medium text-slate-700">
                {issue.crash_count.toLocaleString()} crashes
              </span>
              <span>|</span>
              <span>First seen: {formatDateTime(issue.first_seen_at)}</span>
              <span>|</span>
              <span>Last seen: {formatDateTime(issue.last_seen_at)}</span>
            </div>
          )}
        </div>

        {issue && (
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={pinMutation.isPending}
              onClick={() => pinMutation.mutate(!issue.is_pinned)}
              className={`inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium shadow-sm transition-colors ${
                issue.is_pinned
                  ? "border-amber-300 bg-amber-50 text-amber-800 hover:bg-amber-100"
                  : "border-slate-300 bg-white text-slate-700 hover:bg-slate-50"
              }`}
            >
              <Pin className="h-3.5 w-3.5" />
              {issue.is_pinned ? "Pinned (GC Protected)" : "Pin Issue"}
            </button>
            <Link
              to={`/crashes?issue_id=${issue.id}`}
              className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 shadow-sm hover:bg-slate-50"
            >
              View All Crashes
            </Link>
          </div>
        )}
      </div>

      {/* Query Loading / Error */}
      {issueQuery.isPending && (
        <div className="h-48 animate-pulse rounded-xl border border-slate-200 bg-white p-6" />
      )}

      {issueQuery.isError && (
        <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          <span>
            {issueQuery.error instanceof ApiError
              ? issueQuery.error.message
              : "Failed to load issue details."}
          </span>
          <button
            type="button"
            onClick={() => void issueQuery.refetch()}
            className="rounded border border-rose-300 bg-white px-2.5 py-1 text-xs font-medium text-rose-700"
          >
            Retry
          </button>
        </div>
      )}

      {splitBanner && (
        <div className="flex items-center justify-between rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-2.5 text-sm text-emerald-800">
          <span>
            Cluster moved to{" "}
            <Link
              to={`/issues/${splitBanner.targetIssueId}`}
              className="font-semibold underline"
            >
              Issue #{splitBanner.targetIssueId} ({splitBanner.targetTitle})
            </Link>
            .
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

      {issue && (
        <div className="grid gap-6 lg:grid-cols-12">
          {/* Knowledge Base Editor */}
          <form
            onSubmit={handleSaveForm}
            className="space-y-4 rounded-xl border border-slate-200 bg-white p-5 shadow-sm lg:col-span-6"
          >
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-slate-900">
                Knowledge Base Metadata
              </h2>
              {saveBanner && (
                <span className="inline-flex items-center gap-1 text-xs font-medium text-emerald-700">
                  <CheckCircle2 className="h-3.5 w-3.5" />
                  {saveBanner}
                </span>
              )}
            </div>

            {updateMutation.isError && (
              <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                {updateMutation.error instanceof ApiError
                  ? updateMutation.error.message
                  : "Failed to save issue changes."}
              </div>
            )}

            <div>
              <label className="block text-xs font-medium text-slate-700">
                Issue Title
              </label>
              <input
                type="text"
                required
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 text-sm text-slate-900 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
              />
            </div>

            <div className="grid gap-3 sm:grid-cols-3">
              <div>
                <label className="block text-xs font-medium text-slate-700">
                  Status
                </label>
                <select
                  value={status}
                  onChange={(e) => setStatus(e.target.value as IssueStatus)}
                  className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-900 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
                >
                  {ISSUE_STATUSES.map((st) => (
                    <option key={st} value={st}>
                      {st}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-700">
                  Component
                </label>
                <input
                  type="text"
                  value={component}
                  onChange={(e) => setComponent(e.target.value)}
                  placeholder="e.g. AudioEngine"
                  className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 text-sm text-slate-900"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-700">
                  Fixed In Version
                </label>
                <input
                  type="text"
                  value={fixedInVersion}
                  onChange={(e) => setFixedInVersion(e.target.value)}
                  placeholder="e.g. 2.1.0"
                  className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 font-mono text-sm text-slate-900"
                />
              </div>
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-700">
                Root Cause Analysis
              </label>
              <textarea
                rows={4}
                value={rootCause}
                onChange={(e) => setRootCause(e.target.value)}
                placeholder="Document confirmed root cause, affected invariants, and resolution..."
                className="mt-1 w-full rounded-md border border-slate-300 p-2.5 text-sm text-slate-900 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
              />
            </div>

            <label className="flex cursor-pointer items-start gap-2.5 rounded-lg border border-slate-200 bg-slate-50 p-3">
              <input
                type="checkbox"
                checked={isAmbiguousStack}
                onChange={(e) => setIsAmbiguousStack(e.target.checked)}
                className="mt-0.5 h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
              />
              <div>
                <div className="text-xs font-semibold text-slate-800">
                  Ambiguous Stack Signature
                </div>
                <p className="text-[11px] text-slate-500">
                  When enabled, <code className="font-mono">SMART_TRIAGE</code>{" "}
                  always invokes AI log inspection for incoming crashes matching
                  this issue.
                </p>
              </div>
            </label>

            {/* Log Patterns Tag / Regex Editor */}
            <div>
              <label className="block text-xs font-medium text-slate-700">
                Key Log Patterns (<code className="font-mono">log_patterns_json</code>)
              </label>
              <div className="mt-1.5 flex gap-2">
                <input
                  type="text"
                  value={newPatternInput}
                  onChange={(e) => setNewPatternInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      handleAddPattern();
                    }
                  }}
                  placeholder="Add regex or substring pattern..."
                  className="h-9 flex-1 rounded-md border border-slate-300 px-3 font-mono text-xs text-slate-900"
                />
                <button
                  type="button"
                  onClick={handleAddPattern}
                  className="inline-flex h-9 items-center gap-1 rounded-md border border-slate-300 bg-white px-3 text-xs font-medium text-slate-700 hover:bg-slate-50"
                >
                  <Plus className="h-3.5 w-3.5" />
                  Add
                </button>
              </div>

              {logPatterns.length === 0 ? (
                <p className="mt-2 text-xs text-slate-400">
                  No log patterns configured yet.
                </p>
              ) : (
                <div className="mt-2.5 flex flex-wrap gap-2">
                  {logPatterns.map((pattern) => {
                    const regexOk = isValidRegex(pattern);
                    return (
                      <span
                        key={pattern}
                        className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 font-mono text-xs ${
                          regexOk
                            ? "border-slate-200 bg-slate-100 text-slate-800"
                            : "border-amber-300 bg-amber-50 text-amber-800"
                        }`}
                        title={
                          regexOk
                            ? "Valid regex pattern"
                            : "Invalid regex syntax (will be treated as literal substring if supported)"
                        }
                      >
                        {!regexOk && (
                          <AlertTriangle className="h-3 w-3 text-amber-600" />
                        )}
                        <span>{pattern}</span>
                        <button
                          type="button"
                          onClick={() => handleRemovePattern(pattern)}
                          className="rounded p-0.5 text-slate-500 hover:bg-slate-200 hover:text-slate-900"
                          aria-label={`Remove pattern ${pattern}`}
                        >
                          <X className="h-3 w-3" />
                        </button>
                      </span>
                    );
                  })}
                </div>
              )}
            </div>

            <div className="flex justify-end pt-2">
              <button
                type="submit"
                disabled={updateMutation.isPending || !title.trim()}
                className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-indigo-700 disabled:opacity-50"
              >
                <Save className="h-4 w-4" />
                {updateMutation.isPending ? "Saving..." : "Save Knowledge Base"}
              </button>
            </div>
          </form>

          {/* Right Column: Associated Clusters + Recent Crashes */}
          <div className="space-y-6 lg:col-span-6">
            {/* Associated Clusters */}
            <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
              <div className="mb-3 flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <GitBranch className="h-4 w-4 text-indigo-600" />
                  <h2 className="text-sm font-semibold text-slate-900">
                    Associated Clusters ({issue.clusters.length})
                  </h2>
                </div>
              </div>

              {issue.clusters.length === 0 ? (
                <p className="text-xs text-slate-500">
                  No crash clusters are currently assigned to this issue.
                </p>
              ) : (
                <div className="space-y-3">
                  {issue.clusters.map((cluster) => (
                    <div
                      key={cluster.id}
                      className="rounded-lg border border-slate-200 p-3"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="flex items-center gap-2">
                          <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs text-slate-800">
                            {cluster.signature_hash.slice(0, 12)}
                          </code>
                          {cluster.exception_type && (
                            <span className="font-mono text-xs text-slate-600">
                              {cluster.exception_type}
                            </span>
                          )}
                          <span className="rounded-full bg-indigo-50 px-2 py-0.5 font-mono text-[11px] font-medium text-indigo-700">
                            {cluster.crash_count} crashes
                          </span>
                        </div>

                        <div className="flex items-center gap-2">
                          <Link
                            to={`/crashes?cluster_id=${encodeURIComponent(
                              cluster.id,
                            )}`}
                            className="text-xs font-medium text-indigo-600 hover:underline"
                          >
                            Crashes
                          </Link>
                          <button
                            type="button"
                            onClick={() => openSplitModal(cluster)}
                            className="inline-flex items-center gap-1 rounded border border-slate-200 bg-white px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
                          >
                            <Split className="h-3 w-3" />
                            Split / Reassign
                          </button>
                        </div>
                      </div>
                      <div className="mt-2 truncate font-mono text-[11px] text-slate-600">
                        {formatSignatureFrames(
                          cluster.signature_frames_json,
                          cluster.signature_text,
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Recent Crashes */}
            <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
              <div className="mb-3 flex items-center justify-between">
                <h2 className="text-sm font-semibold text-slate-900">
                  Recent Crashes ({issue.recent_crashes.length})
                </h2>
                <Link
                  to={`/crashes?issue_id=${issue.id}`}
                  className="text-xs font-medium text-indigo-600 hover:underline"
                >
                  View all in Crashes &rarr;
                </Link>
              </div>

              {issue.recent_crashes.length === 0 ? (
                <p className="text-xs text-slate-500">
                  No recent crashes recorded for this issue.
                </p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="min-w-full divide-y divide-slate-200 text-left text-xs">
                    <thead>
                      <tr className="text-slate-500">
                        <th className="pb-2 pr-3 font-medium">Crash</th>
                        <th className="pb-2 pr-3 font-medium">Version</th>
                        <th className="pb-2 pr-3 font-medium">Status / AI</th>
                        <th className="pb-2 font-medium">Time</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {issue.recent_crashes.map((crash: CrashSummary) => {
                        const conf = formatConfidence(crash.ai_confidence);
                        return (
                          <tr key={crash.id} className="hover:bg-slate-50">
                            <td className="py-2 pr-3">
                              <Link
                                to={`/crashes/${crash.id}`}
                                className="font-mono font-medium text-indigo-600 hover:underline"
                              >
                                {crash.id.slice(0, 8)}
                              </Link>
                              {crash.crash_reason && (
                                <div className="max-w-[180px] truncate text-[11px] text-slate-500">
                                  {crash.crash_reason}
                                </div>
                              )}
                            </td>
                            <td className="py-2 pr-3 font-mono text-slate-700">
                              {crash.app_version}
                            </td>
                            <td className="py-2 pr-3">
                              <div className="flex flex-wrap items-center gap-1">
                                <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] font-medium text-slate-700">
                                  {crash.status}
                                </span>
                                {crash.ai_verdict && (
                                  <span className="rounded bg-indigo-50 px-1.5 py-0.5 text-[11px] text-indigo-700">
                                    {crash.ai_verdict}
                                    {conf ? ` (${conf})` : ""}
                                  </span>
                                )}
                                {crash.is_human_verified && (
                                  <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-[11px] text-emerald-700">
                                    Verified
                                  </span>
                                )}
                              </div>
                            </td>
                            <td className="py-2 text-slate-500">
                              {formatDateTime(
                                crash.crash_time || crash.created_at,
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Split / Reassign Modal */}
      {splitTargetCluster && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4"
          role="dialog"
          aria-modal="true"
          aria-labelledby="split-cluster-modal-title"
        >
          <div className="w-full max-w-lg rounded-xl border border-slate-200 bg-white p-6 shadow-xl">
            <div className="flex items-center justify-between">
              <h2
                id="split-cluster-modal-title"
                className="text-base font-semibold text-slate-900"
              >
                Split / Reassign Cluster
              </h2>
              <button
                type="button"
                onClick={() => setSplitTargetCluster(null)}
                className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <p className="mt-1 text-xs text-slate-500">
              Move cluster{" "}
              <code className="font-mono">
                {splitTargetCluster.signature_hash.slice(0, 12)}
              </code>{" "}
              ({splitTargetCluster.crash_count} crashes) to a new or existing
              issue.
            </p>

            <form onSubmit={handleSplitSubmit} className="mt-4 space-y-4">
              {splitMutation.isError && (
                <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                  {splitMutation.error instanceof ApiError
                    ? splitMutation.error.message
                    : "Failed to split cluster."}
                </div>
              )}

              <div className="space-y-2">
                <label className="flex cursor-pointer items-center gap-2 text-sm font-medium text-slate-800">
                  <input
                    type="radio"
                    name="split-mode"
                    checked={splitMode === "new"}
                    onChange={() => setSplitMode("new")}
                  />
                  Create New Issue
                </label>
                {splitMode === "new" && (
                  <input
                    type="text"
                    required
                    value={splitNewTitle}
                    onChange={(e) => setSplitNewTitle(e.target.value)}
                    placeholder="New issue title..."
                    className="h-9 w-full rounded-md border border-slate-300 px-3 text-sm text-slate-900"
                  />
                )}
              </div>

              <div className="space-y-2">
                <label className="flex cursor-pointer items-center gap-2 text-sm font-medium text-slate-800">
                  <input
                    type="radio"
                    name="split-mode"
                    checked={splitMode === "existing"}
                    onChange={() => setSplitMode("existing")}
                  />
                  Reassign to Existing Issue
                </label>
                {splitMode === "existing" && (
                  <select
                    required
                    value={splitTargetIssueId}
                    onChange={(e) => setSplitTargetIssueId(e.target.value)}
                    className="h-9 w-full rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900"
                  >
                    <option value="">Select target issue...</option>
                    {otherProjectIssues.map((cand) => (
                      <option key={cand.id} value={cand.id}>
                        #{cand.id} — {cand.title} ({cand.status})
                      </option>
                    ))}
                  </select>
                )}
              </div>

              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setSplitTargetCluster(null)}
                  className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={
                    splitMutation.isPending ||
                    (splitMode === "new"
                      ? !splitNewTitle.trim()
                      : !splitTargetIssueId)
                  }
                  className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
                >
                  <Split className="h-4 w-4" />
                  {splitMutation.isPending ? "Moving..." : "Move Cluster"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

export default IssueDetailPage;
