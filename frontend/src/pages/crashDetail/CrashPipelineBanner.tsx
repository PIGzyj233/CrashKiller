import { AlertTriangle, Loader2, Radio, RefreshCw } from "lucide-react";
import type { CrashStatus } from "../../api/types";
import { useAppI18n } from "../../i18n";
import { PIPELINE_STEPS } from "./utils";

interface CrashPipelineBannerProps {
  status: CrashStatus;
  inFlight: boolean;
  sseConnected: boolean;
  sseStage: string | null;
  sseError: string | null;
  onRetryFullPipeline: () => void;
}

export function CrashPipelineBanner({
  status,
  inFlight,
  sseConnected,
  sseStage,
  sseError,
  onRetryFullPipeline,
}: CrashPipelineBannerProps) {
  const { t, formatCrashStatus } = useAppI18n();

  if (!inFlight && status !== "FAILED" && !sseError) {
    return null;
  }

  return (
    <div
      className={`rounded-xl border px-4 py-3.5 shadow-sm ${
        status === "FAILED" || sseError
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
            {status === "FAILED"
              ? t("crashDetail.processingFailed")
              : `${t("crashDetail.livePipeline", {
                  status: formatCrashStatus(status),
                })}${sseStage ? ` (${sseStage})` : ""}`}
          </span>
        </div>

        <div className="flex items-center gap-3 text-xs">
          {inFlight && (
            <span className="inline-flex items-center gap-1 rounded-full bg-white/80 px-2.5 py-0.5 font-medium text-indigo-700">
              <Radio className="h-3 w-3" />
              {sseConnected
                ? t("crashDetail.sseLive")
                : t("crashDetail.pollingFallback")}
            </span>
          )}
          {status === "FAILED" && (
            <button
              type="button"
              onClick={onRetryFullPipeline}
              className="inline-flex items-center gap-1 rounded-md bg-rose-600 px-2.5 py-1 font-medium text-white hover:bg-rose-700"
            >
              <RefreshCw className="h-3 w-3" />
              {t("crashDetail.retryFullPipeline")}
            </button>
          )}
        </div>
      </div>

      {inFlight && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {PIPELINE_STEPS.map((step, idx) => {
            const currentIdx = PIPELINE_STEPS.indexOf(status);
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
                <span>{formatCrashStatus(step)}</span>
              </div>
            );
          })}
        </div>
      )}

      {sseError && (
        <p className="mt-2 font-mono text-xs text-rose-800">{sseError}</p>
      )}
    </div>
  );
}
