import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  Bug,
  CheckCircle2,
  Download,
  Loader2,
  Pin,
  Plus,
  Radio,
  RefreshCw,
  RotateCcw,
  Sparkles,
  UserCheck,
  X,
} from "lucide-react";
import {
  ApiError,
  createCrashEventsSource,
  getCrash,
  getCrashArtifactUrl,
  listIssues,
  pinCrash,
  reprocessCrash,
  submitCrashVerdict,
} from "../api/client";
import type {
  CrashDetailRead,
  CrashStatus,
  CrashVerdictRequest,
  IssueStatus,
  ReprocessMode,
  VerdictAction,
} from "../api/types";
import { AgentTracePanel } from "../components/AgentTracePanel";
import { useProjectContext } from "../components/Layout";
import { StackTraceViewer } from "../components/StackTraceViewer";
import {
  VirtualLogViewer,
  type LogHighlightRange,
} from "../components/VirtualLogViewer";

const PIPELINE_STEPS: readonly CrashStatus[] = [
  "PENDING",
  "PARSING",
  "TRIAGING",
  "ANALYZED",
];

const ISSUE_STATUSES: readonly IssueStatus[] = [
  "NEW",
  "KNOWN",
  "FIXED",
  "WONTFIX",
  "REGRESSION",
];

