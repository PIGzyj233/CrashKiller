import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  AlertCircle,
  AlertTriangle,
  Archive,
  ChevronDown,
  ChevronUp,
  Clock,
  FileText,
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
  lineNumber: number;
  align: "start" | "center";
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
  highlightRange,
}: VirtualLogViewerProps) {
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

  // Sidebar state
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [sidebarTab, setSidebarTab] = useState<"errors" | "tail">("errors");

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

  // Caches & concurrency refs
  const linesCacheRef = useRef<Map<string, Map<number, LogLine>>>(new Map());
  const loadedPagesRef = useRef<Map<string, Map<number, number[]>>>(new Map());
  const inFlightRef = useRef<Map<string, Promise<void>>>(new Map());
  const failedPagesRef = useRef<Map<string, string>>(new Map());
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
  const activeTarget =
    scrollTarget?.logId === resolvedActiveLogId ? scrollTarget : null;
  const targetIndex = Math.max(
    0,
    Math.min(totalLines, activeTarget?.lineNumber ?? 1) - 1,
  );
  const windowStart = Math.floor(targetIndex / WINDOW_LINES) * WINDOW_LINES;
  const windowEnd = Math.min(totalLines, windowStart + WINDOW_LINES);
  const windowCount = windowEnd - windowStart;

  const virtualizer = useVirtualizer({
    count: windowCount,
    getScrollElement: () => scrollParentRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: OVERSCAN,
    getItemKey: (index) => `${resolvedActiveLogId}:${windowStart + index}`,
    useFlushSync: false,
    enabled: Boolean(activeLog && totalLines > 0 && !activeGcError),
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
              err instanceof Error ? err.message : "Failed to fetch log lines";
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
    [gcErrorByLog, touchAndStorePage],
  );

  // Compute virtual items and primitive bounds for prefetching
  const virtualItems = virtualizer.getVirtualItems();
  const firstVisibleIdx = virtualItems[0]?.index ?? -1;
  const lastVisibleIdx =
    virtualItems.length > 0 ? virtualItems[virtualItems.length - 1].index : -1;

  // Prefetch visible + adjacent pages
  useEffect(() => {
    if (
      !activeLog ||
      totalLines <= 0 ||
      activeGcError ||
      firstVisibleIdx < 0 ||
      lastVisibleIdx < 0
    ) {
      return;
    }

    const startPage = Math.floor((windowStart + firstVisibleIdx) / PAGE_SIZE);
    const endPage = Math.floor((windowStart + lastVisibleIdx) / PAGE_SIZE);
    const maxPage = Math.floor((totalLines - 1) / PAGE_SIZE);
    const prefetchStart = Math.max(0, startPage - 1);
    const prefetchEnd = Math.min(maxPage, endPage + 1);

    visiblePageRangeRef.current = { start: prefetchStart, end: prefetchEnd };

    for (let p = prefetchStart; p <= prefetchEnd; p += 1) {
      void fetchPage(activeLog.id, p, totalLines);
    }
  }, [
    activeLog,
    totalLines,
    activeGcError,
    firstVisibleIdx,
    lastVisibleIdx,
    windowStart,
    fetchPage,
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
      setScrollTarget({ logId: activeLog.id, lineNumber, align });
    },
    [activeLog, activeGcError],
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

    if (resolvedActiveLogId !== highlightRange.logId) {
      selectLog(highlightRange.logId);
    }
    setScrollTarget({
      logId: highlightRange.logId,
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
    setRetryTick((t) => t + 1);
  };

  const handleJumpAroundTime = async (targetTs: string) => {
    if (!activeLog || !targetTs.trim() || activeGcError) {
      return;
    }
    const requestLogId = activeLog.id;
    const seq = ++aroundTimeSeqRef.current;
    setAroundTimeLoading(true);
    setAroundTimeError(null);
    setAroundTimeInfo(null);
    try {
      const res = await getLogAroundTime(requestLogId, {
        timestamp: targetTs.trim(),
        window_sec: windowSec,
        max_lines: AROUND_TIME_MAX_LINES,
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
          text: "No log lines found within this time window.",
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
        text: `Jumped to L${bestLine} (${res.lines.length} lines in ±${res.window_sec}s window)`,
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
          err instanceof Error ? err.message : "Failed to jump around timestamp",
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

  const handleSearchSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!activeLog || !searchPattern.trim() || activeGcError) {
      return;
    }
    const requestLogId = activeLog.id;
    const seq = ++searchSeqRef.current;
    setIsSearching(true);
    setSearchError(null);
    try {
      const res = await searchLog(requestLogId, {
        pattern: searchPattern,
        is_regex: isRegex,
        case_sensitive: caseSensitive,
        max_matches: SEARCH_MAX_MATCHES,
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
        setSearchError(err instanceof Error ? err.message : "Log search failed");
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

  const activeDigest: LogDigest | null = useMemo(() => {
    if (!activeLog) {
      return null;
    }
    return activeLog.digest_json ?? summaryByLog[activeLog.id] ?? null;
  }, [activeLog, summaryByLog]);

  const recentErrors = activeDigest?.recent_errors ?? [];
  const tailLines = activeDigest?.tail_lines ?? [];

  if (logs.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-slate-300 bg-white px-6 py-12 text-center shadow-sm">
        <FileText className="mx-auto h-7 w-7 text-slate-400" />
        <p className="mt-2 text-sm font-medium text-slate-700">
          No log files attached to this crash report.
        </p>
        <p className="mt-1 text-xs text-slate-500">
          Attach <span className="font-mono">.log</span> or text files when
          uploading crashes to inspect them with virtualized search and
          timestamp navigation.
        </p>
      </div>
    );
  }

  // Trigger dependency read on cacheVersion so cached lines map reflects latest reads
  const currentLogLinesMap =
    cacheVersion >= 0 && resolvedActiveLogId
      ? linesCacheRef.current.get(resolvedActiveLogId)
      : undefined;

  return (
    <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
      {/* Multi-log Tabs Header */}
      <div className="ck-scrollbar flex items-center justify-between gap-2 overflow-x-auto border-b border-slate-200 bg-slate-50 px-3 py-2">
        <div className="flex items-center gap-1.5" role="tablist">
          {logs.map((log) => {
            const isActive = log.id === resolvedActiveLogId;
            const errCount = log.digest_json?.error_count ?? 0;
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
                  {log.total_lines.toLocaleString()}L ·{" "}
                  {formatBytes(log.raw_size)}
                </span>
                {errCount > 0 && (
                  <span className="rounded-full bg-rose-100 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-rose-700">
                    {errCount} err
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
          title={sidebarOpen ? "Hide digest sidebar" : "Show digest sidebar"}
        >
          {sidebarOpen ? (
            <PanelRightClose className="h-3.5 w-3.5" />
          ) : (
            <PanelRightOpen className="h-3.5 w-3.5" />
          )}
          <span className="hidden sm:inline">Digest</span>
        </button>
      </div>

      {totalLines > WINDOW_LINES && !activeGcError && (
        <nav
          aria-label="Log navigation"
          className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-700"
        >
          <div className="flex items-center gap-2">
            <button
              type="button"
              aria-label="Previous log window"
              disabled={windowStart === 0}
              onClick={() => jumpToLine(windowStart - WINDOW_LINES + 1, "start")}
              className="rounded border border-slate-300 bg-white px-2 py-1 hover:bg-slate-100 disabled:opacity-40"
            >
              Previous
            </button>
            <span aria-live="polite" className="font-mono">
              Lines {(windowStart + 1).toLocaleString()}–{windowEnd.toLocaleString()}
              {" of "}{totalLines.toLocaleString()}
            </span>
            <button
              type="button"
              aria-label="Next log window"
              disabled={windowEnd === totalLines}
              onClick={() => jumpToLine(windowEnd + 1, "start")}
              className="rounded border border-slate-300 bg-white px-2 py-1 hover:bg-slate-100 disabled:opacity-40"
            >
              Next
            </button>
          </div>
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
              aria-label="Log line number"
              name="line"
              type="number"
              min={1}
              max={totalLines}
              required
              placeholder="Line number"
              className="w-32 rounded border border-slate-300 bg-white px-2 py-1 font-mono"
            />
            <button
              type="submit"
              className="rounded border border-slate-300 bg-white px-2 py-1 hover:bg-slate-100"
            >
              Go to line
            </button>
          </form>
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
                placeholder="Search log (keyword or regex)..."
                aria-label="Search log"
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
                  aria-label="Clear search"
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
              title="Toggle Regular Expression"
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
              title="Toggle Case Sensitive"
              className={`h-8 rounded-md border px-2 font-mono text-xs font-semibold transition-colors ${
                caseSensitive
                  ? "border-indigo-300 bg-indigo-50 text-indigo-700"
                  : "border-slate-300 bg-white text-slate-600 hover:bg-slate-50"
              }`}
            >
              Aa
            </button>

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
              <span>Search</span>
            </button>

            {searchResult && searchResult.log_id === resolvedActiveLogId && (
              <div className="flex items-center gap-1 text-xs">
                <span className="rounded bg-slate-100 px-2 py-1 font-mono text-[11px] text-slate-700">
                  {searchResult.matches.length > 0
                    ? `${activeMatchIdx + 1} / ${searchResult.matches.length}`
                    : "0 matches"}{" "}
                  <span className="text-slate-400">
                    ({searchResult.elapsed_ms.toFixed(1)} ms)
                  </span>
                </span>
                <button
                  type="button"
                  onClick={() => goToMatch(-1)}
                  disabled={searchResult.matches.length === 0}
                  aria-label="Previous match"
                  className="inline-flex h-7 w-7 items-center justify-center rounded border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 disabled:opacity-40"
                >
                  <ChevronUp className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  onClick={() => goToMatch(1)}
                  disabled={searchResult.matches.length === 0}
                  aria-label="Next match"
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
                title={`Jump to crash time (${crashTime})`}
              >
                <Clock className="h-3.5 w-3.5" />
                <span>Crash Time</span>
              </button>
            )}

            <input
              type="text"
              value={timestampInput}
              onChange={(e) => {
                setTimestampInput(e.target.value);
                setTimestampDirty(true);
              }}
              placeholder="ISO timestamp..."
              aria-label="Timestamp jump target"
              disabled={Boolean(activeGcError)}
              className="h-8 w-44 rounded-md border border-slate-300 bg-white px-2 font-mono text-xs text-slate-800 placeholder:font-sans placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:bg-slate-100"
            />

            <select
              aria-label="Timestamp window seconds"
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
              <span>Jump</span>
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
                Showing first {searchResult.matches.length} matches (result set
                truncated at limit). Refine your pattern to narrow down results.
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
                  Raw compressed log unavailable (HTTP {activeGcError.status})
                </h3>
                <p className="text-xs text-slate-400">
                  This log file&apos;s compressed archive was reclaimed by disk
                  garbage collection or retention cleanup. Its PostgreSQL error
                  digest and tail lines remain accessible in the Digest sidebar.
                </p>
              </div>
            </div>
          ) : totalLines === 0 ? (
            <div className="flex h-[320px] items-center justify-center text-xs text-slate-400">
              This log file is empty (0 lines).
            </div>
          ) : (
            <>
              {activePageError && (
                <div className="flex items-center justify-between gap-2 border-b border-rose-500/30 bg-rose-950/80 px-3 py-1.5 text-xs text-rose-200">
                  <span>Error loading log chunks: {activePageError}</span>
                  <button
                    type="button"
                    onClick={handleRetryFetch}
                    className="inline-flex items-center gap-1 rounded border border-rose-400/40 bg-rose-900/60 px-2 py-0.5 text-[11px] font-medium text-white hover:bg-rose-800"
                  >
                    <RefreshCw className="h-3 w-3" />
                    Retry
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
                    const lineNumber = windowStart + virtualRow.index + 1;
                    const cachedLine = currentLogLinesMap?.get(lineNumber);

                    const isHighlighted =
                      normalizedHighlight !== null &&
                      lineNumber >= normalizedHighlight.start &&
                      lineNumber <= normalizedHighlight.end;

                    const isAroundTimeHighlight =
                      !isHighlighted &&
                      aroundTimeRange !== null &&
                      aroundTimeRange.logId === resolvedActiveLogId &&
                      lineNumber >= aroundTimeRange.startLine &&
                      lineNumber <= aroundTimeRange.endLine;

                    const isActiveMatch = activeMatchLine === lineNumber;
                    const isMatched = matchedLineSet.has(lineNumber);

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
                    }

                    return (
                      <div
                        key={virtualRow.key}
                        data-index={lineNumber - 1}
                        data-line-number={lineNumber}
                        data-highlighted={isHighlighted ? "true" : undefined}
                        className={`absolute left-0 top-0 flex min-w-full items-center ${rowBgClass}`}
                        style={{
                          height: `${ROW_HEIGHT}px`,
                          transform: `translateY(${virtualRow.start}px)`,
                        }}
                      >
                        <span className="w-20 shrink-0 select-none border-r border-slate-800 pr-3 text-right text-[11px] text-slate-500">
                          {lineNumber}
                        </span>
                        {cachedLine ? (
                          <span
                            className={`whitespace-pre px-3 ${getLineTextColorClass(
                              cachedLine,
                            )}`}
                          >
                            {cachedLine.text}
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
            <div className="flex items-center justify-between border-b border-slate-800 px-3 py-2">
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => setSidebarTab("errors")}
                  className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                    sidebarTab === "errors"
                      ? "bg-slate-800 text-white"
                      : "text-slate-400 hover:text-slate-200"
                  }`}
                >
                  Errors ({recentErrors.length})
                </button>
                <button
                  type="button"
                  onClick={() => setSidebarTab("tail")}
                  className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                    sidebarTab === "tail"
                      ? "bg-slate-800 text-white"
                      : "text-slate-400 hover:text-slate-200"
                  }`}
                >
                  Tail ({tailLines.length})
                </button>
              </div>
              {activeDigest && (
                <span className="font-mono text-[11px] text-slate-400">
                  {activeDigest.error_count ?? 0}E /{" "}
                  {activeDigest.warn_count ?? 0}W
                </span>
              )}
            </div>

            <div className="ck-scrollbar flex-1 divide-y divide-slate-800/80 overflow-y-auto">
              {sidebarTab === "errors" ? (
                recentErrors.length === 0 ? (
                  <div className="px-4 py-8 text-center text-xs text-slate-400">
                    No ERROR / FATAL / ASSERT digest entries recorded.
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
                  No tail lines recorded in digest.
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
