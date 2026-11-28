import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { FileText } from "lucide-react";
import type { CrashLogRead } from "../api/types";
import { useAppI18n } from "../i18n";
import { LogDigestSidebar } from "./virtualLogViewer/LogDigestSidebar";
import { LogTabsHeader } from "./virtualLogViewer/LogTabsHeader";
import { LogToolbar } from "./virtualLogViewer/LogToolbar";
import { LogViewport } from "./virtualLogViewer/LogViewport";
import { SessionContextBar } from "./virtualLogViewer/SessionContextBar";
import {
  OVERSCAN,
  PAGE_SIZE,
  ROW_HEIGHT,
  WINDOW_LINES,
  type LogHighlightRange,
  type LogScrollTarget,
  type SidebarTab,
} from "./virtualLogViewer/types";
import { useLogPageCache } from "./virtualLogViewer/useLogPageCache";
import { useLogSearchAndTime } from "./virtualLogViewer/useLogSearchAndTime";
import { WindowNavigator } from "./virtualLogViewer/WindowNavigator";

export type { LogHighlightRange };

export interface VirtualLogViewerProps {
  logs: CrashLogRead[];
  activeLogId?: string;
  onSelectLog?: (logId: string) => void;
  crashTime?: string | null;
  sessionId?: string | null;
  sessionLabel?: string;
  highlightRange?: LogHighlightRange | null;
}

