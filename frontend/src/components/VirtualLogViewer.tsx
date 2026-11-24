import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  AlertCircle,
  AlertTriangle,
  Archive,
  ChevronDown,
  ChevronUp,
  Clock,
  Crosshair,
  FileText,
  Filter,
  Loader2,
  PanelRightClose,
  PanelRightOpen,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import {
  ApiError,
  getLogAroundTime,
  getLogLines,
  getLogSessionLines,
  getLogSummary,
  searchLog,
} from "../api/client";
import type {
  CrashLogRead,
  LogDigest,
  LogDigestErrorEntry,
  LogLine,
  LogSearchResponse,
} from "../api/types";
import { useAppI18n } from "../i18n";

export interface LogHighlightRange {
  logId: string;
  startLine: number;
  endLine: number;
  nonce?: number;
}

export interface VirtualLogViewerProps {
  logs: CrashLogRead[];
  activeLogId?: string;
  onSelectLog?: (logId: string) => void;
  crashTime?: string | null;
  sessionId?: string | null;
  sessionLabel?: string;
  highlightRange?: LogHighlightRange | null;
}

const PAGE_SIZE = 200;
// Bound the scroll height and virtualizer bookkeeping, even for very large logs.
const WINDOW_LINES = 20_000;
const ROW_HEIGHT = 22;
const OVERSCAN = 20;
const MAX_CACHED_PAGES_PER_LOG = 100;
const SEARCH_MAX_MATCHES = 200;
const AROUND_TIME_MAX_LINES = 500;

interface LogScrollTarget {
  logId: string;
  mode: "full" | "session";
  lineNumber: number;
  align: "start" | "center";
}

function lineContainsSession(
  text: string,
  sessionId: string | null | undefined,
): boolean {
  if (!sessionId || !sessionId.trim()) {
    return false;
  }
  return text.toLowerCase().includes(sessionId.trim().toLowerCase());
}

function renderLineWithSessionHighlight(
  text: string,
  sessionId: string | null | undefined,
): ReactNode {
  const needle = sessionId?.trim();
  if (!needle) {
    return text;
  }
  const lowerText = text.toLowerCase();
  const lowerNeedle = needle.toLowerCase();
  const needleLen = needle.length;
  let pos = 0;
  let matchIdx = lowerText.indexOf(lowerNeedle, pos);
  if (matchIdx === -1) {
    return text;
  }
  const parts: ReactNode[] = [];
  let keyIdx = 0;
  while (matchIdx !== -1) {
    if (matchIdx > pos) {
      parts.push(text.slice(pos, matchIdx));
    }
    parts.push(
      <mark
        key={`sid-${keyIdx++}`}
        className="rounded bg-emerald-500/30 px-0.5 text-emerald-200 ring-1 ring-emerald-400/40"
      >
        {text.slice(matchIdx, matchIdx + needleLen)}
      </mark>,
    );
    pos = matchIdx + needleLen;
    matchIdx = lowerText.indexOf(lowerNeedle, pos);
  }
  if (pos < text.length) {
    parts.push(text.slice(pos));
  }
  return parts;
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) {
    return "-";
  }
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function getDigestEntryLine(entry: LogDigestErrorEntry): number | null {
  if (typeof entry.line_number === "number" && entry.line_number >= 1) {
    return entry.line_number;
  }
  if (typeof entry.line === "number" && entry.line >= 1) {
    return entry.line;
  }
  return null;
}

function getLineTextColorClass(line: LogLine): string {
  const level = (line.level ?? "").toUpperCase();
  if (level === "FATAL" || level === "ASSERT" || level === "ERROR") {
    return "text-rose-300";
  }
  if (level === "WARN" || level === "WARNING") {
    return "text-amber-300";
  }
  if (level === "DEBUG" || level === "TRACE") {
    return "text-slate-400";
  }
  return "text-slate-200";
}

