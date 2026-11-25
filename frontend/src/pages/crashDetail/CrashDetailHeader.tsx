import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  ArrowLeft,
  Bug,
  Check,
  CheckCircle2,
  Copy,
  Download,
  FileSearch,
  Pin,
  RotateCcw,
  Sparkles,
  UserCheck,
} from "lucide-react";
import { getCrashArtifactUrl } from "../../api/client";
import type {
  CrashDetailRead,
  ProjectRead,
  ReprocessMode,
} from "../../api/types";
import { useAppI18n } from "../../i18n";

interface CrashDetailHeaderProps {
  crashId: string;
  crash: CrashDetailRead | undefined;
  project: ProjectRead | undefined;
  sessionLabel: string;
  inFlight: boolean;
  isReprocessing: boolean;
  isPinPending: boolean;
  onReprocess: (mode: ReprocessMode) => void;
  onOpenVerdictModal: () => void;
  onTogglePin: (nextPinned: boolean) => void;
  onLocateSessionLogs: () => void;
}

export function CrashDetailHeader({
  crashId,
  crash,
  project,
  sessionLabel,
  inFlight,
  isReprocessing,
  isPinPending,
  onReprocess,
  onOpenVerdictModal,
  onTogglePin,
  onLocateSessionLogs,
}: CrashDetailHeaderProps) {
  const { t, formatDateTime } = useAppI18n();
  const [copiedSessionId, setCopiedSessionId] = useState(false);

  useEffect(() => {
    setCopiedSessionId(false);
  }, [crashId]);

  const handleCopySessionId = () => {
    if (!crash?.session_id) {
      return;
    }
    void navigator.clipboard?.writeText(crash.session_id).then(() => {
      setCopiedSessionId(true);
      window.setTimeout(() => setCopiedSessionId(false), 1800);
    });
  };

  return (
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div className="space-y-1.5">
        <div className="flex items-center gap-2 text-xs text-slate-500">
          <Link
            to="/crashes"
            className="inline-flex items-center gap-1 font-medium text-indigo-600 hover:underline"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            {t("nav.crashes")}
          </Link>
          <span>/</span>
          <span className="font-mono text-slate-700">{crashId}</span>
        </div>

        <div className="flex flex-wrap items-center gap-2.5">
          <Bug className="h-5 w-5 text-indigo-600" />
          <h1 className="text-xl font-semibold text-slate-900">
            {crash?.crash_reason || t("crashDetail.workbenchTitle")}
          </h1>
          {crash?.is_human_verified && (
            <span className="inline-flex items-center gap-1 rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-700">
              <CheckCircle2 className="h-3.5 w-3.5" />
              {t("crashDetail.humanVerified")}
            </span>
          )}
        </div>

        {crash && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
            <span>
              {t("crashDetail.projectLabel")}
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
              {t("crashDetail.crashTimeLabel", {
                time: formatDateTime(crash.crash_time || crash.created_at),
              })}
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
                  {t("crashDetail.clusterLink", {
                    hash: crash.cluster.signature_hash.slice(0, 10),
                    count: crash.cluster.crash_count,
                  })}
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
                  {t("crashDetail.issueLink", {
                    id: crash.issue.id,
                    title: crash.issue.title,
                  })}
                </Link>
              </>
            )}
            {crash.session_id && (
              <>
                <span>|</span>
                <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-0.5 text-emerald-800">
                  <span className="font-medium">{sessionLabel}:</span>
                  <Link
                    to={`/crashes?session_id=${encodeURIComponent(crash.session_id)}`}
                    title={t("crashDetail.sessionFilterLink", {
                      label: sessionLabel,
                    })}
                    className="font-mono font-semibold text-emerald-700 hover:underline"
                  >
                    {crash.session_id}
                  </Link>
                  <button
                    type="button"
                    onClick={handleCopySessionId}
                    aria-label={t("crashDetail.sessionCopyAria", {
                      label: sessionLabel,
                    })}
                    title={t("common.copy")}
                    className="rounded p-0.5 text-emerald-700 hover:bg-emerald-100"
                  >
                    {copiedSessionId ? (
                      <Check className="h-3 w-3" />
                    ) : (
                      <Copy className="h-3 w-3" />
                    )}
                  </button>
                  {crash.logs.length > 0 && (
                    <button
                      type="button"
                      onClick={onLocateSessionLogs}
                      title={t("crashDetail.sessionJumpLogs")}
                      className="inline-flex items-center gap-0.5 rounded bg-white/80 px-1.5 py-0.5 text-[11px] font-medium text-emerald-800 hover:bg-white"
                    >
                      <FileSearch className="h-3 w-3" />
                      <span>{t("crashDetail.sessionJumpLogs")}</span>
                    </button>
                  )}
                </span>
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
            disabled={isReprocessing || inFlight}
            onClick={() => onReprocess("full")}
            className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 shadow-sm hover:bg-slate-50 disabled:opacity-50"
          >
            <RotateCcw className="h-3.5 w-3.5" />
            {t("crashDetail.reprocessFull")}
          </button>

          <button
            type="button"
            disabled={isReprocessing || inFlight}
            onClick={() => onReprocess("triage")}
            className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 shadow-sm hover:bg-slate-50 disabled:opacity-50"
          >
            <Sparkles className="h-3.5 w-3.5 text-indigo-600" />
            {t("crashDetail.rerunAi")}
          </button>

          <button
            type="button"
            onClick={onOpenVerdictModal}
            className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white shadow-sm hover:bg-indigo-700"
          >
            <UserCheck className="h-3.5 w-3.5" />
            {t("crashDetail.verifyAssign")}
          </button>

          <button
            type="button"
            disabled={isPinPending}
            onClick={() => onTogglePin(!crash.is_pinned)}
            className={`inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium shadow-sm transition-colors ${
              crash.is_pinned
                ? "border-amber-300 bg-amber-50 text-amber-800 hover:bg-amber-100"
                : "border-slate-300 bg-white text-slate-700 hover:bg-slate-50"
            }`}
          >
            <Pin className="h-3.5 w-3.5" />
            {crash.is_pinned ? t("crashDetail.pinned") : t("crashDetail.pin")}
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
              title={t("crashDetail.dumpUnavailableTitle")}
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
              title={t("crashDetail.stacksUnavailableTitle")}
            >
              <Download className="h-3.5 w-3.5" />
              stacks.json
            </span>
          )}
        </div>
      )}
    </div>
  );
}
