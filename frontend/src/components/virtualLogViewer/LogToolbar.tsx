import type { FormEvent } from "react";
import {
  AlertCircle,
  AlertTriangle,
  ChevronDown,
  ChevronUp,
  Clock,
  Filter,
  Loader2,
  Search,
  X,
} from "lucide-react";
import type { LogSearchResponse } from "../../api/types";
import { useAppI18n } from "../../i18n";
import type { AroundTimeInfo } from "./types";

export interface LogToolbarProps {
  resolvedActiveLogId: string;
  hasGcError: boolean;
  normalizedSessionId: string | null;
  resolvedSessionLabel: string;
  crashTime?: string | null;

  searchPattern: string;
  isRegex: boolean;
  caseSensitive: boolean;
  filterToSession: boolean;
  isSearching: boolean;
  searchResult: LogSearchResponse | null;
  activeMatchIdx: number;
  searchError: string | null;

  timestampInput: string;
  windowSec: number;
  aroundTimeLoading: boolean;
  aroundTimeInfo: AroundTimeInfo | null;
  aroundTimeError: string | null;

  onSearchPatternChange: (value: string) => void;
  onClearSearch: () => void;
  onToggleRegex: () => void;
  onToggleCaseSensitive: () => void;
  onToggleFilterToSession: () => void;
  onSearchSubmit: (e: FormEvent) => void;
  onGoToMatch: (delta: 1 | -1) => void;
  onDismissSearchError: () => void;

  onJumpToCrashTime: () => void;
  onTimestampInputChange: (value: string) => void;
  onWindowSecChange: (sec: number) => void;
  onJumpAroundTime: () => void;
  onDismissAroundTimeError: () => void;
  onDismissAroundTimeInfo: () => void;
}

