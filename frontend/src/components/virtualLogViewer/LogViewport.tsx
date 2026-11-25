import type { RefObject } from "react";
import type { VirtualItem } from "@tanstack/react-virtual";
import { Archive, Crosshair, RefreshCw } from "lucide-react";
import type { LogLine } from "../../api/types";
import { useAppI18n } from "../../i18n";
import {
  ROW_HEIGHT,
  type AroundTimeRange,
  type LogGcError,
} from "./types";
import {
  getLineTextColorClass,
  lineContainsSession,
  renderLineWithSessionHighlight,
} from "./utils";

export interface LogViewportProps {
  scrollParentRef: RefObject<HTMLDivElement | null>;
  totalSize: number;
  virtualItems: VirtualItem[];
  windowStart: number;
  currentLogLinesMap: Map<number, LogLine> | undefined;
  activeGcError: LogGcError | null;
  activePageError: string | null;
  effectiveTotalLines: number;
  sessionOnlyMode: boolean;
  resolvedSessionLabel: string;
  normalizedSessionId: string | null;
  resolvedActiveLogId: string;
  normalizedHighlight: { start: number; end: number } | null;
  aroundTimeRange: AroundTimeRange | null;
  activeMatchLine: number | null;
  matchedLineSet: Set<number>;
  onExitSessionOnly: () => void;
  onRetryFetch: () => void;
  onJumpToLine: (lineNumber: number) => void;
}