function getDigestLevelBadgeClass(level: string | null | undefined): string {
  const upper = (level ?? "ERROR").toUpperCase();
  if (upper === "FATAL" || upper === "ASSERT") {
    return "bg-rose-500/20 text-rose-300 border-rose-500/30";
  }
  if (upper === "WARN" || upper === "WARNING") {
    return "bg-amber-500/20 text-amber-300 border-amber-500/30";
  }
  return "bg-red-500/20 text-red-300 border-red-500/30";
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
  const { t, formatNumber } = useAppI18n();
  const normalizedSessionId = sessionId?.trim() || null;
  const resolvedSessionLabel = sessionLabel || t("common.sessionId");
  const [internalLogId, setInternalLogId] = useState<string>(
    () => logs[0]?.id ?? "",
  );
  const [cacheVersion, setCacheVersion] = useState(0);
  const [gcErrorByLog, setGcErrorByLog] = useState<
    Record<string, { status: number; message: string }>
  >({});
  const [pageErrorByLog, setPageErrorByLog] = useState<Record<string, string>>(
    {},
  );
  const [summaryByLog, setSummaryByLog] = useState<Record<string, LogDigest>>(
    {},
  );

  // Session mode & filter state
  const [sessionOnlyMode, setSessionOnlyMode] = useState(false);
  const [filterToSession, setFilterToSession] = useState(false);
  const [sessionTotalByLog, setSessionTotalByLog] = useState<
    Record<string, number>
  >({});

  // Sidebar state
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [sidebarTab, setSidebarTab] = useState<
    "session_errors" | "session_tail" | "errors" | "tail"
  >("errors");

  // Timestamp jump state
  const [timestampInput, setTimestampInput] = useState<string>(
    () => crashTime ?? "",
  );
  const [timestampDirty, setTimestampDirty] = useState(false);
  const [windowSec, setWindowSec] = useState<number>(10);
  const [aroundTimeLoading, setAroundTimeLoading] = useState(false);
  const [aroundTimeInfo, setAroundTimeInfo] = useState<{
    logId: string;
    text: string;
  } | null>(null);
  const [aroundTimeError, setAroundTimeError] = useState<string | null>(null);
  const [aroundTimeRange, setAroundTimeRange] = useState<{
    logId: string;
    startLine: number;
    endLine: number;
  } | null>(null);

  // Search state
  const [searchPattern, setSearchPattern] = useState("");
  const [isRegex, setIsRegex] = useState(false);
  const [caseSensitive, setCaseSensitive] = useState(false);
  const [isSearching, setIsSearching] = useState(false);
  const [searchResult, setSearchResult] = useState<LogSearchResponse | null>(
    null,
  );
  const [activeMatchIdx, setActiveMatchIdx] = useState<number>(-1);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [retryTick, setRetryTick] = useState(0);
  const [scrollTarget, setScrollTarget] = useState<LogScrollTarget | null>(null);

  // Caches & concurrency refs (full log + session-only log)
  const linesCacheRef = useRef<Map<string, Map<number, LogLine>>>(new Map());
  const loadedPagesRef = useRef<Map<string, Map<number, number[]>>>(new Map());
  const inFlightRef = useRef<Map<string, Promise<void>>>(new Map());
  const failedPagesRef = useRef<Map<string, string>>(new Map());

  const sessionLinesCacheRef = useRef<Map<string, Map<number, LogLine>>>(
    new Map(),
  );
  const sessionLoadedPagesRef = useRef<Map<string, Map<number, number[]>>>(
    new Map(),
  );
  const sessionInFlightRef = useRef<Map<string, Promise<void>>>(new Map());
  const sessionFailedPagesRef = useRef<Map<string, string>>(new Map());
  const autoAnchoredLogsRef = useRef<Set<string>>(new Set());

  const visiblePageRangeRef = useRef<{ start: number; end: number }>({
    start: 0,
    end: 0,
  });
  const scrollParentRef = useRef<HTMLDivElement | null>(null);
  const lastHighlightKeyRef = useRef<string | null>(null);
  const isMountedRef = useRef(true);
  const activeLogIdRef = useRef<string>("");
  const searchSeqRef = useRef(0);
  const aroundTimeSeqRef = useRef(0);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  // Sync timestamp input with crashTime prop until user edits it
  useEffect(() => {
    if (!timestampDirty) {
      setTimestampInput(crashTime ?? "");
    }
  }, [crashTime, timestampDirty]);

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

  const activeDigest: LogDigest | null = useMemo(() => {
    if (!activeLog) {
      return null;
    }
    return activeLog.digest_json ?? summaryByLog[activeLog.id] ?? null;
  }, [activeLog, summaryByLog]);

  const activeSessionDigest = activeDigest?.session ?? null;

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

  const activeGcError = resolvedActiveLogId
    ? gcErrorByLog[resolvedActiveLogId] ?? null
    : null;
  const activePageError = resolvedActiveLogId
    ? pageErrorByLog[resolvedActiveLogId] ?? null
    : null;

  const totalLines = activeLog?.total_lines ?? 0;
  const sessionTotalLines = activeLog
    ? sessionTotalByLog[activeLog.id] ?? activeSessionDigest?.match_count ?? 0
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
    enabled: Boolean(activeLog && effectiveTotalLines > 0 && !activeGcError),
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

  // Reset search / around-time ephemeral UI when switching log tabs and invalidate in-flight requests
  useEffect(() => {
    activeLogIdRef.current = resolvedActiveLogId;
    searchSeqRef.current += 1;
    aroundTimeSeqRef.current += 1;
    setSearchResult(null);
    setActiveMatchIdx(-1);
    setSearchError(null);
    setIsSearching(false);
    setAroundTimeInfo(null);
    setAroundTimeError(null);
    setAroundTimeLoading(false);
  }, [resolvedActiveLogId]);

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

  // Seed partial lines (from around-time or digest) without marking a full page
  const seedLines = useCallback((logId: string, lines: LogLine[]) => {
    if (lines.length === 0) {
      return;
    }
    let logLinesMap = linesCacheRef.current.get(logId);
    if (!logLinesMap) {
      logLinesMap = new Map<number, LogLine>();
      linesCacheRef.current.set(logId, logLinesMap);
    }
    for (const line of lines) {
      logLinesMap.set(line.line_number, line);
    }
    if (isMountedRef.current) {
      setCacheVersion((v) => v + 1);
    }
  }, []);

  const touchAndStorePage = useCallback(
    (logId: string, pageIdx: number, lines: LogLine[]) => {
      let logLinesMap = linesCacheRef.current.get(logId);
      if (!logLinesMap) {
        logLinesMap = new Map<number, LogLine>();
        linesCacheRef.current.set(logId, logLinesMap);
      }
      let pagesMap = loadedPagesRef.current.get(logId);
      if (!pagesMap) {
        pagesMap = new Map<number, number[]>();
        loadedPagesRef.current.set(logId, pagesMap);
      }

      const lineNumbers: number[] = [];
      for (const line of lines) {
        logLinesMap.set(line.line_number, line);
        lineNumbers.push(line.line_number);
      }

      // Refresh LRU insertion order
      if (pagesMap.has(pageIdx)) {
        pagesMap.delete(pageIdx);
      }
      pagesMap.set(pageIdx, lineNumbers);

      // Evict oldest pages above capacity (skipping currently visible pages)
      if (pagesMap.size > MAX_CACHED_PAGES_PER_LOG) {
        const visible = visiblePageRangeRef.current;
        for (const [candidatePage, candidateLines] of pagesMap.entries()) {
          if (pagesMap.size <= MAX_CACHED_PAGES_PER_LOG) {
            break;
          }
          if (candidatePage >= visible.start && candidatePage <= visible.end) {
            continue;
          }
          pagesMap.delete(candidatePage);
          for (const ln of candidateLines) {
            logLinesMap.delete(ln);
          }
        }
      }
    },
    [],
  );

  const fetchPage = useCallback(
    async (logId: string, pageIdx: number, maxTotalLines: number) => {
      if (!logId || maxTotalLines <= 0) {
        return;
      }
      if (gcErrorByLog[logId]) {
        return;
      }

      const key = `${logId}:${pageIdx}`;
      if (failedPagesRef.current.has(key)) {
        return;
      }

      const pagesMap = loadedPagesRef.current.get(logId);
      if (pagesMap?.has(pageIdx)) {
        // Refresh LRU recency
        const existing = pagesMap.get(pageIdx)!;
        pagesMap.delete(pageIdx);
        pagesMap.set(pageIdx, existing);
        return;
      }

      const existingInFlight = inFlightRef.current.get(key);
      if (existingInFlight) {
        return existingInFlight;
      }

      const startLine = pageIdx * PAGE_SIZE + 1;
      const endLine = Math.min(maxTotalLines, (pageIdx + 1) * PAGE_SIZE);
      if (startLine > maxTotalLines || startLine > endLine) {
        return;
      }

      const promise = (async () => {
        try {
          const res = await getLogLines(logId, startLine, endLine);
          touchAndStorePage(logId, pageIdx, res.lines);
          failedPagesRef.current.delete(key);
          if (isMountedRef.current) {
            setPageErrorByLog((prev) => {
              if (!prev[logId]) {
                return prev;
              }
              const next = { ...prev };
              delete next[logId];
              return next;
            });
            setCacheVersion((v) => v + 1);
          }
        } catch (err) {
          if (!isMountedRef.current) {
            return;
          }
          if (err instanceof ApiError && (err.status === 404 || err.status === 410)) {
            setGcErrorByLog((prev) => ({
              ...prev,
              [logId]: {
                status: err.status,
                message: err.message,
              },
            }));
          } else {
            const msg =
              err instanceof Error ? err.message : t("log.fetchLinesFailed");
            failedPagesRef.current.set(key, msg);
            setPageErrorByLog((prev) => ({
              ...prev,
              [logId]: msg,
            }));
          }
        } finally {
          inFlightRef.current.delete(key);
        }
      })();

      inFlightRef.current.set(key, promise);
      return promise;
    },
    [gcErrorByLog, t, touchAndStorePage],
  );

  const touchAndStoreSessionPage = useCallback(
    (logId: string, pageIdx: number, startIdx: number, lines: LogLine[]) => {
      let logLinesMap = sessionLinesCacheRef.current.get(logId);
      if (!logLinesMap) {
        logLinesMap = new Map<number, LogLine>();
        sessionLinesCacheRef.current.set(logId, logLinesMap);
      }
      let pagesMap = sessionLoadedPagesRef.current.get(logId);
      if (!pagesMap) {
        pagesMap = new Map<number, number[]>();
        sessionLoadedPagesRef.current.set(logId, pagesMap);
      }

      const indices: number[] = [];
      for (let i = 0; i < lines.length; i += 1) {
        const idx = startIdx + i;
        logLinesMap.set(idx, lines[i]);
        indices.push(idx);
      }

      if (pagesMap.has(pageIdx)) {
        pagesMap.delete(pageIdx);
      }
      pagesMap.set(pageIdx, indices);

      if (pagesMap.size > MAX_CACHED_PAGES_PER_LOG) {
        const visible = visiblePageRangeRef.current;
        for (const [candidatePage, candidateIndices] of pagesMap.entries()) {
          if (pagesMap.size <= MAX_CACHED_PAGES_PER_LOG) {
            break;
          }
          if (candidatePage >= visible.start && candidatePage <= visible.end) {
            continue;
          }
          pagesMap.delete(candidatePage);
          for (const idx of candidateIndices) {
            logLinesMap.delete(idx);
          }
        }
      }
    },
    [],
  );

  const fetchSessionPage = useCallback(
    async (logId: string, pageIdx: number) => {
      if (!logId || !normalizedSessionId) {
        return;
      }
      if (gcErrorByLog[logId]) {
        return;
      }
      const key = `${logId}:sess:${pageIdx}`;
      if (sessionFailedPagesRef.current.has(key)) {
        return;
      }
      const pagesMap = sessionLoadedPagesRef.current.get(logId);
      if (pagesMap?.has(pageIdx)) {
        const existing = pagesMap.get(pageIdx)!;
        pagesMap.delete(pageIdx);
        pagesMap.set(pageIdx, existing);
        return;
      }
      const existingInFlight = sessionInFlightRef.current.get(key);
      if (existingInFlight) {
        return existingInFlight;
      }

      const startIdx = pageIdx * PAGE_SIZE + 1;
      const endIdx = (pageIdx + 1) * PAGE_SIZE;

      const promise = (async () => {
        try {
          const res = await getLogSessionLines(logId, {
            session_id: normalizedSessionId,
            start: startIdx,
            end: endIdx,
          });
          if (res.lines.length > 0 && res.start_index >= 1) {
            touchAndStoreSessionPage(logId, pageIdx, res.start_index, res.lines);
          }
          sessionFailedPagesRef.current.delete(key);
          if (isMountedRef.current) {
            setSessionTotalByLog((prev) =>
              prev[logId] === res.total_matches
                ? prev
                : { ...prev, [logId]: res.total_matches },
            );
            setPageErrorByLog((prev) => {
              if (!prev[logId]) {
                return prev;
              }
              const next = { ...prev };
              delete next[logId];
              return next;
            });
            setCacheVersion((v) => v + 1);
          }
        } catch (err) {
          if (!isMountedRef.current) {
            return;
          }
          if (
            err instanceof ApiError &&
            (err.status === 404 || err.status === 410)
          ) {
            setGcErrorByLog((prev) => ({
              ...prev,
              [logId]: { status: err.status, message: err.message },
            }));
          } else {
            const msg =
              err instanceof Error ? err.message : t("log.fetchLinesFailed");
            sessionFailedPagesRef.current.set(key, msg);
            setPageErrorByLog((prev) => ({
              ...prev,
              [logId]: msg,
            }));
          }
        } finally {
          sessionInFlightRef.current.delete(key);
        }
      })();

      sessionInFlightRef.current.set(key, promise);
      return promise;
    },
    [gcErrorByLog, normalizedSessionId, t, touchAndStoreSessionPage],
  );

  // Compute virtual items and primitive bounds for prefetching
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
  ]);

  // Fallback getLogSummary if digest_json was omitted on CrashLogRead
  useEffect(() => {
    if (
      !activeLog ||
      activeLog.digest_json ||
      summaryByLog[activeLog.id] ||
      activeGcError
    ) {
      return;
    }
    let cancelled = false;
    void getLogSummary(activeLog.id)
      .then((res) => {
        if (!cancelled && isMountedRef.current && res.digest) {
          setSummaryByLog((prev) => ({
            ...prev,
            [activeLog.id]: res.digest,
          }));
        }
      })
      .catch((err: unknown) => {
        if (
          !cancelled &&
          isMountedRef.current &&
          err instanceof ApiError &&
          (err.status === 404 || err.status === 410)
        ) {
          setGcErrorByLog((prev) => ({
            ...prev,
            [activeLog.id]: { status: err.status, message: err.message },
          }));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [activeLog, summaryByLog, activeGcError]);

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

  const handleRetryFetch = () => {
    if (!activeLog) {
      return;
    }
    for (const key of failedPagesRef.current.keys()) {
      if (key.startsWith(`${activeLog.id}:`)) {
        failedPagesRef.current.delete(key);
      }
    }
    for (const key of sessionFailedPagesRef.current.keys()) {
      if (key.startsWith(`${activeLog.id}:`)) {
        sessionFailedPagesRef.current.delete(key);
      }
    }
    setGcErrorByLog((prev) => {
      if (!(activeLog.id in prev)) {
        return prev;
      }
      const next = { ...prev };
      delete next[activeLog.id];
      return next;
    });
    setPageErrorByLog((prev) => {
      if (!(activeLog.id in prev)) {
        return prev;
      }
      const next = { ...prev };
      delete next[activeLog.id];
      return next;
    });
    setRetryTick((tick) => tick + 1);
  };

  const handleJumpAroundTime = async (
    targetTs: string,
    opts?: { onlySession?: boolean },
  ) => {
    if (!activeLog || !targetTs.trim() || activeGcError) {
      return;
    }
    const requestLogId = activeLog.id;
    const seq = ++aroundTimeSeqRef.current;
    const useOnlySession =
      opts?.onlySession !== undefined
        ? opts.onlySession
        : Boolean(normalizedSessionId && filterToSession);
    setAroundTimeLoading(true);
    setAroundTimeError(null);
    setAroundTimeInfo(null);
    try {
      const res = await getLogAroundTime(requestLogId, {
        timestamp: targetTs.trim(),
        window_sec: windowSec,
        max_lines: AROUND_TIME_MAX_LINES,
        only_session: useOnlySession,
        session_id: useOnlySession ? normalizedSessionId ?? undefined : undefined,
      });
      if (
        !isMountedRef.current ||
        activeLogIdRef.current !== requestLogId ||
        seq !== aroundTimeSeqRef.current
      ) {
        return;
      }
      seedLines(requestLogId, res.lines);
      setAroundTimeRange({
        logId: requestLogId,
        startLine: res.start_line,
        endLine: res.end_line,
      });

      if (res.lines.length === 0) {
        setAroundTimeInfo({
          logId: requestLogId,
          text: t("log.noLinesInWindow"),
        });
        return;
      }

      const centerMs = Date.parse(res.center_timestamp);
      let bestLine = Math.round((res.start_line + res.end_line) / 2);
      if (!Number.isNaN(centerMs)) {
        let minDiff = Number.POSITIVE_INFINITY;
        for (const line of res.lines) {
          if (!line.timestamp) {
            continue;
          }
          const lineMs = Date.parse(line.timestamp);
          if (!Number.isNaN(lineMs)) {
            const diff = Math.abs(lineMs - centerMs);
            if (diff < minDiff) {
              minDiff = diff;
              bestLine = line.line_number;
            }
          }
        }
      }

      jumpToLine(bestLine);
      setAroundTimeInfo({
        logId: requestLogId,
        text: t("log.jumpedAroundTime", {
          line: bestLine,
          count: res.lines.length,
          sec: res.window_sec,
        }),
      });
    } catch (err) {
      if (!isMountedRef.current) {
        return;
      }
      if (err instanceof ApiError && (err.status === 404 || err.status === 410)) {
        setGcErrorByLog((prev) => ({
          ...prev,
          [requestLogId]: { status: err.status, message: err.message },
        }));
      }
      if (
        activeLogIdRef.current !== requestLogId ||
        seq !== aroundTimeSeqRef.current
      ) {
        return;
      }
      if (!(err instanceof ApiError && (err.status === 404 || err.status === 410))) {
        setAroundTimeError(
          err instanceof Error ? err.message : t("log.jumpAroundTimeFailed"),
        );
      }
    } finally {
      if (
        isMountedRef.current &&
        activeLogIdRef.current === requestLogId &&
        seq === aroundTimeSeqRef.current
      ) {
        setAroundTimeLoading(false);
      }
    }
  };

  const runLogSearch = async (
    patternToRun: string,
    opts?: { isRegex?: boolean; onlySession?: boolean },
  ) => {
    if (!activeLog || !patternToRun.trim() || activeGcError) {
      return;
    }
    const requestLogId = activeLog.id;
    const seq = ++searchSeqRef.current;
    const useRegex = opts?.isRegex ?? isRegex;
    const useOnlySession =
      opts?.onlySession !== undefined
        ? opts.onlySession
        : Boolean(normalizedSessionId && filterToSession);
    setIsSearching(true);
    setSearchError(null);
    try {
      const res = await searchLog(requestLogId, {
        pattern: patternToRun,
        is_regex: useRegex,
        case_sensitive: caseSensitive,
        max_matches: SEARCH_MAX_MATCHES,
        only_session: useOnlySession,
        session_id: useOnlySession ? normalizedSessionId ?? undefined : undefined,
      });
      if (
        !isMountedRef.current ||
        activeLogIdRef.current !== requestLogId ||
        seq !== searchSeqRef.current
      ) {
        return;
      }
      setSearchResult(res);
      if (res.matches.length > 0) {
        setActiveMatchIdx(0);
        jumpToLine(res.matches[0].line_number);
      } else {
        setActiveMatchIdx(-1);
      }
    } catch (err) {
      if (!isMountedRef.current) {
        return;
      }
      if (err instanceof ApiError && (err.status === 404 || err.status === 410)) {
        setGcErrorByLog((prev) => ({
          ...prev,
          [requestLogId]: { status: err.status, message: err.message },
        }));
      }
      if (
        activeLogIdRef.current !== requestLogId ||
        seq !== searchSeqRef.current
      ) {
        return;
      }
      setSearchResult(null);
      setActiveMatchIdx(-1);
      if (!(err instanceof ApiError && (err.status === 404 || err.status === 410))) {
        setSearchError(err instanceof Error ? err.message : t("log.searchFailed"));
      }
    } finally {
      if (
        isMountedRef.current &&
        activeLogIdRef.current === requestLogId &&
        seq === searchSeqRef.current
      ) {
        setIsSearching(false);
      }
    }
  };

  const handleSearchSubmit = async (e: FormEvent) => {
    e.preventDefault();
    await runLogSearch(searchPattern);
  };

  const goToMatch = (delta: 1 | -1) => {
    if (
      !searchResult ||
      searchResult.log_id !== resolvedActiveLogId ||
      searchResult.matches.length === 0
    ) {
      return;
    }
    const len = searchResult.matches.length;
    const base = activeMatchIdx < 0 ? 0 : activeMatchIdx;
    const nextIdx = (base + delta + len) % len;
    setActiveMatchIdx(nextIdx);
    jumpToLine(searchResult.matches[nextIdx].line_number);
  };

  const matchedLineSet = useMemo(() => {
    const set = new Set<number>();
    if (searchResult && searchResult.log_id === resolvedActiveLogId) {
      for (const m of searchResult.matches) {
        set.add(m.line_number);
      }
    }
    return set;
  }, [searchResult, resolvedActiveLogId]);

  const activeMatchLine =
    searchResult &&
    searchResult.log_id === resolvedActiveLogId &&
    activeMatchIdx >= 0
      ? searchResult.matches[activeMatchIdx]?.line_number ?? null
      : null;

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

  const recentErrors = activeDigest?.recent_errors ?? [];
  const tailLines = activeDigest?.tail_lines ?? [];
  const sessionRecentErrors = activeSessionDigest?.recent_errors ?? [];
  const sessionTailLines = activeSessionDigest?.tail_lines ?? [];
  const postSessionLines = activeSessionDigest?.post_session_lines ?? [];

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

  // Trigger dependency read on cacheVersion so cached lines map reflects latest reads
  const currentLogLinesMap =
    cacheVersion >= 0 && resolvedActiveLogId
      ? sessionOnlyMode
        ? sessionLinesCacheRef.current.get(resolvedActiveLogId)
        : linesCacheRef.current.get(resolvedActiveLogId)
      : undefined;

  return (
    <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
      {/* Multi-log Tabs Header */}
      <div className="ck-scrollbar flex items-center justify-between gap-2 overflow-x-auto border-b border-slate-200 bg-slate-50 px-3 py-2">
        <div className="flex items-center gap-1.5" role="tablist">
          {logs.map((log) => {
            const isActive = log.id === resolvedActiveLogId;
            const errCount = log.digest_json?.error_count ?? 0;
            const sessMatches = log.digest_json?.session?.match_count ?? 0;
            return (
              <button
                key={log.id}
                type="button"
                role="tab"
                aria-selected={isActive}
                onClick={() => selectLog(log.id)}
                className={`inline-flex shrink-0 items-center gap-2 rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors ${
                  isActive
                    ? "border-indigo-300 bg-white text-indigo-700 shadow-sm"
                    : "border-transparent bg-transparent text-slate-600 hover:bg-slate-200/60 hover:text-slate-900"
                }`}
              >
                <FileText className="h-3.5 w-3.5 shrink-0" />
                <span className="max-w-[180px] truncate">{log.filename}</span>
                <span className="font-mono text-[11px] text-slate-400">
                  {t("log.linesShort", { lines: formatNumber(log.total_lines) })}{" "}
                  · {formatBytes(log.raw_size)}
                </span>
                {sessMatches > 0 && (
                  <span className="rounded-full bg-emerald-100 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-emerald-700">
                    {t("log.sessionHitsShort", {
                      count: sessMatches,
                    })}
                  </span>
                )}
                {errCount > 0 && (
                  <span className="rounded-full bg-rose-100 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-rose-700">
                    {t("log.errorsShort", { count: errCount })}
                  </span>
                )}
              </button>
            );
          })}
        </div>

        <button
          type="button"
          onClick={() => setSidebarOpen((prev) => !prev)}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-slate-300 bg-white px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
          title={sidebarOpen ? t("log.hideDigestTitle") : t("log.showDigestTitle")}
        >
          {sidebarOpen ? (
            <PanelRightClose className="h-3.5 w-3.5" />
          ) : (
            <PanelRightOpen className="h-3.5 w-3.5" />
          )}
          <span className="hidden sm:inline">{t("log.digestButton")}</span>
        </button>
      </div>

      {/* Session Anchor & Mid-file Context Bar */}
      {normalizedSessionId && (
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-emerald-200 bg-emerald-50/70 px-3 py-2 text-xs text-emerald-950">
          <div className="flex flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-1 rounded-full border border-emerald-300 bg-white px-2.5 py-0.5 font-medium text-emerald-800">
              <span>{resolvedSessionLabel}:</span>
              <code className="font-mono font-semibold">
                {normalizedSessionId}
              </code>
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
                disabled={Boolean(activeGcError)}
                onClick={() => jumpToLine(activeSessionDigest.first_line!)}
                className="rounded-md border border-emerald-300 bg-white px-2 py-1 text-[11px] font-medium text-emerald-800 hover:bg-emerald-100 disabled:opacity-50"
              >
                {t("log.jumpSessionStartBtn")}
              </button>
            )}
            {activeSessionDigest?.last_line != null && (
              <button
                type="button"
                disabled={Boolean(activeGcError)}
                onClick={() => jumpToLine(activeSessionDigest.last_line!)}
                className="rounded-md border border-emerald-400 bg-emerald-600 px-2 py-1 text-[11px] font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
              >
                {t("log.jumpSessionEndBtn")}
              </button>
            )}
            {activeSessionDigest?.last_ts && (
              <button
                type="button"
                disabled={aroundTimeLoading || Boolean(activeGcError)}
                onClick={() => {
                  const ts = activeSessionDigest.last_ts!;
                  setTimestampInput(ts);
                  setTimestampDirty(true);
                  void handleJumpAroundTime(ts, { onlySession: true });
                }}
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
              disabled={isSearching || Boolean(activeGcError)}
              onClick={() => {
                setSearchPattern(normalizedSessionId);
                setIsRegex(false);
                void runLogSearch(normalizedSessionId, {
                  isRegex: false,
                  onlySession: false,
                });
              }}
              className="inline-flex items-center gap-1 rounded-md border border-emerald-300 bg-white px-2 py-1 text-[11px] font-medium text-emerald-800 hover:bg-emerald-100 disabled:opacity-50"
            >
              <Search className="h-3 w-3" />
              <span>{t("log.searchSessionIdBtn")}</span>
            </button>
            <button
              type="button"
              disabled={Boolean(activeGcError)}
              onClick={() => {
                setSessionOnlyMode((prev) => !prev);
                setScrollTarget(null);
              }}
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
      )}

      {effectiveTotalLines > WINDOW_LINES && !activeGcError && (
        <nav
          aria-label={t("log.navAria")}
          className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-700"
        >
          <div className="flex items-center gap-2">
            <button
              type="button"
              aria-label={t("log.prevWindowAria")}
              disabled={windowStart === 0}
              onClick={() =>
                sessionOnlyMode
                  ? jumpToSessionIndex(windowStart - WINDOW_LINES + 1, "start")
                  : jumpToLine(windowStart - WINDOW_LINES + 1, "start")
              }
              className="rounded border border-slate-300 bg-white px-2 py-1 hover:bg-slate-100 disabled:opacity-40"
            >
              {t("log.prevWindow")}
            </button>
            <span aria-live="polite" className="font-mono">
              {t("log.windowRange", {
                start: formatNumber(windowStart + 1),
                end: formatNumber(windowEnd),
                total: formatNumber(effectiveTotalLines),
              })}
            </span>
            <button
              type="button"
              aria-label={t("log.nextWindowAria")}
              disabled={windowEnd === effectiveTotalLines}
              onClick={() =>
                sessionOnlyMode
                  ? jumpToSessionIndex(windowEnd + 1, "start")
                  : jumpToLine(windowEnd + 1, "start")
              }
              className="rounded border border-slate-300 bg-white px-2 py-1 hover:bg-slate-100 disabled:opacity-40"
            >
              {t("log.nextWindow")}
            </button>
          </div>
          {!sessionOnlyMode && (
            <form
              key={resolvedActiveLogId}
              onSubmit={(event) => {
                event.preventDefault();
                const line = new FormData(event.currentTarget).get("line");
                jumpToLine(Number(line));
              }}
              className="flex items-center gap-1.5"
            >
              <input
                aria-label={t("log.lineNumberAria")}
                name="line"
                type="number"
                min={1}
                max={totalLines}
                required
                placeholder={t("log.lineNumberPlaceholder")}
                className="w-32 rounded border border-slate-300 bg-white px-2 py-1 font-mono"
              />
              <button
                type="submit"
                className="rounded border border-slate-300 bg-white px-2 py-1 hover:bg-slate-100"
              >
                {t("log.goToLine")}
              </button>
            </form>
          )}
        </nav>
      )}

      {/* Search & Timestamp Toolbar */}
      <div className="space-y-2 border-b border-slate-200 bg-white px-3 py-2.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          {/* Search Form */}
          <form
            onSubmit={(e) => {
              void handleSearchSubmit(e);
            }}
            className="flex min-w-[260px] flex-1 flex-wrap items-center gap-1.5"
          >
            <div className="relative min-w-[180px] flex-1 sm:max-w-sm">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-400" />
              <input
                type="text"
                value={searchPattern}
                onChange={(e) => setSearchPattern(e.target.value)}
                placeholder={t("log.searchPlaceholder")}
                aria-label={t("log.searchAria")}
                disabled={Boolean(activeGcError)}
                className="h-8 w-full rounded-md border border-slate-300 bg-white pl-8 pr-7 font-mono text-xs text-slate-800 placeholder:font-sans placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:bg-slate-100"
              />
              {searchPattern && (
                <button
                  type="button"
                  onClick={() => {
                    searchSeqRef.current += 1;
                    setSearchPattern("");
                    setSearchResult(null);
                    setActiveMatchIdx(-1);
                    setSearchError(null);
                  }}
                  aria-label={t("log.clearSearchAria")}
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              )}
            </div>

            <button
              type="button"
              onClick={() => setIsRegex((prev) => !prev)}
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
              onClick={() => setCaseSensitive((prev) => !prev)}
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
                onClick={() => setFilterToSession((prev) => !prev)}
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
              disabled={
                isSearching || !searchPattern.trim() || Boolean(activeGcError)
              }
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
                  onClick={() => goToMatch(-1)}
                  disabled={searchResult.matches.length === 0}
                  aria-label={t("log.prevMatchAria")}
                  className="inline-flex h-7 w-7 items-center justify-center rounded border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 disabled:opacity-40"
                >
                  <ChevronUp className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  onClick={() => goToMatch(1)}
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
                disabled={aroundTimeLoading || Boolean(activeGcError)}
                onClick={() => {
                  setTimestampInput(crashTime);
                  setTimestampDirty(false);
                  void handleJumpAroundTime(crashTime);
                }}
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
              onChange={(e) => {
                setTimestampInput(e.target.value);
                setTimestampDirty(true);
              }}
              placeholder={t("log.timestampPlaceholder")}
              aria-label={t("log.timestampAria")}
              disabled={Boolean(activeGcError)}
              className="h-8 w-44 rounded-md border border-slate-300 bg-white px-2 font-mono text-xs text-slate-800 placeholder:font-sans placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:bg-slate-100"
            />

            <select
              aria-label={t("log.windowSecAria")}
              value={windowSec}
              onChange={(e) => setWindowSec(Number(e.target.value))}
              disabled={Boolean(activeGcError)}
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
                aroundTimeLoading ||
                !timestampInput.trim() ||
                Boolean(activeGcError)
              }
              onClick={() => {
                void handleJumpAroundTime(timestampInput);
              }}
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
              onClick={() => setSearchError(null)}
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
              onClick={() => setAroundTimeError(null)}
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
                onClick={() => {
                  aroundTimeSeqRef.current += 1;
                  setAroundTimeInfo(null);
                  setAroundTimeRange(null);
                }}
                className="text-indigo-600 hover:text-indigo-800"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          )}
      </div>

      {/* Main Split Body: Virtual Log Viewport + Digest Sidebar */}
      <div className="flex flex-col lg:flex-row">
        {/* Left: Virtualized Log Viewport */}
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
                    onClick={() => {
                      setSessionOnlyMode(false);
                      setScrollTarget(null);
                    }}
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
                    onClick={handleRetryFetch}
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
                  style={{ height: `${virtualizer.getTotalSize()}px` }}
                >
                  {virtualItems.map((virtualRow) => {
                    const rowIdx = windowStart + virtualRow.index + 1;
                    const cachedLine = currentLogLinesMap?.get(rowIdx);
                    const displayLineNumber = sessionOnlyMode
                      ? cachedLine?.line_number ?? null
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
                          lineContainsSession(
                            cachedLine.text,
                            normalizedSessionId,
                          ),
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
                              onClick={() => jumpToLine(displayLineNumber)}
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

        {/* Right: Digest Sidebar */}
        {sidebarOpen && (
          <aside className="flex h-[520px] w-full shrink-0 flex-col border-t border-slate-200 bg-slate-900 text-slate-100 lg:w-80 lg:border-l lg:border-t-0">
            <div className="space-y-1.5 border-b border-slate-800 px-3 py-2">
              <div className="flex flex-wrap items-center gap-1">
                {activeSessionDigest && (
                  <>
                    <button
                      type="button"
                      onClick={() => setSidebarTab("session_errors")}
                      className={`rounded-md px-2 py-1 text-[11px] font-medium transition-colors ${
                        sidebarTab === "session_errors"
                          ? "bg-emerald-700/80 text-white"
                          : "text-emerald-300/80 hover:text-emerald-200"
                      }`}
                    >
                      {t("log.tabSessionErrors", {
                        count: sessionRecentErrors.length,
                      })}
                    </button>
                    <button
                      type="button"
                      onClick={() => setSidebarTab("session_tail")}
                      className={`rounded-md px-2 py-1 text-[11px] font-medium transition-colors ${
                        sidebarTab === "session_tail"
                          ? "bg-emerald-700/80 text-white"
                          : "text-emerald-300/80 hover:text-emerald-200"
                      }`}
                    >
                      {t("log.tabSessionTail", {
                        count: sessionTailLines.length,
                      })}
                    </button>
                  </>
                )}
                <button
                  type="button"
                  onClick={() => setSidebarTab("errors")}
                  className={`rounded-md px-2 py-1 text-[11px] font-medium transition-colors ${
                    sidebarTab === "errors"
                      ? "bg-slate-800 text-white"
                      : "text-slate-400 hover:text-slate-200"
                  }`}
                >
                  {t("log.tabErrors", { count: recentErrors.length })}
                </button>
                <button
                  type="button"
                  onClick={() => setSidebarTab("tail")}
                  className={`rounded-md px-2 py-1 text-[11px] font-medium transition-colors ${
                    sidebarTab === "tail"
                      ? "bg-slate-800 text-white"
                      : "text-slate-400 hover:text-slate-200"
                  }`}
                >
                  {t("log.tabTail", { count: tailLines.length })}
                </button>
              </div>
              {activeDigest && (
                <div className="flex items-center justify-between font-mono text-[10px] text-slate-400">
                  <span>
                    All: {activeDigest.error_count ?? 0}E /{" "}
                    {activeDigest.warn_count ?? 0}W
                  </span>
                  {activeSessionDigest && (
                    <span className="text-emerald-400">
                      Sess: {activeSessionDigest.error_count}E /{" "}
                      {activeSessionDigest.warn_count}W
                    </span>
                  )}
                </div>
              )}
            </div>

            <div className="ck-scrollbar flex-1 divide-y divide-slate-800/80 overflow-y-auto">
              {sidebarTab === "session_errors" ? (
                sessionRecentErrors.length === 0 ? (
                  <div className="px-4 py-8 text-center text-xs text-slate-400">
                    {t("log.noSessionDigestErrors")}
                  </div>
                ) : (
                  sessionRecentErrors.map((entry, idx) => {
                    const lineNo = getDigestEntryLine(entry);
                    const canJump = lineNo !== null && !activeGcError;
                    return (
                      <button
                        key={`serr-${lineNo ?? idx}-${idx}`}
                        type="button"
                        disabled={!canJump}
                        onClick={() => {
                          if (lineNo !== null) {
                            jumpToLine(lineNo);
                          }
                        }}
                        className="w-full px-3 py-2 text-left transition-colors hover:bg-slate-800/70 disabled:cursor-default disabled:hover:bg-transparent"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span
                            className={`rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase ${getDigestLevelBadgeClass(
                              entry.level,
                            )}`}
                          >
                            {entry.level || "ERROR"}
                          </span>
                          <span className="font-mono text-[11px] text-emerald-300">
                            {lineNo !== null ? `L${lineNo}` : "-"}
                          </span>
                        </div>
                        {entry.timestamp && (
                          <div className="mt-1 font-mono text-[10px] text-slate-400">
                            {entry.timestamp}
                          </div>
                        )}
                        <p className="mt-1 line-clamp-2 break-all font-mono text-[11px] text-slate-200">
                          {entry.text}
                        </p>
                      </button>
                    );
                  })
                )
              ) : sidebarTab === "session_tail" ? (
                sessionTailLines.length === 0 && postSessionLines.length === 0 ? (
                  <div className="px-4 py-8 text-center text-xs text-slate-400">
                    {t("log.noSessionDigestTail")}
                  </div>
                ) : (
                  <>
                    {sessionTailLines.map((entry, idx) => {
                      const lineNo = getDigestEntryLine(entry);
                      const canJump = lineNo !== null && !activeGcError;
                      return (
                        <button
                          key={`stail-${lineNo ?? idx}-${idx}`}
                          type="button"
                          disabled={!canJump}
                          onClick={() => {
                            if (lineNo !== null) {
                              jumpToLine(lineNo);
                            }
                          }}
                          className="w-full px-3 py-2 text-left transition-colors hover:bg-slate-800/70 disabled:cursor-default disabled:hover:bg-transparent"
                        >
                          <div className="flex items-center justify-between gap-2">
                            <span className="font-mono text-[11px] text-emerald-300">
                              {lineNo !== null ? `L${lineNo}` : `#${idx + 1}`}
                            </span>
                            {entry.timestamp && (
                              <span className="truncate font-mono text-[10px] text-slate-400">
                                {entry.timestamp}
                              </span>
                            )}
                          </div>
                          <p className="mt-1 line-clamp-2 break-all font-mono text-[11px] text-slate-200">
                            {entry.text}
                          </p>
                        </button>
                      );
                    })}
                    {postSessionLines.length > 0 && (
                      <div className="bg-amber-950/30">
                        <div className="border-y border-amber-500/30 bg-amber-950/60 px-3 py-1.5 text-[10px] font-semibold text-amber-300">
                          {t("log.postSessionHeading", {
                            count: postSessionLines.length,
                          })}
                        </div>
                        <div className="divide-y divide-slate-800/80">
                          {postSessionLines.map((entry, idx) => {
                            const lineNo = getDigestEntryLine(entry);
                            const canJump = lineNo !== null && !activeGcError;
                            return (
                              <button
                                key={`post-${lineNo ?? idx}-${idx}`}
                                type="button"
                                disabled={!canJump}
                                onClick={() => {
                                  if (lineNo !== null) {
                                    jumpToLine(lineNo);
                                  }
                                }}
                                className="w-full px-3 py-2 text-left transition-colors hover:bg-amber-950/50 disabled:cursor-default"
                              >
                                <div className="flex items-center justify-between gap-2">
                                  <span className="font-mono text-[11px] text-amber-300">
                                    {lineNo !== null ? `L${lineNo}` : `#${idx + 1}`}
                                  </span>
                                  {entry.level && (
                                    <span className="rounded border border-amber-500/30 bg-amber-500/10 px-1 py-0.5 font-mono text-[9px] text-amber-200">
                                      {entry.level}
                                    </span>
                                  )}
                                </div>
                                <p className="mt-1 line-clamp-2 break-all font-mono text-[11px] text-amber-100/90">
                                  {entry.text}
                                </p>
                              </button>
                            );
                          })}
                        </div>
                      </div>
                    )}
                  </>
                )
              ) : sidebarTab === "errors" ? (
                recentErrors.length === 0 ? (
                  <div className="px-4 py-8 text-center text-xs text-slate-400">
                    {t("log.noDigestErrors")}
                  </div>
                ) : (
                  recentErrors.map((entry, idx) => {
                    const lineNo = getDigestEntryLine(entry);
                    const canJump = lineNo !== null && !activeGcError;
                    return (
                      <button
                        key={`err-${lineNo ?? idx}-${idx}`}
                        type="button"
                        disabled={!canJump}
                        onClick={() => {
                          if (lineNo !== null) {
                            jumpToLine(lineNo);
                          }
                        }}
                        className="w-full px-3 py-2 text-left transition-colors hover:bg-slate-800/70 disabled:cursor-default disabled:hover:bg-transparent"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span
                            className={`rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase ${getDigestLevelBadgeClass(
                              entry.level,
                            )}`}
                          >
                            {entry.level || "ERROR"}
                          </span>
                          <span className="font-mono text-[11px] text-indigo-300">
                            {lineNo !== null ? `L${lineNo}` : "-"}
                          </span>
                        </div>
                        {entry.timestamp && (
                          <div className="mt-1 font-mono text-[10px] text-slate-400">
                            {entry.timestamp}
                          </div>
                        )}
                        <p className="mt-1 line-clamp-2 break-all font-mono text-[11px] text-slate-200">
                          {entry.text}
                        </p>
                      </button>
                    );
                  })
                )
              ) : tailLines.length === 0 ? (
                <div className="px-4 py-8 text-center text-xs text-slate-400">
                  {t("log.noDigestTail")}
                </div>
              ) : (
                tailLines.map((entry, idx) => {
                  const lineNo = getDigestEntryLine(entry);
                  const canJump = lineNo !== null && !activeGcError;
                  return (
                    <button
                      key={`tail-${lineNo ?? idx}-${idx}`}
                      type="button"
                      disabled={!canJump}
                      onClick={() => {
                        if (lineNo !== null) {
                          jumpToLine(lineNo);
                        }
                      }}
                      className="w-full px-3 py-2 text-left transition-colors hover:bg-slate-800/70 disabled:cursor-default disabled:hover:bg-transparent"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-mono text-[11px] text-indigo-300">
                          {lineNo !== null ? `L${lineNo}` : `#${idx + 1}`}
                        </span>
                        {entry.timestamp && (
                          <span className="truncate font-mono text-[10px] text-slate-400">
                            {entry.timestamp}
                          </span>
                        )}
                      </div>
                      <p className="mt-1 line-clamp-2 break-all font-mono text-[11px] text-slate-300">
                        {entry.text}
                      </p>
                    </button>
                  );
                })
              )}
            </div>
          </aside>
        )}
      </div>
    </div>
  );
}

export default VirtualLogViewer;
