import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  getLogLines,
  getLogSessionLines,
  getLogSummary,
} from "../../api/client";
import type { CrashLogRead, LogDigest, LogLine } from "../../api/types";
import { useAppI18n } from "../../i18n";
import {
  MAX_CACHED_PAGES_PER_LOG,
  PAGE_SIZE,
  type LogGcError,
} from "./types";

export interface UseLogPageCacheOptions {
  activeLog: CrashLogRead | null;
  resolvedActiveLogId: string;
  normalizedSessionId: string | null;
}

export function useLogPageCache({
  activeLog,
  resolvedActiveLogId,
  normalizedSessionId,
}: UseLogPageCacheOptions) {
  const { t } = useAppI18n();
  const [cacheVersion, setCacheVersion] = useState(0);
  const [gcErrorByLog, setGcErrorByLog] = useState<Record<string, LogGcError>>(
    {},
  );
  const [pageErrorByLog, setPageErrorByLog] = useState<Record<string, string>>(
    {},
  );
  const [summaryByLog, setSummaryByLog] = useState<Record<string, LogDigest>>(
    {},
  );
  const [sessionTotalByLog, setSessionTotalByLog] = useState<
    Record<string, number>
  >({});
  const [retryTick, setRetryTick] = useState(0);

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

  const visiblePageRangeRef = useRef<{ start: number; end: number }>({
    start: 0,
    end: 0,
  });
  const isMountedRef = useRef(true);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  const activeDigest: LogDigest | null = useMemo(() => {
    if (!activeLog) {
      return null;
    }
    return activeLog.digest_json ?? summaryByLog[activeLog.id] ?? null;
  }, [activeLog, summaryByLog]);

  const activeSessionDigest = activeDigest?.session ?? null;

  const activeGcError = resolvedActiveLogId
    ? (gcErrorByLog[resolvedActiveLogId] ?? null)
    : null;
  const activePageError = resolvedActiveLogId
    ? (pageErrorByLog[resolvedActiveLogId] ?? null)
    : null;

  const markGcError = useCallback((logId: string, error: LogGcError) => {
    setGcErrorByLog((prev) => ({
      ...prev,
      [logId]: error,
    }));
  }, []);

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
          if (
            err instanceof ApiError &&
            (err.status === 404 || err.status === 410)
          ) {
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
            touchAndStoreSessionPage(
              logId,
              pageIdx,
              res.start_index,
              res.lines,
            );
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

  const handleRetryFetch = useCallback(() => {
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
  }, [activeLog]);

  const getCurrentLogLinesMap = (
    sessionOnlyMode: boolean,
  ): Map<number, LogLine> | undefined => {
    if (cacheVersion < 0 || !resolvedActiveLogId) {
      return undefined;
    }
    return sessionOnlyMode
      ? sessionLinesCacheRef.current.get(resolvedActiveLogId)
      : linesCacheRef.current.get(resolvedActiveLogId);
  };

  return {
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
  };
}