export function VirtualLogViewer({
  logs,
  activeLogId,
  onSelectLog,
  crashTime,
  sessionId,
  sessionLabel,
  highlightRange,
}: VirtualLogViewerProps) {
  const { t } = useAppI18n();
  const normalizedSessionId = sessionId?.trim() || null;
  const resolvedSessionLabel = sessionLabel || t("common.sessionId");
  const [internalLogId, setInternalLogId] = useState<string>(
    () => logs[0]?.id ?? "",
  );
  const [sessionOnlyMode, setSessionOnlyMode] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [sidebarTab, setSidebarTab] = useState<SidebarTab>("errors");
  const [scrollTarget, setScrollTarget] = useState<LogScrollTarget | null>(
    null,
  );

  const autoAnchoredLogsRef = useRef<Set<string>>(new Set());
  const scrollParentRef = useRef<HTMLDivElement | null>(null);
  const lastHighlightKeyRef = useRef<string | null>(null);

  const resolvedActiveLogId = useMemo(() => {
    if (activeLogId && logs.some((l) => l.id === activeLogId)) {
      return activeLogId;
    }
    if (internalLogId && logs.some((l) => l.id === internalLogId)) {
      return internalLogId;
    }
    return logs[0]?.id ?? "";
  }, [activeLogId, internalLogId, logs]);

  const activeLog = useMemo(
    () => logs.find((l) => l.id === resolvedActiveLogId) ?? null,
    [logs, resolvedActiveLogId],
  );

  const {
    activeDigest,
    activeSessionDigest,
    activeGcError,
    activePageError,
    sessionTotalByLog,
    retryTick,
    visiblePageRangeRef,
    seedLines,
    fetchPage,
    fetchSessionPage,
    markGcError,
    handleRetryFetch,
    getCurrentLogLinesMap,
  } = useLogPageCache({
    activeLog,
    resolvedActiveLogId,
    normalizedSessionId,
  });

  const selectLog = useCallback(
    (logId: string) => {
      if (logId !== resolvedActiveLogId) {
        setScrollTarget(null);
      }
      setInternalLogId(logId);
      onSelectLog?.(logId);
    },
    [onSelectLog, resolvedActiveLogId],
  );

  const isActiveLogIndexing = Boolean(activeLog?.is_indexing);
  const totalLines = activeLog?.total_lines ?? 0;
  const sessionTotalLines = activeLog
    ? (sessionTotalByLog[activeLog.id] ?? activeSessionDigest?.match_count ?? 0)
    : 0;
  const effectiveTotalLines = sessionOnlyMode ? sessionTotalLines : totalLines;
  const activeMode: "full" | "session" = sessionOnlyMode ? "session" : "full";
  const activeTarget =
    scrollTarget?.logId === resolvedActiveLogId &&
    scrollTarget.mode === activeMode
      ? scrollTarget
      : null;
  const targetIndex = Math.max(
    0,
    Math.min(effectiveTotalLines, activeTarget?.lineNumber ?? 1) - 1,
  );
  const windowStart = Math.floor(targetIndex / WINDOW_LINES) * WINDOW_LINES;
  const windowEnd = Math.min(effectiveTotalLines, windowStart + WINDOW_LINES);
  const windowCount = windowEnd - windowStart;

  const virtualizer = useVirtualizer({
    count: windowCount,
    getScrollElement: () => scrollParentRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: OVERSCAN,
    getItemKey: (index) =>
      `${resolvedActiveLogId}:${activeMode}:${windowStart + index}`,
    useFlushSync: false,
    enabled: Boolean(
      activeLog &&
        !isActiveLogIndexing &&
        effectiveTotalLines > 0 &&
        !activeGcError,
    ),
  });

  useEffect(() => {
    if (windowCount === 0 || activeGcError) {
      return;
    }
    const frameId = window.requestAnimationFrame(() => {
      virtualizer.scrollToIndex(targetIndex - windowStart, {
        align: activeTarget?.align ?? "start",
        behavior: "auto",
      });
    });
    return () => window.cancelAnimationFrame(frameId);
  }, [
    activeGcError,
    activeMode,
    activeTarget,
    resolvedActiveLogId,
    targetIndex,
    virtualizer,
    windowCount,
    windowStart,
  ]);

  // Auto-anchor to mid-file session last_line and pick session sidebar tab on first open
  useEffect(() => {
    if (!activeLog || !normalizedSessionId || !activeSessionDigest) {
      return;
    }
    if (autoAnchoredLogsRef.current.has(activeLog.id)) {
      return;
    }
    autoAnchoredLogsRef.current.add(activeLog.id);

    if ((activeSessionDigest.recent_errors?.length ?? 0) > 0) {
      setSidebarTab("session_errors");
    } else if ((activeSessionDigest.tail_lines?.length ?? 0) > 0) {
      setSidebarTab("session_tail");
    }

    if (
      !highlightRange &&
      typeof activeSessionDigest.last_line === "number" &&
      activeSessionDigest.last_line >= 1
    ) {
      setScrollTarget({
        logId: activeLog.id,
        mode: "full",
        lineNumber: activeSessionDigest.last_line,
        align: "center",
      });
    }
  }, [activeLog, activeSessionDigest, highlightRange, normalizedSessionId]);

  const virtualItems = virtualizer.getVirtualItems();
  const firstVisibleIdx = virtualItems[0]?.index ?? -1;
  const lastVisibleIdx =
    virtualItems.length > 0 ? virtualItems[virtualItems.length - 1].index : -1;

  // Bootstrap first session page when entering session-only mode for a log without precomputed total
  useEffect(() => {
    if (
      !sessionOnlyMode ||
      !activeLog ||
      !normalizedSessionId ||
      activeGcError
    ) {
      return;
    }
    if (
      sessionTotalByLog[activeLog.id] === undefined &&
      activeSessionDigest == null
    ) {
      void fetchSessionPage(activeLog.id, 0);
    }
  }, [
    activeGcError,
    activeLog,
    activeSessionDigest,
    fetchSessionPage,
    normalizedSessionId,
    sessionOnlyMode,
    sessionTotalByLog,
  ]);

  // Prefetch visible + adjacent pages
  useEffect(() => {
    if (
      !activeLog ||
      effectiveTotalLines <= 0 ||
      activeGcError ||
      firstVisibleIdx < 0 ||
      lastVisibleIdx < 0
    ) {
      return;
    }

    const startPage = Math.floor((windowStart + firstVisibleIdx) / PAGE_SIZE);
    const endPage = Math.floor((windowStart + lastVisibleIdx) / PAGE_SIZE);
    const maxPage = Math.floor((effectiveTotalLines - 1) / PAGE_SIZE);
    const prefetchStart = Math.max(0, startPage - 1);
    const prefetchEnd = Math.min(maxPage, endPage + 1);

    visiblePageRangeRef.current = { start: prefetchStart, end: prefetchEnd };

    for (let p = prefetchStart; p <= prefetchEnd; p += 1) {
      if (sessionOnlyMode) {
        void fetchSessionPage(activeLog.id, p);
      } else {
        void fetchPage(activeLog.id, p, totalLines);
      }
    }
  }, [
    activeLog,
    effectiveTotalLines,
    totalLines,
    sessionOnlyMode,
    activeGcError,
    firstVisibleIdx,
    lastVisibleIdx,
    windowStart,
    fetchPage,
    fetchSessionPage,
    retryTick,
    visiblePageRangeRef,
  ]);

  const jumpToLine = useCallback(
    (lineNumber: number, align: LogScrollTarget["align"] = "center") => {
      if (!activeLog || activeLog.total_lines <= 0 || activeGcError) {
        return;
      }
      setSessionOnlyMode(false);
      setScrollTarget({ logId: activeLog.id, mode: "full", lineNumber, align });
    },
    [activeLog, activeGcError],
  );

  const jumpToSessionIndex = useCallback(
    (sessionIdx: number, align: LogScrollTarget["align"] = "start") => {
      if (!activeLog || sessionTotalLines <= 0 || activeGcError) {
        return;
      }
      setScrollTarget({
        logId: activeLog.id,
        mode: "session",
        lineNumber: sessionIdx,
        align,
      });
    },
    [activeLog, activeGcError, sessionTotalLines],
  );

  const highlightKey = highlightRange
    ? `${highlightRange.logId}:${highlightRange.startLine}:${highlightRange.endLine}:${highlightRange.nonce ?? 0}`
    : null;

  useEffect(() => {
    if (!highlightRange || !highlightKey) {
      lastHighlightKeyRef.current = null;
      return;
    }
    if (lastHighlightKeyRef.current === highlightKey) {
      return;
    }
    if (!logs.some((l) => l.id === highlightRange.logId)) {
      return;
    }
    lastHighlightKeyRef.current = highlightKey;
    setSessionOnlyMode(false);

    if (resolvedActiveLogId !== highlightRange.logId) {
      selectLog(highlightRange.logId);
    }
    setScrollTarget({
      logId: highlightRange.logId,
      mode: "full",
      lineNumber: Math.min(highlightRange.startLine, highlightRange.endLine),
      align: "center",
    });
  }, [highlightRange, highlightKey, logs, resolvedActiveLogId, selectLog]);

  const hasGcError = Boolean(activeGcError);
  const searchAndTime = useLogSearchAndTime({
    activeLog,
    resolvedActiveLogId,
    crashTime,
    normalizedSessionId,
    hasGcError,
    seedLines,
    markGcError,
    jumpToLine,
  });

  const normalizedHighlight = useMemo(() => {
    if (!highlightRange || highlightRange.logId !== resolvedActiveLogId) {
      return null;
    }
    const start = Math.max(
      1,
      Math.min(highlightRange.startLine, highlightRange.endLine),
    );
    const end = Math.max(
      start,
      Math.max(highlightRange.startLine, highlightRange.endLine),
    );
    return { start, end };
  }, [highlightRange, resolvedActiveLogId]);

  if (logs.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-slate-300 bg-white px-6 py-12 text-center shadow-sm">
        <FileText className="mx-auto h-7 w-7 text-slate-400" />
        <p className="mt-2 text-sm font-medium text-slate-700">
          {t("log.noLogsTitle")}
        </p>
        <p className="mt-1 text-xs text-slate-500">
          {t("log.noLogsHintBefore")}
          <span className="font-mono">.log</span>
          {t("log.noLogsHintAfter")}
        </p>
      </div>
    );
  }

  const currentLogLinesMap = getCurrentLogLinesMap(sessionOnlyMode);

  return (
    <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
      <LogTabsHeader
        logs={logs}
        resolvedActiveLogId={resolvedActiveLogId}
        sidebarOpen={sidebarOpen}
        onSelectLog={selectLog}
        onToggleSidebar={() => setSidebarOpen((prev) => !prev)}
      />

      {normalizedSessionId && (
        <SessionContextBar
          normalizedSessionId={normalizedSessionId}
          resolvedSessionLabel={resolvedSessionLabel}
          activeSessionDigest={activeSessionDigest}
          hasGcError={hasGcError}
          aroundTimeLoading={searchAndTime.aroundTimeLoading}
          isSearching={searchAndTime.isSearching}
          sessionOnlyMode={sessionOnlyMode}
          onJumpToLine={(line) => jumpToLine(line)}
          onJumpSessionLastTs={searchAndTime.jumpSessionLastTs}
          onSearchSessionId={searchAndTime.searchSessionId}
          onToggleSessionOnlyMode={() => {
            setSessionOnlyMode((prev) => !prev);
            setScrollTarget(null);
          }}
        />
      )}

      {effectiveTotalLines > WINDOW_LINES && !activeGcError && (
        <WindowNavigator
          resolvedActiveLogId={resolvedActiveLogId}
          sessionOnlyMode={sessionOnlyMode}
          windowStart={windowStart}
          windowEnd={windowEnd}
          effectiveTotalLines={effectiveTotalLines}
          totalLines={totalLines}
          onJumpToLine={jumpToLine}
          onJumpToSessionIndex={jumpToSessionIndex}
        />
      )}

      <LogToolbar
        resolvedActiveLogId={resolvedActiveLogId}
        hasGcError={hasGcError}
        normalizedSessionId={normalizedSessionId}
        resolvedSessionLabel={resolvedSessionLabel}
        crashTime={crashTime}
        searchPattern={searchAndTime.searchPattern}
        isRegex={searchAndTime.isRegex}
        caseSensitive={searchAndTime.caseSensitive}
        filterToSession={searchAndTime.filterToSession}
        isSearching={searchAndTime.isSearching}
        searchResult={searchAndTime.searchResult}
        activeMatchIdx={searchAndTime.activeMatchIdx}
        searchError={searchAndTime.searchError}
        timestampInput={searchAndTime.timestampInput}
        windowSec={searchAndTime.windowSec}
        aroundTimeLoading={searchAndTime.aroundTimeLoading}
        aroundTimeInfo={searchAndTime.aroundTimeInfo}
        aroundTimeError={searchAndTime.aroundTimeError}
        onSearchPatternChange={searchAndTime.setSearchPattern}
        onClearSearch={searchAndTime.clearSearch}
        onToggleRegex={() => searchAndTime.setIsRegex((prev) => !prev)}
        onToggleCaseSensitive={() =>
          searchAndTime.setCaseSensitive((prev) => !prev)
        }
        onToggleFilterToSession={() =>
          searchAndTime.setFilterToSession((prev) => !prev)
        }
        onSearchSubmit={(e) => {
          void searchAndTime.handleSearchSubmit(e);
        }}
        onGoToMatch={searchAndTime.goToMatch}
        onDismissSearchError={() => searchAndTime.setSearchError(null)}
        onJumpToCrashTime={searchAndTime.jumpToCrashTime}
        onTimestampInputChange={(value) => {
          searchAndTime.setTimestampInput(value);
          searchAndTime.setTimestampDirty(true);
        }}
        onWindowSecChange={searchAndTime.setWindowSec}
        onJumpAroundTime={() => {
          void searchAndTime.handleJumpAroundTime(searchAndTime.timestampInput);
        }}
        onDismissAroundTimeError={() => searchAndTime.setAroundTimeError(null)}
        onDismissAroundTimeInfo={searchAndTime.dismissAroundTimeInfo}
      />

      <div className="flex flex-col lg:flex-row">
        <LogViewport
          scrollParentRef={scrollParentRef}
          totalSize={virtualizer.getTotalSize()}
          virtualItems={virtualItems}
          windowStart={windowStart}
          currentLogLinesMap={currentLogLinesMap}
          isIndexing={isActiveLogIndexing}
          activeGcError={activeGcError}
          activePageError={activePageError}
          effectiveTotalLines={effectiveTotalLines}
          sessionOnlyMode={sessionOnlyMode}
          resolvedSessionLabel={resolvedSessionLabel}
          normalizedSessionId={normalizedSessionId}
          resolvedActiveLogId={resolvedActiveLogId}
          normalizedHighlight={normalizedHighlight}
          aroundTimeRange={searchAndTime.aroundTimeRange}
          activeMatchLine={searchAndTime.activeMatchLine}
          matchedLineSet={searchAndTime.matchedLineSet}
          onExitSessionOnly={() => {
            setSessionOnlyMode(false);
            setScrollTarget(null);
          }}
          onRetryFetch={handleRetryFetch}
          onJumpToLine={(line) => jumpToLine(line)}
        />

        {sidebarOpen && (
          <LogDigestSidebar
            activeDigest={activeDigest}
            activeSessionDigest={activeSessionDigest}
            sidebarTab={sidebarTab}
            hasGcError={hasGcError}
            onSelectTab={setSidebarTab}
            onJumpToLine={(line) => jumpToLine(line)}
          />
        )}
      </div>
    </div>
  );
}

export default VirtualLogViewer;
