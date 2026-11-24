import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  Bot,
  Brain,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Copy,
  ExternalLink,
  FileText,
  HelpCircle,
  Loader2,
  Play,
  RotateCcw,
  Sparkles,
  Terminal,
  UserCheck,
  Wrench,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import type {
  AIAnalysisRunRead,
  AITraceStep,
  AIVerdict,
  CrashDetailRead,
} from "../api/types";
import { useAppI18n } from "../i18n";

export interface AgentTracePanelProps {
  crash: CrashDetailRead;
  onSelectLogEvidence: (
    logId: string,
    startLine: number,
    endLine: number,
  ) => void;
  onVerifyVerdict?: () => void;
  onRerunAI?: () => void;
  isRerunningAI?: boolean;
}

const VERDICT_META: Record<
  AIVerdict,
  { badgeClass: string; icon: LucideIcon }
> = {
  KNOWN_ISSUE: {
    badgeClass: "border-emerald-200 bg-emerald-50 text-emerald-700",
    icon: CheckCircle2,
  },
  NEW_ISSUE: {
    badgeClass: "border-sky-200 bg-sky-50 text-sky-700",
    icon: Sparkles,
  },
  REGRESSION: {
    badgeClass: "border-rose-200 bg-rose-50 text-rose-700",
    icon: AlertTriangle,
  },
  NEEDS_REVIEW: {
    badgeClass: "border-amber-200 bg-amber-50 text-amber-800",
    icon: HelpCircle,
  },
};

function sortRunsNewestFirst(runs: AIAnalysisRunRead[]): AIAnalysisRunRead[] {
  return [...runs].sort((a, b) => {
    const ta = Date.parse(a.created_at);
    const tb = Date.parse(b.created_at);
    if (Number.isNaN(ta) || Number.isNaN(tb)) {
      return 0;
    }
    return tb - ta;
  });
}

function normalizeTraceSteps(rawTrace: unknown): {
  steps: AITraceStep[];
  malformed: boolean;
} {
  if (rawTrace === null || rawTrace === undefined) {
    return { steps: [], malformed: false };
  }

  let parsed: unknown = rawTrace;
  if (typeof rawTrace === "string") {
    if (!rawTrace.trim()) {
      return { steps: [], malformed: false };
    }
    try {
      parsed = JSON.parse(rawTrace) as unknown;
    } catch {
      return { steps: [], malformed: true };
    }
  }

  if (!Array.isArray(parsed)) {
    return { steps: [], malformed: true };
  }

  const steps: AITraceStep[] = [];
  let malformed = false;
  for (let i = 0; i < parsed.length; i += 1) {
    const item = parsed[i];
    if (typeof item === "object" && item !== null && !Array.isArray(item)) {
      const obj = item as Record<string, unknown>;
      steps.push({
        step: typeof obj.step === "number" ? obj.step : i + 1,
        role: typeof obj.role === "string" ? obj.role : undefined,
        tool_name:
          typeof obj.tool_name === "string" ? obj.tool_name : null,
        arguments:
          typeof obj.arguments === "string" ||
          (typeof obj.arguments === "object" && obj.arguments !== null)
            ? (obj.arguments as Record<string, unknown> | string)
            : null,
        output_preview:
          typeof obj.output_preview === "string" ? obj.output_preview : null,
        error: typeof obj.error === "string" ? obj.error : null,
        duration_ms:
          typeof obj.duration_ms === "number" ? obj.duration_ms : null,
        message: typeof obj.message === "string" ? obj.message : null,
      });
    } else {
      malformed = true;
    }
  }
  return { steps, malformed };
}

function formatTraceValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (
      (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
      (trimmed.startsWith("[") && trimmed.endsWith("]"))
    ) {
      try {
        return JSON.stringify(JSON.parse(trimmed), null, 2);
      } catch {
        return value;
      }
    }
    return value;
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) {
    return "-";
  }
  if (ms < 1000) {
    return `${Math.round(ms)}ms`;
  }
  return `${(ms / 1000).toFixed(2)}s`;
}

function normalizeConfidence(confidence: number | null | undefined): number | null {
  if (confidence === null || confidence === undefined || !Number.isFinite(confidence)) {
    return null;
  }
  const pct = confidence <= 1 ? Math.round(confidence * 100) : Math.round(confidence);
  return Math.max(0, Math.min(100, pct));
}