export function LogToolbar({
  resolvedActiveLogId,
  hasGcError,
  normalizedSessionId,
  resolvedSessionLabel,
  crashTime,
  searchPattern,
  isRegex,
  caseSensitive,
  filterToSession,
  isSearching,
  searchResult,
  activeMatchIdx,
  searchError,
  timestampInput,
  windowSec,
  aroundTimeLoading,
  aroundTimeInfo,
  aroundTimeError,
  onSearchPatternChange,
  onClearSearch,
  onToggleRegex,
  onToggleCaseSensitive,
  onToggleFilterToSession,
  onSearchSubmit,
  onGoToMatch,
  onDismissSearchError,
  onJumpToCrashTime,
  onTimestampInputChange,
  onWindowSecChange,
  onJumpAroundTime,
  onDismissAroundTimeError,
  onDismissAroundTimeInfo,
}: LogToolbarProps) {
  const { t } = useAppI18n();

  return (
    <div className="space-y-2 border-b border-slate-200 bg-white px-3 py-2.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        {/* Search Form */}
        <form
          onSubmit={onSearchSubmit}
          className="flex min-w-[260px] flex-1 flex-wrap items-center gap-1.5"
        >
          <div className="relative min-w-[180px] flex-1 sm:max-w-sm">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-400" />
            <input
              type="text"
              value={searchPattern}
              onChange={(e) => onSearchPatternChange(e.target.value)}
              placeholder={t("log.searchPlaceholder")}
              aria-label={t("log.searchAria")}
              disabled={hasGcError}
              className="h-8 w-full rounded-md border border-slate-300 bg-white pl-8 pr-7 font-mono text-xs text-slate-800 placeholder:font-sans placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:bg-slate-100"
            />
            {searchPattern && (
              <button
                type="button"
                onClick={onClearSearch}
                aria-label={t("log.clearSearchAria")}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
          </div>

          <button
            type="button"
            onClick={onToggleRegex}
            aria-pressed={isRegex}
            title={t("log.toggleRegexTitle")}
            className={`h-8 rounded-md border px-2 font-mono text-xs font-semibold transition-colors ${
              isRegex
                ? "border-indigo-300 bg-indigo-50 text-indigo-700"
                : "border-slate-300 bg-white text-slate-600 hover:bg-slate-50"
            }`}
          >
            .*
          </button>

          <button
            type="button"
            onClick={onToggleCaseSensitive}
            aria-pressed={caseSensitive}
            title={t("log.toggleCaseTitle")}
            className={`h-8 rounded-md border px-2 font-mono text-xs font-semibold transition-colors ${
              caseSensitive
                ? "border-indigo-300 bg-indigo-50 text-indigo-700"
                : "border-slate-300 bg-white text-slate-600 hover:bg-slate-50"
            }`}
          >
            Aa
          </button>

          {normalizedSessionId && (
            <button
              type="button"
              onClick={onToggleFilterToSession}
              aria-pressed={filterToSession}
              title={t("log.sessionScopeTitle", {
                label: resolvedSessionLabel,
              })}
              className={`inline-flex h-8 items-center gap-1 rounded-md border px-2 text-xs font-medium transition-colors ${
                filterToSession
                  ? "border-emerald-400 bg-emerald-50 text-emerald-800"
                  : "border-slate-300 bg-white text-slate-600 hover:bg-slate-50"
              }`}
            >
              <Filter className="h-3 w-3" />
              <span>{t("log.sessionScopeToggle")}</span>
            </button>
          )}

          <button
            type="submit"
            disabled={isSearching || !searchPattern.trim() || hasGcError}
            className="inline-flex h-8 items-center gap-1 rounded-md bg-indigo-600 px-3 text-xs font-medium text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {isSearching ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Search className="h-3.5 w-3.5" />
            )}
            <span>{t("log.searchButton")}</span>
          </button>

          {searchResult && searchResult.log_id === resolvedActiveLogId && (
            <div className="flex items-center gap-1 text-xs">
              <span className="rounded bg-slate-100 px-2 py-1 font-mono text-[11px] text-slate-700">
                {searchResult.matches.length > 0
                  ? `${activeMatchIdx + 1} / ${searchResult.matches.length}`
                  : t("log.zeroMatches")}{" "}
                <span className="text-slate-400">
                  ({searchResult.elapsed_ms.toFixed(1)} ms)
                </span>
              </span>
              <button
                type="button"
                onClick={() => onGoToMatch(-1)}
                disabled={searchResult.matches.length === 0}
                aria-label={t("log.prevMatchAria")}
                className="inline-flex h-7 w-7 items-center justify-center rounded border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 disabled:opacity-40"
              >
                <ChevronUp className="h-3.5 w-3.5" />
              </button>
              <button
                type="button"
                onClick={() => onGoToMatch(1)}
                disabled={searchResult.matches.length === 0}
                aria-label={t("log.nextMatchAria")}
                className="inline-flex h-7 w-7 items-center justify-center rounded border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 disabled:opacity-40"
              >
                <ChevronDown className="h-3.5 w-3.5" />
              </button>
            </div>
          )}
        </form>

        {/* Timestamp Jump Controls */}
        <div className="flex flex-wrap items-center gap-1.5">
          {crashTime && (
            <button
              type="button"
              disabled={aroundTimeLoading || hasGcError}
              onClick={onJumpToCrashTime}
              className="inline-flex h-8 items-center gap-1 rounded-md border border-rose-200 bg-rose-50 px-2.5 text-xs font-medium text-rose-700 hover:bg-rose-100 disabled:opacity-50"
              title={t("log.jumpCrashTimeTitle", { time: crashTime })}
            >
              <Clock className="h-3.5 w-3.5" />
              <span>{t("log.crashTimeBtn")}</span>
            </button>
          )}

          <input
            type="text"
            value={timestampInput}
            onChange={(e) => onTimestampInputChange(e.target.value)}
            placeholder={t("log.timestampPlaceholder")}
            aria-label={t("log.timestampAria")}
            disabled={hasGcError}
            className="h-8 w-44 rounded-md border border-slate-300 bg-white px-2 font-mono text-xs text-slate-800 placeholder:font-sans placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:bg-slate-100"
          />

          <select
            aria-label={t("log.windowSecAria")}
            value={windowSec}
            onChange={(e) => onWindowSecChange(Number(e.target.value))}
            disabled={hasGcError}
            className="h-8 rounded-md border border-slate-300 bg-white px-2 text-xs text-slate-700 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
          >
            <option value={5}>±5s</option>
            <option value={10}>±10s</option>
            <option value={30}>±30s</option>
            <option value={60}>±60s</option>
          </select>

          <button
            type="button"
            disabled={
              aroundTimeLoading || !timestampInput.trim() || hasGcError
            }
            onClick={onJumpAroundTime}
            className="inline-flex h-8 items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {aroundTimeLoading ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Clock className="h-3.5 w-3.5" />
            )}
            <span>{t("log.jumpBtn")}</span>
          </button>
        </div>
      </div>

      {/* Status / Warning Strips */}
      {searchError && (
        <div
          className="flex items-center justify-between gap-2 rounded-md border border-rose-200 bg-rose-50 px-3 py-1.5 text-xs text-rose-800"
          role="alert"
        >
          <span className="flex items-center gap-1.5">
            <AlertCircle className="h-3.5 w-3.5 shrink-0" />
            <span>{searchError}</span>
          </span>
          <button
            type="button"
            onClick={onDismissSearchError}
            className="text-rose-600 hover:text-rose-800"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      {searchResult &&
        searchResult.log_id === resolvedActiveLogId &&
        searchResult.truncated && (
          <div className="flex items-center gap-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-1.5 text-xs text-amber-900">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-amber-600" />
            <span>
              {t("log.truncatedWarning", {
                count: searchResult.matches.length,
              })}
            </span>
          </div>
        )}

      {aroundTimeError && (
        <div
          className="flex items-center justify-between gap-2 rounded-md border border-rose-200 bg-rose-50 px-3 py-1.5 text-xs text-rose-800"
          role="alert"
        >
          <span>{aroundTimeError}</span>
          <button
            type="button"
            onClick={onDismissAroundTimeError}
            className="text-rose-600 hover:text-rose-800"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      {aroundTimeInfo &&
        aroundTimeInfo.logId === resolvedActiveLogId &&
        !aroundTimeError && (
          <div className="flex items-center justify-between gap-2 rounded-md border border-indigo-200 bg-indigo-50 px-3 py-1.5 text-xs text-indigo-800">
            <span>{aroundTimeInfo.text}</span>
            <button
              type="button"
              onClick={onDismissAroundTimeInfo}
              className="text-indigo-600 hover:text-indigo-800"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        )}
    </div>
  );
}
