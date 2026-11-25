import { AlertTriangle, Clock, Filter, Search } from "lucide-react";
import type { LogSessionDigest } from "../../api/types";
import { useAppI18n } from "../../i18n";

export interface SessionContextBarProps {
  normalizedSessionId: string;
  resolvedSessionLabel: string;
  activeSessionDigest: LogSessionDigest | null;
  hasGcError: boolean;
  aroundTimeLoading: boolean;
  isSearching: boolean;
  sessionOnlyMode: boolean;
  onJumpToLine: (lineNumber: number) => void;
  onJumpSessionLastTs: (ts: string) => void;
  onSearchSessionId: () => void;
  onToggleSessionOnlyMode: () => void;
}

export function SessionContextBar({
  normalizedSessionId,
  resolvedSessionLabel,
  activeSessionDigest,
  hasGcError,
  aroundTimeLoading,
  isSearching,
  sessionOnlyMode,
  onJumpToLine,
  onJumpSessionLastTs,
  onSearchSessionId,
  onToggleSessionOnlyMode,
}: SessionContextBarProps) {
  const { t, formatNumber } = useAppI18n();

  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-emerald-200 bg-emerald-50/70 px-3 py-2 text-xs text-emerald-950">
      <div className="flex flex-wrap items-center gap-2">
        <span className="inline-flex items-center gap-1 rounded-full border border-emerald-300 bg-white px-2.5 py-0.5 font-medium text-emerald-800">
          <span>{resolvedSessionLabel}:</span>
          <code className="font-mono font-semibold">{normalizedSessionId}</code>
        </span>
        {activeSessionDigest && activeSessionDigest.match_count > 0 ? (
          <>
            <span className="font-mono text-[11px] text-emerald-800">
              {t("log.sessionHitsShort", {
                count: activeSessionDigest.match_count,
              })}
              {activeSessionDigest.first_line != null &&
              activeSessionDigest.last_line != null
                ? ` · ${t("log.sessionSpan", {
                    first: formatNumber(activeSessionDigest.first_line),
                    last: formatNumber(activeSessionDigest.last_line),
                  })}`
                : ""}
              {` · ${activeSessionDigest.error_count}E / ${activeSessionDigest.warn_count}W`}
            </span>
            {(activeSessionDigest.trailing_lines_after_session ?? 0) > 0 && (
              <span className="inline-flex items-center gap-1 rounded-full border border-amber-300 bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-800">
                <AlertTriangle className="h-3 w-3 shrink-0 text-amber-600" />
                <span>
                  {t("log.sessionMidFileHint", {
                    count:
                      activeSessionDigest.trailing_lines_after_session ?? 0,
                  })}
                </span>
              </span>
            )}
          </>
        ) : (
          <span className="text-[11px] text-slate-600">
            {t("log.sessionNotFoundInLog", { label: resolvedSessionLabel })}
          </span>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        {activeSessionDigest?.first_line != null && (
          <button
            type="button"
            disabled={hasGcError}
            onClick={() => onJumpToLine(activeSessionDigest.first_line!)}
            className="rounded-md border border-emerald-300 bg-white px-2 py-1 text-[11px] font-medium text-emerald-800 hover:bg-emerald-100 disabled:opacity-50"
          >
            {t("log.jumpSessionStartBtn")}
          </button>
        )}
        {activeSessionDigest?.last_line != null && (
          <button
            type="button"
            disabled={hasGcError}
            onClick={() => onJumpToLine(activeSessionDigest.last_line!)}
            className="rounded-md border border-emerald-400 bg-emerald-600 px-2 py-1 text-[11px] font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
          >
            {t("log.jumpSessionEndBtn")}
          </button>
        )}
        {activeSessionDigest?.last_ts && (
          <button
            type="button"
            disabled={aroundTimeLoading || hasGcError}
            onClick={() => onJumpSessionLastTs(activeSessionDigest.last_ts!)}
            title={t("log.jumpSessionLastTsTitle", {
              time: activeSessionDigest.last_ts,
            })}
            className="inline-flex items-center gap-1 rounded-md border border-emerald-300 bg-white px-2 py-1 text-[11px] font-medium text-emerald-800 hover:bg-emerald-100 disabled:opacity-50"
          >
            <Clock className="h-3 w-3" />
            <span>{t("log.jumpSessionLastTsBtn")}</span>
          </button>
        )}
        <button
          type="button"
          disabled={isSearching || hasGcError}
          onClick={onSearchSessionId}
          className="inline-flex items-center gap-1 rounded-md border border-emerald-300 bg-white px-2 py-1 text-[11px] font-medium text-emerald-800 hover:bg-emerald-100 disabled:opacity-50"
        >
          <Search className="h-3 w-3" />
          <span>{t("log.searchSessionIdBtn")}</span>
        </button>
        <button
          type="button"
          disabled={hasGcError}
          onClick={onToggleSessionOnlyMode}
          aria-pressed={sessionOnlyMode}
          className={`inline-flex items-center gap-1 rounded-md border px-2.5 py-1 text-[11px] font-semibold transition-colors disabled:opacity-50 ${
            sessionOnlyMode
              ? "border-indigo-500 bg-indigo-600 text-white hover:bg-indigo-700"
              : "border-emerald-300 bg-white text-emerald-800 hover:bg-emerald-100"
          }`}
        >
          <Filter className="h-3 w-3" />
          <span>
            {sessionOnlyMode
              ? t("log.sessionOnlyExitBtn")
              : t("log.sessionOnlyBtn")}
          </span>
        </button>
      </div>
    </div>
  );
}