function formatSafeLineRange(start: number, end: number): [number, number] {
  const s = Math.max(1, Math.floor(Number.isFinite(start) ? start : 1));
  const e = Math.max(s, Math.floor(Number.isFinite(end) ? end : s));
  return [s, e];
}

export function AgentTracePanel({
  crash,
  onSelectLogEvidence,
  onVerifyVerdict,
  onRerunAI,
  isRerunningAI = false,
}: AgentTracePanelProps) {
  const { t, formatDateTime, formatVerdict, formatAIRunStatus } = useAppI18n();
  const sortedRuns = useMemo(
    () => sortRunsNewestFirst(crash.ai_runs ?? []),
    [crash.ai_runs],
  );
  const newestRunId = sortedRuns[0]?.id ?? null;

  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const prevCrashIdRef = useRef<string>(crash.id);
  const prevNewestRunIdRef = useRef<string | null>(newestRunId);
  const [traceExpanded, setTraceExpanded] = useState(false);
  const [expandedStepKeys, setExpandedStepKeys] = useState<
    Record<string, boolean>
  >({});
  const [copiedPattern, setCopiedPattern] = useState<string | null>(null);

  // Auto-follow newest run whenever the crash changes or a newer run arrives
  useEffect(() => {
    const crashChanged = prevCrashIdRef.current !== crash.id;
    const newestRunChanged = prevNewestRunIdRef.current !== newestRunId;
    if (crashChanged || newestRunChanged) {
      prevCrashIdRef.current = crash.id;
      prevNewestRunIdRef.current = newestRunId;
      setSelectedRunId(null);
    }
  }, [crash.id, newestRunId]);

  const activeRun = useMemo(() => {
    if (sortedRuns.length === 0) {
      return null;
    }
    if (selectedRunId) {
      const matched = sortedRuns.find((r) => r.id === selectedRunId);
      if (matched) {
        return matched;
      }
    }
    return sortedRuns[0];
  }, [sortedRuns, selectedRunId]);

  const traceParse = useMemo(
    () => normalizeTraceSteps(activeRun?.trace_json),
    [activeRun],
  );

  const result = activeRun?.result_json ?? null;
  const effectiveVerdict: AIVerdict | null =
    result?.verdict ?? crash.ai_verdict ?? null;
  const effectiveConfidence = normalizeConfidence(
    result?.confidence ?? crash.ai_confidence,
  );
  const linkedIssueId = crash.issue_id ?? result?.matched_issue_id ?? null;

  const isBusy =
    Boolean(isRerunningAI) ||
    activeRun?.status === "RUNNING" ||
    crash.status === "TRIAGING";

  const primaryTraceError = useMemo(() => {
    for (let i = traceParse.steps.length - 1; i >= 0; i -= 1) {
      const step = traceParse.steps[i];
      if (step.role === "system" && step.error) {
        return step.error;
      }
    }
    for (let i = traceParse.steps.length - 1; i >= 0; i -= 1) {
      const step = traceParse.steps[i];
      if (step.error) {
        return step.error;
      }
    }
    return null;
  }, [traceParse.steps]);

  const verdictMeta = effectiveVerdict ? VERDICT_META[effectiveVerdict] : null;
  const VerdictIcon = verdictMeta?.icon ?? Bot;

  const handleCopyPattern = async (pattern: string) => {
    try {
      await navigator.clipboard.writeText(pattern);
      setCopiedPattern(pattern);
      window.setTimeout(() => {
        setCopiedPattern((prev) => (prev === pattern ? null : prev));
      }, 1500);
    } catch {
      // Clipboard unavailable
    }
  };

  const toggleStepExpanded = (stepKey: string) => {
    setExpandedStepKeys((prev) => ({
      ...prev,
      [stepKey]: !prev[stepKey],
    }));
  };

  return (
    <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
      {/* Header & Controls */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-slate-50 px-4 py-3">
        <div className="flex flex-wrap items-center gap-2.5">
          <div className="flex items-center gap-2">
            <Bot className="h-4 w-4 text-indigo-600" />
            <h2 className="text-sm font-semibold text-slate-900">
              {t("agent.title")}
            </h2>
          </div>

          {verdictMeta && effectiveVerdict && (
            <span
              className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-semibold ${verdictMeta.badgeClass}`}
            >
              <VerdictIcon className="h-3.5 w-3.5" />
              {formatVerdict(effectiveVerdict)}
            </span>
          )}

          {effectiveConfidence !== null && (
            <div className="inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-white px-2.5 py-0.5 text-xs">
              <span className="text-slate-500">{t("agent.confidence")}</span>
              <span className="h-1.5 w-12 overflow-hidden rounded-full bg-slate-200">
                <span
                  className={`block h-full rounded-full ${
                    effectiveConfidence >= 75
                      ? "bg-emerald-500"
                      : effectiveConfidence >= 45
                        ? "bg-amber-500"
                        : "bg-rose-500"
                  }`}
                  style={{ width: `${effectiveConfidence}%` }}
                />
              </span>
              <span className="font-mono font-semibold text-slate-800">
                {effectiveConfidence}%
              </span>
            </div>
          )}

          {crash.is_human_verified && (
            <span className="inline-flex items-center gap-1 rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-0.5 text-xs font-semibold text-emerald-700">
              <UserCheck className="h-3.5 w-3.5" />
              {t("agent.humanVerified")}
            </span>
          )}

          {linkedIssueId !== null && (
            <Link
              to={`/issues/${linkedIssueId}`}
              className="inline-flex items-center gap-1 rounded-md border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-xs font-medium text-indigo-700 hover:bg-indigo-100"
            >
              <span>
                {t("agent.issueBadge", { id: linkedIssueId })}
                {crash.issue?.title ? `: ${crash.issue.title}` : ""}
              </span>
              <ExternalLink className="h-3 w-3" />
            </Link>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {sortedRuns.length > 1 && (
            <select
              aria-label={t("agent.selectRunAria")}
              value={activeRun?.id ?? ""}
              onChange={(e) => {
                const nextId = e.target.value;
                setSelectedRunId(
                  !nextId || nextId === newestRunId ? null : nextId,
                );
              }}
              className="h-8 rounded-md border border-slate-300 bg-white px-2 text-xs text-slate-700 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            >
              {sortedRuns.map((run, index) => (
                <option key={run.id} value={run.id}>
                  {t("agent.runOption", {
                    num: sortedRuns.length - index,
                    status: formatAIRunStatus(run.status),
                    model: run.model_name,
                  })}
                </option>
              ))}
            </select>
          )}

          {onVerifyVerdict && (
            <button
              type="button"
              onClick={onVerifyVerdict}
              className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-700 shadow-sm hover:bg-slate-50"
            >
              <UserCheck className="h-3.5 w-3.5 text-slate-600" />
              <span>
                {crash.is_human_verified
                  ? t("agent.editVerdict")
                  : t("agent.verifyAssign")}
              </span>
            </button>
          )}

          {onRerunAI && (
            <button
              type="button"
              onClick={onRerunAI}
              disabled={isBusy}
              className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-2.5 py-1.5 text-xs font-medium text-white shadow-sm hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {isBusy ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  <span>{t("agent.running")}</span>
                </>
              ) : sortedRuns.length > 0 ? (
                <>
                  <RotateCcw className="h-3.5 w-3.5" />
                  <span>{t("agent.rerunAi")}</span>
                </>
              ) : (
                <>
                  <Play className="h-3.5 w-3.5" />
                  <span>{t("agent.runAi")}</span>
                </>
              )}
            </button>
          )}
        </div>
      </div>

      {/* Body */}
      <div className="space-y-4 p-4">
        {/* Status Callout Banners */}
        {(crash.status === "PENDING" || crash.status === "PARSING") &&
          !activeRun && (
            <div className="flex items-center gap-2.5 rounded-lg border border-slate-200 bg-slate-50 px-3.5 py-3 text-xs text-slate-700">
              <Loader2 className="h-4 w-4 shrink-0 animate-spin text-indigo-600" />
              <span>{t("agent.waitingParsing")}</span>
            </div>
          )}

        {(crash.status === "TRIAGING" || activeRun?.status === "RUNNING") && (
          <div className="flex items-center gap-2.5 rounded-lg border border-indigo-200 bg-indigo-50 px-3.5 py-3 text-xs text-indigo-900">
            <Loader2 className="h-4 w-4 shrink-0 animate-spin text-indigo-600" />
            <span>{t("agent.investigating")}</span>
          </div>
        )}

        {(activeRun?.status === "FAILED" ||
          (crash.status === "FAILED" && !result)) && (
          <div
            className="flex items-start gap-2.5 rounded-lg border border-rose-200 bg-rose-50 px-3.5 py-3 text-xs text-rose-900"
            role="alert"
          >
            <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-rose-600" />
            <div className="min-w-0 flex-1">
              <p className="font-semibold">{t("agent.analysisFailedTitle")}</p>
              {primaryTraceError ? (
                <p className="mt-1 break-words font-mono text-[11px] text-rose-800">
                  {primaryTraceError}
                </p>
              ) : (
                <p className="mt-1 text-rose-700">
                  {t("agent.analysisFailedFallback")}
                </p>
              )}
            </div>
          </div>
        )}

        {effectiveVerdict === "NEEDS_REVIEW" &&
          activeRun?.status !== "FAILED" && (
            <div className="flex items-start gap-2.5 rounded-lg border border-amber-200 bg-amber-50 px-3.5 py-3 text-xs text-amber-900">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
              <div>
                <p className="font-semibold">{t("agent.needsReviewTitle")}</p>
                <p className="mt-0.5 text-amber-800">
                  {t("agent.needsReviewDesc")}
                </p>
              </div>
            </div>
          )}

        {!activeRun && crash.status === "ANALYZED" && (
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3.5 py-3 text-xs text-slate-700">
            <span>{t("agent.skippedBanner")}</span>
            {onRerunAI && (
              <button
                type="button"
                onClick={onRerunAI}
                disabled={isBusy}
                className="font-medium text-indigo-600 hover:text-indigo-700 disabled:opacity-50"
              >
                {t("agent.runManually")}
              </button>
            )}
          </div>
        )}

        {/* Structured AI Result */}
        {result && (
          <div className="space-y-4">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <div className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                  {t("agent.suggestedTitle")}
                </div>
                <h3 className="mt-0.5 text-sm font-semibold text-slate-900">
                  {result.suggested_title || t("agent.untitledFinding")}
                </h3>
              </div>
              {result.suggested_component && (
                <span className="rounded-md border border-slate-200 bg-slate-100 px-2 py-0.5 font-mono text-xs text-slate-700">
                  {result.suggested_component}
                </span>
              )}
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <div className="rounded-lg border border-slate-200 bg-slate-50/70 p-3">
                <div className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                  {t("agent.rootCauseAnalysis")}
                </div>
                <p className="mt-1.5 whitespace-pre-wrap text-xs leading-relaxed text-slate-800">
                  {result.root_cause_analysis || t("agent.noRootCause")}
                </p>
              </div>

              <div className="rounded-lg border border-slate-200 bg-slate-50/70 p-3">
                <div className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                  {t("agent.reasoningSummary")}
                </div>
                <p className="mt-1.5 whitespace-pre-wrap text-xs leading-relaxed text-slate-700">
                  {result.reasoning_summary || t("agent.noReasoningSummary")}
                </p>
              </div>
            </div>

            {/* Evidence Log Snippets */}
            <div>
              <div className="mb-2 flex items-center justify-between">
                <span className="text-xs font-semibold text-slate-700">
                  {t("agent.evidenceLogSnippets", {
                    count: result.evidence_log_snippets?.length ?? 0,
                  })}
                </span>
              </div>
              {result.evidence_log_snippets &&
              result.evidence_log_snippets.length > 0 ? (
                <div className="grid gap-2 sm:grid-cols-2">
                  {result.evidence_log_snippets.map((snippet, idx) => {
                    const [safeStart, safeEnd] = formatSafeLineRange(
                      snippet.start_line,
                      snippet.end_line,
                    );
                    const logName =
                      crash.logs.find((l) => l.id === snippet.log_id)
                        ?.filename ?? snippet.log_id;
                    return (
                      <button
                        key={`${snippet.log_id}-${safeStart}-${safeEnd}-${idx}`}
                        type="button"
                        onClick={() =>
                          onSelectLogEvidence(snippet.log_id, safeStart, safeEnd)
                        }
                        className="group flex w-full flex-col rounded-lg border border-slate-200 bg-white p-3 text-left transition-colors hover:border-indigo-300 hover:bg-indigo-50/40"
                      >
                        <div className="flex w-full items-center justify-between gap-2">
                          <span className="inline-flex min-w-0 items-center gap-1.5 text-xs font-medium text-slate-900">
                            <FileText className="h-3.5 w-3.5 shrink-0 text-indigo-600" />
                            <span className="truncate">{logName}</span>
                          </span>
                          <span className="shrink-0 rounded border border-indigo-200 bg-indigo-50 px-1.5 py-0.5 font-mono text-[11px] font-semibold text-indigo-700">
                            L{safeStart}–L{safeEnd}
                          </span>
                        </div>
                        <p className="mt-1.5 line-clamp-2 text-xs text-slate-600">
                          {snippet.relevance}
                        </p>
                        <span className="mt-2 text-[11px] font-medium text-indigo-600 group-hover:underline">
                          {t("agent.jumpToLog")}
                        </span>
                      </button>
                    );
                  })}
                </div>
              ) : (
                <p className="text-xs text-slate-500">
                  {t("agent.noLogSnippets")}
                </p>
              )}
            </div>

            {/* Key Log Patterns */}
            {result.key_log_patterns && result.key_log_patterns.length > 0 && (
              <div>
                <div className="mb-1.5 text-xs font-semibold text-slate-700">
                  {t("agent.keyLogPatterns")}
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {result.key_log_patterns.map((pattern, idx) => {
                    const isCopied = copiedPattern === pattern;
                    return (
                      <button
                        key={`${pattern}-${idx}`}
                        type="button"
                        onClick={() => {
                          void handleCopyPattern(pattern);
                        }}
                        title={t("agent.clickToCopyPattern")}
                        className="inline-flex items-center gap-1.5 rounded-md border border-slate-200 bg-slate-100 px-2 py-1 font-mono text-xs text-slate-800 hover:border-slate-300 hover:bg-slate-200/70"
                      >
                        <span>{pattern}</span>
                        {isCopied ? (
                          <Check className="h-3 w-3 text-emerald-600" />
                        ) : (
                          <Copy className="h-3 w-3 text-slate-400" />
                        )}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        )}

        {/* Run Telemetry Footer + Collapsible ReAct Trace */}
        {activeRun && (
          <div className="border-t border-slate-200 pt-3">
            <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500">
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
                <span>
                  {t("agent.modelLabel")}
                  <span className="font-mono font-medium text-slate-700">
                    {activeRun.model_name}
                  </span>
                </span>
                <span>
                  {t("agent.tokensLabel")}
                  <span className="font-mono font-medium text-slate-700">
                    {activeRun.prompt_tokens + activeRun.completion_tokens}
                  </span>{" "}
                  <span className="font-mono text-[11px] text-slate-400">
                    {t("agent.tokensBreakdown", {
                      prompt: activeRun.prompt_tokens,
                      completion: activeRun.completion_tokens,
                    })}
                  </span>
                </span>
                <span className="inline-flex items-center gap-1">
                  <Clock className="h-3.5 w-3.5 text-slate-400" />
                  <span className="font-mono font-medium text-slate-700">
                    {formatDuration(activeRun.duration_ms)}
                  </span>
                </span>
                <span>{formatDateTime(activeRun.created_at)}</span>
              </div>

              <button
                type="button"
                aria-expanded={traceExpanded}
                onClick={() => setTraceExpanded((prev) => !prev)}
                className="inline-flex items-center gap-1.5 rounded-md border border-slate-200 bg-slate-50 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-100"
              >
                {traceExpanded ? (
                  <ChevronDown className="h-3.5 w-3.5" />
                ) : (
                  <ChevronRight className="h-3.5 w-3.5" />
                )}
                <Terminal className="h-3.5 w-3.5 text-slate-500" />
                <span>
                  {t("agent.investigationTrace", {
                    count: traceParse.steps.length,
                  })}
                </span>
              </button>
            </div>

            {traceExpanded && (
              <div className="mt-3 space-y-2.5">
                {traceParse.malformed && (
                  <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900">
                    <p className="font-semibold">{t("agent.malformedTrace")}</p>
                    <pre className="ck-scrollbar mt-2 max-h-48 overflow-auto rounded bg-white/80 p-2 font-mono text-[11px] text-slate-700">
                      {formatTraceValue(activeRun.trace_json)}
                    </pre>
                  </div>
                )}

                {traceParse.steps.length === 0 && !traceParse.malformed ? (
                  <p className="rounded-lg border border-dashed border-slate-200 bg-slate-50 px-3 py-4 text-center text-xs text-slate-500">
                    {t("agent.emptyTrace")}
                  </p>
                ) : (
                  <div className="ck-scrollbar max-h-[420px] space-y-2 overflow-y-auto pr-1">
                    {traceParse.steps.map((step, idx) => {
                      const stepKey = `${activeRun.id}-${step.step}-${idx}`;
                      const argsStr = formatTraceValue(step.arguments);
                      const outputStr = step.output_preview ?? "";
                      const isLongOutput = outputStr.length > 400;
                      const isOutputExpanded = Boolean(expandedStepKeys[stepKey]);
                      const displayedOutput =
                        isLongOutput && !isOutputExpanded
                          ? `${outputStr.slice(0, 400)}...`
                          : outputStr;
                      const isReasoningStep = step.role === "reasoning";
                      const roleDisplay =
                        step.role === "reasoning"
                          ? t("agent.roleReasoning")
                          : step.role === "tool"
                            ? t("agent.roleTool")
                            : step.role === "system"
                              ? t("agent.roleSystem")
                              : step.role;

                      return (
                        <div
                          key={stepKey}
                          className={`rounded-lg border p-3 text-xs ${
                            isReasoningStep
                              ? "border-violet-200 bg-violet-50/40"
                              : "border-slate-200 bg-slate-50/60"
                          }`}
                        >
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="font-mono font-semibold text-slate-500">
                                #{step.step ?? idx + 1}
                              </span>
                              {roleDisplay && (
                                <span
                                  className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium ${
                                    isReasoningStep
                                      ? "border border-violet-200 bg-violet-100 text-violet-800"
                                      : "bg-slate-200/80 font-mono text-slate-700"
                                  }`}
                                >
                                  {isReasoningStep && (
                                    <Brain className="h-3 w-3" />
                                  )}
                                  {roleDisplay}
                                </span>
                              )}
                              {step.tool_name && (
                                <span className="inline-flex items-center gap-1 rounded border border-indigo-200 bg-indigo-50 px-2 py-0.5 font-mono text-[11px] font-semibold text-indigo-700">
                                  <Wrench className="h-3 w-3" />
                                  {step.tool_name}
                                </span>
                              )}
                              {step.error && (
                                <span className="rounded border border-rose-200 bg-rose-50 px-1.5 py-0.5 text-[11px] font-semibold text-rose-700">
                                  {t("agent.errorBadge")}
                                </span>
                              )}
                            </div>

                            {step.duration_ms !== null &&
                              step.duration_ms !== undefined && (
                                <span className="font-mono text-[11px] text-slate-400">
                                  {formatDuration(step.duration_ms)}
                                </span>
                              )}
                          </div>

                          {step.message && (
                            <p className="mt-2 whitespace-pre-wrap text-slate-700">
                              {step.message}
                            </p>
                          )}

                          {argsStr && (
                            <div className="mt-2">
                              <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-slate-400">
                                {t("agent.argumentsLabel")}
                              </div>
                              <pre className="ck-scrollbar max-h-36 overflow-auto rounded border border-slate-200 bg-white p-2 font-mono text-[11px] text-slate-700">
                                {argsStr}
                              </pre>
                            </div>
                          )}

                          {outputStr && (
                            <div className="mt-2">
                              <div className="mb-1 flex items-center justify-between">
                                <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">
                                  {isReasoningStep
                                    ? t("agent.reasoningPreviewLabel")
                                    : t("agent.outputPreviewLabel")}
                                </span>
                                {isLongOutput && (
                                  <button
                                    type="button"
                                    onClick={() => toggleStepExpanded(stepKey)}
                                    className="text-[11px] font-medium text-indigo-600 hover:text-indigo-700"
                                  >
                                    {isOutputExpanded
                                      ? t("agent.showLess")
                                      : t("agent.showMore")}
                                  </button>
                                )}
                              </div>
                              <pre
                                className={`ck-scrollbar max-h-56 overflow-auto whitespace-pre-wrap rounded border p-2 font-mono text-[11px] ${
                                  isReasoningStep
                                    ? "border-violet-200 bg-white text-slate-800"
                                    : "border-slate-200 bg-slate-950 text-slate-100"
                                }`}
                              >
                                {displayedOutput}
                              </pre>
                            </div>
                          )}

                          {step.error && (
                            <div className="mt-2 rounded border border-rose-200 bg-rose-50 p-2 font-mono text-[11px] text-rose-800">
                              {step.error}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default AgentTracePanel;
