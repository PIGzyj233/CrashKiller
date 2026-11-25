import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeft, X } from "lucide-react";
import {
  ApiError,
  pinCrash,
  reprocessCrash,
  submitCrashVerdict,
} from "../api/client";
import type {
  CrashDetailRead,
  CrashVerdictRequest,
  ReprocessMode,
} from "../api/types";
import { AgentTracePanel } from "../components/AgentTracePanel";
import { useProjectContext } from "../components/Layout";
import { StackTraceViewer } from "../components/StackTraceViewer";
import {
  VirtualLogViewer,
  type LogHighlightRange,
} from "../components/VirtualLogViewer";
import { useAppI18n } from "../i18n";
import { getSessionIdLabel } from "../utils/sessionLabel";
import { CrashDetailHeader } from "./crashDetail/CrashDetailHeader";
import { CrashPipelineBanner } from "./crashDetail/CrashPipelineBanner";
import { HumanVerdictModal } from "./crashDetail/HumanVerdictModal";
import { RelatedSessionCrashesCard } from "./crashDetail/RelatedSessionCrashesCard";
import { useCrashDetailLive } from "./crashDetail/useCrashDetailLive";
import { getLatestAIResult } from "./crashDetail/utils";

export function CrashDetailPage() {
  const { t } = useAppI18n();
  const { crashId } = useParams<{ crashId: string }>();
  const { projects } = useProjectContext();
  const queryClient = useQueryClient();

  const [activeLogId, setActiveLogId] = useState<string>("");
  const [highlightRange, setHighlightRange] =
    useState<LogHighlightRange | null>(null);
  const [isVerdictModalOpen, setIsVerdictModalOpen] = useState(false);

  const logViewerSectionRef = useRef<HTMLDivElement | null>(null);

  // Reset ephemeral workbench state on crashId route change
  useEffect(() => {
    setActiveLogId("");
    setHighlightRange(null);
    setIsVerdictModalOpen(false);
  }, [crashId]);

  const {
    crashQuery,
    crash,
    inFlight,
    sseConnected,
    sseStage,
    sseError,
    resetSseState,
  } = useCrashDetailLive(crashId);

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

  const reprocessMutation = useMutation({
    mutationFn: (mode: ReprocessMode) => reprocessCrash(crashId!, { mode }),
    onSuccess: (res) => {
      resetSseState();
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
    setIsVerdictModalOpen(true);
  };

  const project = useMemo(
    () => (crash ? projects.find((p) => p.id === crash.project_id) : undefined),
    [crash, projects],
  );

  const sessionLabel = useMemo(
    () => getSessionIdLabel(project, t("common.sessionId")),
    [project, t],
  );

  const handleLocateSessionLogs = () => {
    if (!crash?.logs || crash.logs.length === 0) {
      return;
    }
    const matchedLog =
      crash.logs.find((lg) => (lg.digest_json?.session?.match_count ?? 0) > 0) ??
      crash.logs[0];
    if (matchedLog) {
      setActiveLogId(matchedLog.id);
      const lastLine = matchedLog.digest_json?.session?.last_line;
      if (typeof lastLine === "number" && lastLine >= 1) {
        setHighlightRange((prev) => ({
          logId: matchedLog.id,
          startLine: lastLine,
          endLine: lastLine,
          nonce: (prev?.nonce ?? 0) + 1,
        }));
      }
    }
    logViewerSectionRef.current?.scrollIntoView({
      behavior: "smooth",
      block: "nearest",
    });
  };

  const latestAIResult = useMemo(() => getLatestAIResult(crash), [crash]);

  if (!crashId) {
    return (
      <div className="rounded-xl border border-rose-200 bg-white p-6">
        <p className="text-sm text-rose-700">{t("crashDetail.missingId")}</p>
        <Link
          to="/crashes"
          className="mt-3 inline-flex items-center gap-1 text-sm font-medium text-indigo-600 hover:underline"
        >
          <ArrowLeft className="h-4 w-4" />
          {t("crashDetail.backToCrashes")}
        </Link>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <CrashDetailHeader
        crashId={crashId}
        crash={crash}
        project={project}
        sessionLabel={sessionLabel}
        inFlight={inFlight}
        isReprocessing={reprocessMutation.isPending}
        isPinPending={pinMutation.isPending}
        onReprocess={(mode) => reprocessMutation.mutate(mode)}
        onOpenVerdictModal={openVerdictModal}
        onTogglePin={(nextPinned) => pinMutation.mutate(nextPinned)}
        onLocateSessionLogs={handleLocateSessionLogs}
      />

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
                : t("crashDetail.loadFailed")}
            </span>
          </div>
          <button
            type="button"
            onClick={() => void crashQuery.refetch()}
            className="rounded border border-rose-300 bg-white px-2.5 py-1 text-xs font-medium text-rose-700"
          >
            {t("common.retry")}
          </button>
        </div>
      )}

      {reprocessMutation.isError && (
        <div className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-2.5 text-xs text-rose-700">
          {reprocessMutation.error instanceof ApiError
            ? reprocessMutation.error.message
            : t("crashDetail.reprocessFailed")}
        </div>
      )}

      {pinMutation.isError && (
        <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-4 py-2.5 text-xs text-rose-700">
          <span>
            {pinMutation.error instanceof ApiError
              ? pinMutation.error.message
              : t("crashDetail.pinFailed")}
          </span>
          <button
            type="button"
            onClick={() => pinMutation.reset()}
            className="rounded p-0.5 text-rose-700 hover:bg-rose-100"
            aria-label={t("crashDetail.dismissPinError")}
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      {crash && (
        <CrashPipelineBanner
          status={crash.status}
          inFlight={inFlight}
          sseConnected={sseConnected}
          sseStage={sseStage}
          sseError={sseError}
          onRetryFullPipeline={() => reprocessMutation.mutate("full")}
        />
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

          {crash.session_id && (
            <RelatedSessionCrashesCard
              sessionId={crash.session_id}
              sessionLabel={sessionLabel}
              relatedCrashes={crash.related_session_crashes ?? []}
              relatedCount={crash.related_session_crash_count}
            />
          )}

          <div ref={logViewerSectionRef}>
            <VirtualLogViewer
              key={crash.id}
              logs={crash.logs ?? []}
              activeLogId={activeLogId}
              onSelectLog={setActiveLogId}
              crashTime={crash.crash_time}
              sessionId={crash.session_id}
              sessionLabel={sessionLabel}
              highlightRange={highlightRange}
            />
          </div>
        </div>
      )}

      {isVerdictModalOpen && crash && (
        <HumanVerdictModal
          crash={crash}
          latestAIResult={latestAIResult}
          isPending={verdictMutation.isPending}
          errorMessage={
            verdictMutation.isError
              ? verdictMutation.error instanceof ApiError
                ? verdictMutation.error.message
                : t("crashDetail.submitFailed")
              : null
          }
          onClose={() => setIsVerdictModalOpen(false)}
          onSubmit={(payload) => verdictMutation.mutate(payload)}
        />
      )}
    </div>
  );
}

export default CrashDetailPage;