function isCrashInFlight(status: CrashStatus | undefined): boolean {
  return status === "PENDING" || status === "PARSING" || status === "TRIAGING";
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

function getLatestAIResult(crash: CrashDetailRead | undefined) {
  if (!crash || !Array.isArray(crash.ai_runs) || crash.ai_runs.length === 0) {
    return null;
  }
  const sortedRuns = [...crash.ai_runs].sort((a, b) => {
    const ta = Date.parse(a.created_at);
    const tb = Date.parse(b.created_at);
    if (Number.isNaN(ta) || Number.isNaN(tb)) {
      return 0;
    }
    return tb - ta;
  });
  for (const run of sortedRuns) {
    if (run.result_json) {
      return run.result_json;
    }
  }
  return null;
}

export function CrashDetailPage() {
  const { crashId } = useParams<{ crashId: string }>();
  const { projects } = useProjectContext();
  const queryClient = useQueryClient();

  const [activeLogId, setActiveLogId] = useState<string>("");
  const [highlightRange, setHighlightRange] =
    useState<LogHighlightRange | null>(null);
  const [sseConnected, setSseConnected] = useState(false);
  const [sseStage, setSseStage] = useState<string | null>(null);
  const [sseError, setSseError] = useState<string | null>(null);

  // Human Verdict Modal State
  const [isVerdictModalOpen, setIsVerdictModalOpen] = useState(false);
  const [verdictAction, setVerdictAction] = useState<VerdictAction>("confirm");
  const [issueSearchInput, setIssueSearchInput] = useState("");
  const [debouncedIssueSearch, setDebouncedIssueSearch] = useState("");
  const [assignIssueId, setAssignIssueId] = useState<string>("");
  const [newIssueTitle, setNewIssueTitle] = useState("");
  const [newIssueComponent, setNewIssueComponent] = useState("");
  const [newIssueStatus, setNewIssueStatus] = useState<IssueStatus>("KNOWN");
  const [verdictRootCause, setVerdictRootCause] = useState("");
  const [verdictLogPatterns, setVerdictLogPatterns] = useState<string[]>([]);
  const [patternDraft, setPatternDraft] = useState("");
  const [verdictAmbiguousStack, setVerdictAmbiguousStack] = useState(false);

  const logViewerSectionRef = useRef<HTMLDivElement | null>(null);

  // Reset ephemeral workbench state on crashId route change
  useEffect(() => {
    setActiveLogId("");
    setHighlightRange(null);
    setIsVerdictModalOpen(false);
    setSseConnected(false);
    setSseStage(null);
    setSseError(null);
  }, [crashId]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedIssueSearch(issueSearchInput.trim());
    }, 250);
    return () => window.clearTimeout(timer);
  }, [issueSearchInput]);

  const crashQuery = useQuery({
    queryKey: ["crash", crashId],
    queryFn: () => getCrash(crashId!),
    enabled: Boolean(crashId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return isCrashInFlight(status) && !sseConnected ? 3000 : false;
    },
  });

  const crash = crashQuery.data;
  const inFlight = isCrashInFlight(crash?.status);

  // Initialize / validate activeLogId from crash.logs
  useEffect(() => {
    const logs = crash?.logs ?? [];
    if (logs.length === 0) {
      if (activeLogId !== "") {
        setActiveLogId("");
      }
      return;
    }
    if (!activeLogId || !logs.some((l) => l.id === activeLogId)) {
      setActiveLogId(logs[0].id);
    }
  }, [crash?.logs, activeLogId]);

  // Live SSE subscription while crash is PENDING / PARSING / TRIAGING
  useEffect(() => {
    if (!crashId || !inFlight) {
      setSseConnected(false);
      return;
    }

    const close = createCrashEventsSource(crashId, {
      onOpen: () => {
        setSseConnected(true);
        setSseError(null);
      },
      onStatus: (event) => {
        setSseStage(event.stage ?? null);
        setSseError(event.error ?? null);

        let statusChanged = false;
        queryClient.setQueryData<CrashDetailRead>(
          ["crash", crashId],
          (prev) => {
            if (!prev) {
              return prev;
            }
            if (prev.status !== event.status) {
              statusChanged = true;
            }
            return {
              ...prev,
              status: event.status,
              ai_verdict: event.ai_verdict ?? prev.ai_verdict,
            };
          },
        );

        const isTerminal =
          event.status === "ANALYZED" || event.status === "FAILED";
        if (statusChanged || isTerminal) {
          void queryClient.invalidateQueries({ queryKey: ["crash", crashId] });
        }
        if (isTerminal) {
          void queryClient.invalidateQueries({ queryKey: ["crashes"] });
          void queryClient.invalidateQueries({ queryKey: ["issues"] });
          void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
        }
      },
      onError: () => {
        setSseConnected(false);
      },
    });

    return () => {
      close();
      setSseConnected(false);
    };
  }, [crashId, inFlight, queryClient]);

  const issuesPickerQuery = useQuery({
    queryKey: [
      "issues",
      "picker",
      crash?.project_id ?? "",
      debouncedIssueSearch,
    ],
    queryFn: () =>
      listIssues({
        project_id: crash?.project_id,
        q: debouncedIssueSearch || undefined,
        limit: 50,
        offset: 0,
      }),
    enabled: Boolean(isVerdictModalOpen && crash?.project_id),
  });

  const reprocessMutation = useMutation({
    mutationFn: (mode: ReprocessMode) => reprocessCrash(crashId!, { mode }),
    onSuccess: (res) => {
      setSseError(null);
      setSseStage(null);
      queryClient.setQueryData<CrashDetailRead>(["crash", crashId], (prev) =>
        prev ? { ...prev, status: res.status } : prev,
      );
      void queryClient.invalidateQueries({ queryKey: ["crash", crashId] });
      void queryClient.invalidateQueries({ queryKey: ["crashes"] });
    },
  });

  const pinMutation = useMutation({
    mutationFn: (isPinned: boolean) => pinCrash(crashId!, isPinned),
    onSuccess: (res) => {
      queryClient.setQueryData<CrashDetailRead>(["crash", crashId], (prev) =>
        prev ? { ...prev, is_pinned: res.is_pinned } : prev,
      );
      void queryClient.invalidateQueries({ queryKey: ["crash", crashId] });
      void queryClient.invalidateQueries({ queryKey: ["crashes"] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  const verdictMutation = useMutation({
    mutationFn: (payload: CrashVerdictRequest) =>
      submitCrashVerdict(crashId!, payload),
    onSuccess: (updated) => {
      queryClient.setQueryData(["crash", crashId], updated);
      setIsVerdictModalOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["crash", crashId] });
      void queryClient.invalidateQueries({ queryKey: ["crashes"] });
      void queryClient.invalidateQueries({ queryKey: ["issues"] });
      void queryClient.invalidateQueries({ queryKey: ["issue"] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  const handleSelectLogEvidence = (
    logId: string,
    startLine: number,
    endLine: number,
  ) => {
    setActiveLogId(logId);
    setHighlightRange((prev) => ({
      logId,
      startLine,
      endLine,
      nonce: (prev?.nonce ?? 0) + 1,
    }));
    logViewerSectionRef.current?.scrollIntoView({
      behavior: "smooth",
      block: "nearest",
    });
  };

  const openVerdictModal = () => {
    if (!crash) {
      return;
    }
    verdictMutation.reset();
    const latestResult = getLatestAIResult(crash);
    const linkedIssueId = crash.issue_id ?? crash.cluster?.issue_id ?? null;
    const defaultAction: VerdictAction = linkedIssueId
      ? "confirm"
      : latestResult?.matched_issue_id != null
        ? "assign_issue"
        : "create_issue";

    setVerdictAction(defaultAction);
    setIssueSearchInput("");
    setAssignIssueId(
      linkedIssueId
        ? String(linkedIssueId)
        : latestResult?.matched_issue_id != null
          ? String(latestResult.matched_issue_id)
          : "",
    );
    setNewIssueTitle(
      latestResult?.suggested_title ||
        crash.crash_reason ||
        `Crash in ${crash.app_version}`,
    );
    setNewIssueComponent(latestResult?.suggested_component ?? "");
    setNewIssueStatus("KNOWN");
    setVerdictRootCause(
      crash.issue?.root_cause ?? latestResult?.root_cause_analysis ?? "",
    );
    setVerdictLogPatterns(
      latestResult?.key_log_patterns
        ? [...latestResult.key_log_patterns]
        : [],
    );
    setPatternDraft("");
    setVerdictAmbiguousStack(crash.issue?.is_ambiguous_stack ?? false);
    setIsVerdictModalOpen(true);
  };

  const handleAddVerdictPattern = (pat?: string) => {
    const trimmed = (pat ?? patternDraft).trim();
    if (!trimmed) {
      return;
    }
    if (!verdictLogPatterns.includes(trimmed)) {
      setVerdictLogPatterns((prev) => [...prev, trimmed]);
    }
    if (pat === undefined) {
      setPatternDraft("");
    }
  };

  const handleSubmitVerdict = (e: FormEvent) => {
    e.preventDefault();
    if (!crash) {
      return;
    }
    const payload: CrashVerdictRequest = {
      action: verdictAction,
      root_cause: verdictRootCause.trim() || undefined,
      add_log_patterns:
        verdictLogPatterns.length > 0 ? verdictLogPatterns : undefined,
      is_ambiguous_stack: verdictAmbiguousStack,
    };

    if (verdictAction === "confirm") {
      if (!crash.issue_id && !crash.cluster?.issue_id) {
        if (latestAIResult?.matched_issue_id == null) {
          return;
        }
        payload.action = "assign_issue";
        payload.issue_id = latestAIResult.matched_issue_id;
      }
    } else if (verdictAction === "assign_issue") {
      const targetId = Number(assignIssueId);
      if (!Number.isInteger(targetId) || targetId <= 0) {
        return;
      }
      payload.issue_id = targetId;
    } else if (verdictAction === "create_issue") {
      if (!newIssueTitle.trim()) {
        return;
      }
      payload.new_issue = {
        title: newIssueTitle.trim(),
        component: newIssueComponent.trim() || undefined,
        root_cause: verdictRootCause.trim() || undefined,
        status: newIssueStatus,
      };
    }

    verdictMutation.mutate(payload);
  };

  const project = useMemo(
    () => (crash ? projects.find((p) => p.id === crash.project_id) : undefined),
    [crash, projects],
  );

  const latestAIResult = useMemo(() => getLatestAIResult(crash), [crash]);

  if (!crashId) {
    return (
      <div className="rounded-xl border border-rose-200 bg-white p-6">
        <p className="text-sm text-rose-700">Missing crash ID in route.</p>
        <Link
          to="/crashes"
          className="mt-3 inline-flex items-center gap-1 text-sm font-medium text-indigo-600 hover:underline"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to Crashes
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
              to="/crashes"
              className="inline-flex items-center gap-1 font-medium text-indigo-600 hover:underline"
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              Crashes
            </Link>
            <span>/</span>
            <span className="font-mono text-slate-700">{crashId}</span>
          </div>

          <div className="flex flex-wrap items-center gap-2.5">
            <Bug className="h-5 w-5 text-indigo-600" />
            <h1 className="text-xl font-semibold text-slate-900">
              {crash?.crash_reason || "Crash Workbench"}
            </h1>
            {crash?.is_human_verified && (
              <span className="inline-flex items-center gap-1 rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-700">
                <CheckCircle2 className="h-3.5 w-3.5" />
                Human Verified
              </span>
            )}
          </div>

          {crash && (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
              <span>
                Project:{" "}
                <strong className="font-medium text-slate-700">
                  {project ? `${project.name} (${project.key})` : crash.project_id}
                </strong>
              </span>
              <span>|</span>
              <span className="font-mono">
                v{crash.app_version}
                {crash.build_id ? ` (${crash.build_id})` : ""}
              </span>
              {crash.os_version && (
                <>
                  <span>|</span>
                  <span>{crash.os_version}</span>
                </>
              )}
              <span>|</span>
              <span>
                Crash Time: {formatDateTime(crash.crash_time || crash.created_at)}
              </span>
              {crash.cluster && (
                <>
                  <span>|</span>
                  <Link
                    to={`/crashes?cluster_id=${encodeURIComponent(
                      crash.cluster.id,
                    )}`}
                    className="font-mono text-indigo-600 hover:underline"
                  >
                    Cluster {crash.cluster.signature_hash.slice(0, 10)} (
                    {crash.cluster.crash_count})
                  </Link>
                </>
              )}
              {crash.issue && (
                <>
                  <span>|</span>
                  <Link
                    to={`/issues/${crash.issue.id}`}
                    className="font-medium text-indigo-600 hover:underline"
                  >
                    Issue #{crash.issue.id}: {crash.issue.title}
                  </Link>
                </>
              )}
            </div>
          )}
        </div>

        {/* Action Controls */}
        {crash && (
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={reprocessMutation.isPending || inFlight}
              onClick={() => reprocessMutation.mutate("full")}
              className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 shadow-sm hover:bg-slate-50 disabled:opacity-50"
            >
              <RotateCcw className="h-3.5 w-3.5" />
              Reprocess Full
            </button>

            <button
              type="button"
              disabled={reprocessMutation.isPending || inFlight}
              onClick={() => reprocessMutation.mutate("triage")}
              className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 shadow-sm hover:bg-slate-50 disabled:opacity-50"
            >
              <Sparkles className="h-3.5 w-3.5 text-indigo-600" />
              Re-run AI
            </button>

            <button
              type="button"
              onClick={openVerdictModal}
              className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white shadow-sm hover:bg-indigo-700"
            >
              <UserCheck className="h-3.5 w-3.5" />
              Verify / Assign
            </button>

            <button
              type="button"
              disabled={pinMutation.isPending}
              onClick={() => pinMutation.mutate(!crash.is_pinned)}
              className={`inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium shadow-sm transition-colors ${
                crash.is_pinned
                  ? "border-amber-300 bg-amber-50 text-amber-800 hover:bg-amber-100"
                  : "border-slate-300 bg-white text-slate-700 hover:bg-slate-50"
              }`}
            >
              <Pin className="h-3.5 w-3.5" />
              {crash.is_pinned ? "Pinned" : "Pin"}
            </button>

            {Boolean(crash.dump_path) ? (
              <a
                href={getCrashArtifactUrl(crash.id, "dump")}
                download
                className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-700 shadow-sm hover:bg-slate-50"
              >
                <Download className="h-3.5 w-3.5" />
                .dmp
              </a>
            ) : (
              <span
                className="inline-flex cursor-not-allowed items-center gap-1 rounded-md border border-slate-200 bg-slate-100 px-2.5 py-1.5 text-xs text-slate-400"
                title="Dump artifact unavailable or evicted by GC"
              >
                <Download className="h-3.5 w-3.5" />
                .dmp
              </span>
            )}

            {crash.parsed_stacks_json ? (
              <a
                href={getCrashArtifactUrl(crash.id, "stacks_json")}
                download
                className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-700 shadow-sm hover:bg-slate-50"
              >
                <Download className="h-3.5 w-3.5" />
                stacks.json
              </a>
            ) : (
              <span
                className="inline-flex cursor-not-allowed items-center gap-1 rounded-md border border-slate-200 bg-slate-100 px-2.5 py-1.5 text-xs text-slate-400"
                title="Parsed stacks JSON not yet available"
              >
                <Download className="h-3.5 w-3.5" />
                stacks.json
              </span>
            )}
          </div>
        )}
      </div>

      {/* Loading / Error */}
      {crashQuery.isPending && (
        <div className="h-56 animate-pulse rounded-xl border border-slate-200 bg-white p-6" />
      )}

      {crashQuery.isError && (
        <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          <div className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 shrink-0 text-rose-600" />
            <span>
              {crashQuery.error instanceof ApiError
                ? crashQuery.error.message
                : "Failed to load crash details."}
            </span>
          </div>
          <button
            type="button"
            onClick={() => void crashQuery.refetch()}
            className="rounded border border-rose-300 bg-white px-2.5 py-1 text-xs font-medium text-rose-700"
          >
            Retry
          </button>
        </div>
      )}

      {reprocessMutation.isError && (
        <div className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-2.5 text-xs text-rose-700">
          {reprocessMutation.error instanceof ApiError
            ? reprocessMutation.error.message
            : "Failed to queue reprocessing."}
        </div>
      )}

      {pinMutation.isError && (
        <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-4 py-2.5 text-xs text-rose-700">
          <span>
            {pinMutation.error instanceof ApiError
              ? pinMutation.error.message
              : "Failed to update crash pin status."}
          </span>
          <button
            type="button"
            onClick={() => pinMutation.reset()}
            className="rounded p-0.5 text-rose-700 hover:bg-rose-100"
            aria-label="Dismiss pin error"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      {/* Live SSE Pipeline Banner */}
      {crash && (inFlight || crash.status === "FAILED" || sseError) && (
        <div
          className={`rounded-xl border px-4 py-3.5 shadow-sm ${
            crash.status === "FAILED" || sseError
              ? "border-rose-200 bg-rose-50 text-rose-900"
              : "border-indigo-200 bg-indigo-50 text-indigo-900"
          }`}
        >
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2.5">
              {inFlight ? (
                <Loader2 className="h-4 w-4 animate-spin text-indigo-600" />
              ) : (
                <AlertTriangle className="h-4 w-4 text-rose-600" />
              )}
              <span className="text-sm font-semibold">
                {crash.status === "FAILED"
                  ? "Crash processing failed"
                  : `Live Pipeline: ${crash.status}${
                      sseStage ? ` (${sseStage})` : ""
                    }`}
              </span>
            </div>

            <div className="flex items-center gap-3 text-xs">
              {inFlight && (
                <span className="inline-flex items-center gap-1 rounded-full bg-white/80 px-2.5 py-0.5 font-medium text-indigo-700">
                  <Radio className="h-3 w-3" />
                  {sseConnected ? "SSE Live" : "Polling (3s fallback)"}
                </span>
              )}
              {crash.status === "FAILED" && (
                <button
                  type="button"
                  onClick={() => reprocessMutation.mutate("full")}
                  className="inline-flex items-center gap-1 rounded-md bg-rose-600 px-2.5 py-1 font-medium text-white hover:bg-rose-700"
                >
                  <RefreshCw className="h-3 w-3" />
                  Retry Full Pipeline
                </button>
              )}
            </div>
          </div>

          {inFlight && (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              {PIPELINE_STEPS.map((step, idx) => {
                const currentIdx = PIPELINE_STEPS.indexOf(crash.status);
                const done = idx < currentIdx;
                const active = idx === currentIdx;
                return (
                  <div
                    key={step}
                    className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[11px] font-medium ${
                      active
                        ? "bg-indigo-600 text-white"
                        : done
                          ? "bg-emerald-100 text-emerald-800"
                          : "bg-white/70 text-slate-500"
                    }`}
                  >
                    <span>{idx + 1}.</span>
                    <span>{step}</span>
                  </div>
                );
              })}
            </div>
          )}

          {sseError && (
            <p className="mt-2 font-mono text-xs text-rose-800">{sseError}</p>
          )}
        </div>
      )}

      {/* Workbench Panels */}
      {crash && (
        <div className="space-y-6">
          <AgentTracePanel
            crash={crash}
            onSelectLogEvidence={handleSelectLogEvidence}
            onVerifyVerdict={openVerdictModal}
            onRerunAI={() => reprocessMutation.mutate("triage")}
            isRerunningAI={
              reprocessMutation.isPending || crash.status === "TRIAGING"
            }
          />

          <StackTraceViewer
            key={crash.id}
            parsedStacks={crash.parsed_stacks_json}
            missingSymbols={crash.missing_symbols_json}
            crashReason={crash.crash_reason}
            crashAddress={crash.crash_address}
            status={crash.status}
          />

          <div ref={logViewerSectionRef}>
            <VirtualLogViewer
              key={crash.id}
              logs={crash.logs ?? []}
              activeLogId={activeLogId}
              onSelectLog={setActiveLogId}
              crashTime={crash.crash_time}
              highlightRange={highlightRange}
            />
          </div>
        </div>
      )}

      {/* Human Verdict / Knowledge Base Feedback Modal */}
      {isVerdictModalOpen && crash && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4"
          role="dialog"
          aria-modal="true"
          aria-labelledby="verdict-modal-title"
        >
          <div className="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-xl border border-slate-200 bg-white p-6 shadow-xl">
            <div className="flex items-center justify-between">
              <div>
                <h2
                  id="verdict-modal-title"
                  className="text-base font-semibold text-slate-900"
                >
                  Human Verdict &amp; Knowledge Base Feedback
                </h2>
                <p className="mt-0.5 text-xs text-slate-500">
                  Verify AI triage, assign or create a Known Issue, and save
                  log patterns for future fuzzy recall.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setIsVerdictModalOpen(false)}
                className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <form onSubmit={handleSubmitVerdict} className="mt-4 space-y-4">
              {verdictMutation.isError && (
                <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                  {verdictMutation.error instanceof ApiError
                    ? verdictMutation.error.message
                    : "Failed to submit verdict."}
                </div>
              )}

              {/* Action Tabs */}
              <div className="grid gap-2 sm:grid-cols-3">
                <button
                  type="button"
                  disabled={
                    !crash.issue_id &&
                    !crash.cluster?.issue_id &&
                    latestAIResult?.matched_issue_id == null
                  }
                  onClick={() => setVerdictAction("confirm")}
                  className={`rounded-lg border p-2.5 text-left text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                    verdictAction === "confirm"
                      ? "border-indigo-500 bg-indigo-50 text-indigo-900"
                      : "border-slate-200 hover:bg-slate-50"
                  }`}
                >
                  <div className="font-semibold">Confirm Current</div>
                  <div className="mt-0.5 text-[11px] text-slate-500">
                    {crash.issue_id
                      ? `Confirm Issue #${crash.issue_id}`
                      : crash.cluster?.issue_id
                        ? `Confirm Cluster Issue #${crash.cluster.issue_id}`
                        : latestAIResult?.matched_issue_id != null
                          ? `Confirm AI #${latestAIResult.matched_issue_id}`
                          : "Requires linked issue"}
                  </div>
                </button>

                <button
                  type="button"
                  onClick={() => setVerdictAction("assign_issue")}
                  className={`rounded-lg border p-2.5 text-left text-xs transition-colors ${
                    verdictAction === "assign_issue"
                      ? "border-indigo-500 bg-indigo-50 text-indigo-900"
                      : "border-slate-200 hover:bg-slate-50"
                  }`}
                >
                  <div className="font-semibold">Assign Existing Issue</div>
                  <div className="mt-0.5 text-[11px] text-slate-500">
                    Link cluster to a known issue
                  </div>
                </button>

                <button
                  type="button"
                  onClick={() => setVerdictAction("create_issue")}
                  className={`rounded-lg border p-2.5 text-left text-xs transition-colors ${
                    verdictAction === "create_issue"
                      ? "border-indigo-500 bg-indigo-50 text-indigo-900"
                      : "border-slate-200 hover:bg-slate-50"
                  }`}
                >
                  <div className="font-semibold">Create New Issue</div>
                  <div className="mt-0.5 text-[11px] text-slate-500">
                    Add a new issue to KB
                  </div>
                </button>
              </div>

              {verdictAction === "assign_issue" && (
                <div className="space-y-2 rounded-lg border border-slate-200 bg-slate-50 p-3">
                  <label className="block text-xs font-medium text-slate-700">
                    Search &amp; Select Target Issue
                  </label>
                  <input
                    type="text"
                    value={issueSearchInput}
                    onChange={(e) => setIssueSearchInput(e.target.value)}
                    placeholder="Filter issues by title or root cause..."
                    className="h-8 w-full rounded-md border border-slate-300 bg-white px-2.5 text-xs text-slate-900"
                  />
                  <select
                    required
                    value={assignIssueId}
                    onChange={(e) => setAssignIssueId(e.target.value)}
                    className="h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-900"
                  >
                    <option value="">Select an issue...</option>
                    {(issuesPickerQuery.data?.items ?? []).map((iss) => (
                      <option key={iss.id} value={iss.id}>
                        #{iss.id} — {iss.title} [{iss.status}]
                      </option>
                    ))}
                  </select>
                </div>
              )}

              {verdictAction === "create_issue" && (
                <div className="space-y-3 rounded-lg border border-slate-200 bg-slate-50 p-3">
                  <div>
                    <label className="block text-xs font-medium text-slate-700">
                      New Issue Title <span className="text-rose-500">*</span>
                    </label>
                    <input
                      type="text"
                      required
                      value={newIssueTitle}
                      onChange={(e) => setNewIssueTitle(e.target.value)}
                      className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900"
                    />
                  </div>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <div>
                      <label className="block text-xs font-medium text-slate-700">
                        Component
                      </label>
                      <input
                        type="text"
                        value={newIssueComponent}
                        onChange={(e) => setNewIssueComponent(e.target.value)}
                        placeholder="e.g. Network"
                        className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900"
                      />
                    </div>
                    <div>
                      <label className="block text-xs font-medium text-slate-700">
                        Initial Status
                      </label>
                      <select
                        value={newIssueStatus}
                        onChange={(e) =>
                          setNewIssueStatus(e.target.value as IssueStatus)
                        }
                        className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-900"
                      >
                        {ISSUE_STATUSES.map((st) => (
                          <option key={st} value={st}>
                            {st}
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                </div>
              )}

              <div>
                <label className="block text-xs font-medium text-slate-700">
                  Root Cause Analysis
                </label>
                <textarea
                  rows={3}
                  value={verdictRootCause}
                  onChange={(e) => setVerdictRootCause(e.target.value)}
                  placeholder="Confirmed root cause to persist to the issue knowledge base..."
                  className="mt-1 w-full rounded-md border border-slate-300 p-2.5 text-sm text-slate-900"
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-700">
                  Add Log Patterns to Knowledge Base
                </label>
                <div className="mt-1 flex gap-2">
                  <input
                    type="text"
                    value={patternDraft}
                    onChange={(e) => setPatternDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        handleAddVerdictPattern();
                      }
                    }}
                    placeholder="Regex or literal log signature..."
                    className="h-9 flex-1 rounded-md border border-slate-300 px-3 font-mono text-xs text-slate-900"
                  />
                  <button
                    type="button"
                    onClick={() => handleAddVerdictPattern()}
                    className="inline-flex h-9 items-center gap-1 rounded-md border border-slate-300 bg-white px-3 text-xs font-medium text-slate-700 hover:bg-slate-50"
                  >
                    <Plus className="h-3.5 w-3.5" />
                    Add
                  </button>
                </div>

                {latestAIResult &&
                  latestAIResult.key_log_patterns.length > 0 && (
                    <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px]">
                      <span className="text-slate-500">AI suggestions:</span>
                      {latestAIResult.key_log_patterns.map((pat) => (
                        <button
                          key={pat}
                          type="button"
                          onClick={() => handleAddVerdictPattern(pat)}
                          className="rounded border border-indigo-200 bg-indigo-50 px-1.5 py-0.5 font-mono text-indigo-700 hover:bg-indigo-100"
                        >
                          + {pat}
                        </button>
                      ))}
                    </div>
                  )}

                {verdictLogPatterns.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {verdictLogPatterns.map((pat) => (
                      <span
                        key={pat}
                        className="inline-flex items-center gap-1 rounded border border-slate-200 bg-slate-100 px-2 py-0.5 font-mono text-xs text-slate-800"
                      >
                        {pat}
                        <button
                          type="button"
                          onClick={() =>
                            setVerdictLogPatterns((prev) =>
                              prev.filter((p) => p !== pat),
                            )
                          }
                          className="rounded p-0.5 text-slate-500 hover:bg-slate-200"
                        >
                          <X className="h-3 w-3" />
                        </button>
                      </span>
                    ))}
                  </div>
                )}
              </div>

              <label className="flex cursor-pointer items-center gap-2 text-xs text-slate-700">
                <input
                  type="checkbox"
                  checked={verdictAmbiguousStack}
                  onChange={(e) => setVerdictAmbiguousStack(e.target.checked)}
                  className="h-4 w-4 rounded border-slate-300 text-indigo-600"
                />
                Mark stack as ambiguous (always require AI log triage for this
                issue)
              </label>

              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setIsVerdictModalOpen(false)}
                  className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={
                    verdictMutation.isPending ||
                    (verdictAction === "assign_issue" && !assignIssueId) ||
                    (verdictAction === "create_issue" && !newIssueTitle.trim())
                  }
                  className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
                >
                  <UserCheck className="h-4 w-4" />
                  {verdictMutation.isPending ? "Saving..." : "Submit Verdict"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

export default CrashDetailPage;