export function LogViewport({
  scrollParentRef,
  totalSize,
  virtualItems,
  windowStart,
  currentLogLinesMap,
  activeGcError,
  activePageError,
  effectiveTotalLines,
  sessionOnlyMode,
  resolvedSessionLabel,
  normalizedSessionId,
  resolvedActiveLogId,
  normalizedHighlight,
  aroundTimeRange,
  activeMatchLine,
  matchedLineSet,
  onExitSessionOnly,
  onRetryFetch,
  onJumpToLine,
}: LogViewportProps) {
  const { t, formatNumber } = useAppI18n();

  return (
    <div className="relative min-w-0 flex-1 bg-slate-950 text-slate-100">
      {activeGcError ? (
        <div className="flex h-[520px] flex-col items-center justify-center gap-3 px-6 text-center">
          <div className="flex h-11 w-11 items-center justify-center rounded-full bg-amber-500/15 text-amber-400">
            <Archive className="h-6 w-6" />
          </div>
          <div className="max-w-md space-y-1">
            <h3 className="text-sm font-semibold text-white">
              {t("log.gcUnavailableTitle", { status: activeGcError.status })}
            </h3>
            <p className="text-xs text-slate-400">
              {t("log.gcUnavailableDesc")}
            </p>
          </div>
        </div>
      ) : effectiveTotalLines === 0 ? (
        <div className="flex h-[320px] items-center justify-center px-6 text-center text-xs text-slate-400">
          {sessionOnlyMode
            ? t("log.sessionOnlyEmpty", { label: resolvedSessionLabel })
            : t("log.emptyLogFile")}
        </div>
      ) : (
        <>
          {sessionOnlyMode && (
            <div className="flex items-center justify-between gap-2 border-b border-indigo-500/30 bg-indigo-950/70 px-3 py-1.5 text-xs text-indigo-200">
              <span>
                {t("log.sessionOnlyBanner", {
                  total: formatNumber(effectiveTotalLines),
                })}
              </span>
              <button
                type="button"
                onClick={onExitSessionOnly}
                className="shrink-0 rounded border border-indigo-400/50 bg-indigo-900/60 px-2 py-0.5 text-[11px] font-medium text-white hover:bg-indigo-800"
              >
                {t("log.sessionOnlyExitBtn")}
              </button>
            </div>
          )}

          {activePageError && (
            <div className="flex items-center justify-between gap-2 border-b border-rose-500/30 bg-rose-950/80 px-3 py-1.5 text-xs text-rose-200">
              <span>
                {t("log.chunkErrorPrefix", { error: activePageError })}
              </span>
              <button
                type="button"
                onClick={onRetryFetch}
                className="inline-flex items-center gap-1 rounded border border-rose-400/40 bg-rose-900/60 px-2 py-0.5 text-[11px] font-medium text-white hover:bg-rose-800"
              >
                <RefreshCw className="h-3 w-3" />
                {t("log.retry")}
              </button>
            </div>
          )}

          <div
            ref={scrollParentRef}
            className="ck-scrollbar h-[520px] overflow-auto font-mono text-xs leading-[22px]"
          >
            <div
              className="relative w-full"
              style={{ height: `${totalSize}px` }}
            >
              {virtualItems.map((virtualRow) => {
                const rowIdx = windowStart + virtualRow.index + 1;
                const cachedLine = currentLogLinesMap?.get(rowIdx);
                const displayLineNumber = sessionOnlyMode
                  ? (cachedLine?.line_number ?? null)
                  : rowIdx;

                const isHighlighted =
                  !sessionOnlyMode &&
                  normalizedHighlight !== null &&
                  rowIdx >= normalizedHighlight.start &&
                  rowIdx <= normalizedHighlight.end;

                const isAroundTimeHighlight =
                  !sessionOnlyMode &&
                  !isHighlighted &&
                  aroundTimeRange !== null &&
                  aroundTimeRange.logId === resolvedActiveLogId &&
                  rowIdx >= aroundTimeRange.startLine &&
                  rowIdx <= aroundTimeRange.endLine;

                const isActiveMatch =
                  !sessionOnlyMode && activeMatchLine === rowIdx;
                const isMatched =
                  !sessionOnlyMode && matchedLineSet.has(rowIdx);
                const isSessionLine =
                  !sessionOnlyMode &&
                  Boolean(
                    cachedLine &&
                      lineContainsSession(cachedLine.text, normalizedSessionId),
                  );

                let rowBgClass = "hover:bg-slate-900/80";
                if (isActiveMatch) {
                  rowBgClass =
                    "bg-amber-500/25 ring-1 ring-inset ring-amber-400/70";
                } else if (isHighlighted) {
                  rowBgClass =
                    "bg-indigo-500/20 border-l-2 border-l-indigo-400";
                } else if (isMatched) {
                  rowBgClass = "bg-amber-500/10";
                } else if (isAroundTimeHighlight) {
                  rowBgClass = "bg-sky-500/10";
                } else if (isSessionLine) {
                  rowBgClass =
                    "bg-emerald-500/[0.06] border-l-2 border-l-emerald-500/70 hover:bg-emerald-500/15";
                }

                return (
                  <div
                    key={virtualRow.key}
                    data-index={rowIdx - 1}
                    data-line-number={displayLineNumber ?? undefined}
                    data-highlighted={isHighlighted ? "true" : undefined}
                    className={`absolute left-0 top-0 flex min-w-full items-center ${rowBgClass}`}
                    style={{
                      height: `${ROW_HEIGHT}px`,
                      transform: `translateY(${virtualRow.start}px)`,
                    }}
                  >
                    <span className="flex w-24 shrink-0 select-none items-center justify-end gap-1 border-r border-slate-800 pr-2.5 text-right text-[11px] text-slate-500">
                      {sessionOnlyMode && displayLineNumber != null && (
                        <button
                          type="button"
                          onClick={() => onJumpToLine(displayLineNumber)}
                          title={t("log.revealInFullLogTitle", {
                            line: displayLineNumber,
                          })}
                          className="rounded p-0.5 text-emerald-400 hover:bg-slate-800 hover:text-emerald-200"
                        >
                          <Crosshair className="h-3 w-3" />
                        </button>
                      )}
                      <span>
                        {displayLineNumber != null ? displayLineNumber : "…"}
                      </span>
                    </span>
                    {cachedLine ? (
                      <span
                        className={`whitespace-pre px-3 ${getLineTextColorClass(
                          cachedLine,
                        )}`}
                      >
                        {renderLineWithSessionHighlight(
                          cachedLine.text,
                          normalizedSessionId,
                        )}
                      </span>
                    ) : (
                      <span className="px-3">
                        <span className="inline-block h-2.5 w-48 animate-pulse rounded bg-slate-800 align-middle" />
                      </span>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
