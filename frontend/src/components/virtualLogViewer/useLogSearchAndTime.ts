import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { ApiError, getLogAroundTime, searchLog } from "../../api/client";
import type {
  CrashLogRead,
  LogLine,
  LogSearchResponse,
} from "../../api/types";
import { useAppI18n } from "../../i18n";
import {
  AROUND_TIME_MAX_LINES,
  SEARCH_MAX_MATCHES,
  type AroundTimeInfo,
  type AroundTimeRange,
  type LogGcError,
} from "./types";
import { findClosestLineToTimestamp } from "./utils";

export interface UseLogSearchAndTimeOptions {
  activeLog: CrashLogRead | null;
  resolvedActiveLogId: string;
  crashTime?: string | null;
  normalizedSessionId: string | null;
  hasGcError: boolean;
  seedLines: (logId: string, lines: LogLine[]) => void;
  markGcError: (logId: string, error: LogGcError) => void;
  jumpToLine: (lineNumber: number) => void;
}

export function useLogSearchAndTime({
  activeLog,
  resolvedActiveLogId,
  crashTime,
  normalizedSessionId,
  hasGcError,
  seedLines,
  markGcError,
  jumpToLine,
}: UseLogSearchAndTimeOptions) {
  const { t } = useAppI18n();

  const [filterToSession, setFilterToSession] = useState(false);

  // Timestamp jump state
  const [timestampInput, setTimestampInput] = useState<string>(
    () => crashTime ?? "",
  );
  const [timestampDirty, setTimestampDirty] = useState(false);
  const [windowSec, setWindowSec] = useState<number>(10);
  const [aroundTimeLoading, setAroundTimeLoading] = useState(false);
  const [aroundTimeInfo, setAroundTimeInfo] = useState<AroundTimeInfo | null>(
    null,
  );
  const [aroundTimeError, setAroundTimeError] = useState<string | null>(null);
  const [aroundTimeRange, setAroundTimeRange] =
    useState<AroundTimeRange | null>(null);

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

  const handleJumpAroundTime = async (
    targetTs: string,
    opts?: { onlySession?: boolean },
  ) => {
    if (!activeLog || !targetTs.trim() || hasGcError) {
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
        session_id: useOnlySession
          ? (normalizedSessionId ?? undefined)
          : undefined,
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

      const bestLine = findClosestLineToTimestamp(
        res.lines,
        res.center_timestamp,
        res.start_line,
        res.end_line,
      );

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
      if (
        err instanceof ApiError &&
        (err.status === 404 || err.status === 410)
      ) {
        markGcError(requestLogId, { status: err.status, message: err.message });
      }
      if (
        activeLogIdRef.current !== requestLogId ||
        seq !== aroundTimeSeqRef.current
      ) {
        return;
      }
      if (
        !(
          err instanceof ApiError &&
          (err.status === 404 || err.status === 410)
        )
      ) {
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
    if (!activeLog || !patternToRun.trim() || hasGcError) {
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
        session_id: useOnlySession
          ? (normalizedSessionId ?? undefined)
          : undefined,
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
      if (
        err instanceof ApiError &&
        (err.status === 404 || err.status === 410)
      ) {
        markGcError(requestLogId, { status: err.status, message: err.message });
      }
      if (
        activeLogIdRef.current !== requestLogId ||
        seq !== searchSeqRef.current
      ) {
        return;
      }
      setSearchResult(null);
      setActiveMatchIdx(-1);
      if (
        !(
          err instanceof ApiError &&
          (err.status === 404 || err.status === 410)
        )
      ) {
        setSearchError(
          err instanceof Error ? err.message : t("log.searchFailed"),
        );
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

  const clearSearch = () => {
    searchSeqRef.current += 1;
    setSearchPattern("");
    setSearchResult(null);
    setActiveMatchIdx(-1);
    setSearchError(null);
  };

  const dismissAroundTimeInfo = () => {
    aroundTimeSeqRef.current += 1;
    setAroundTimeInfo(null);
    setAroundTimeRange(null);
  };

  const jumpToCrashTime = () => {
    if (!crashTime) {
      return;
    }
    setTimestampInput(crashTime);
    setTimestampDirty(false);
    void handleJumpAroundTime(crashTime);
  };

  const jumpSessionLastTs = (ts: string) => {
    setTimestampInput(ts);
    setTimestampDirty(true);
    void handleJumpAroundTime(ts, { onlySession: true });
  };

  const searchSessionId = () => {
    if (!normalizedSessionId) {
      return;
    }
    setSearchPattern(normalizedSessionId);
    setIsRegex(false);
    void runLogSearch(normalizedSessionId, {
      isRegex: false,
      onlySession: false,
    });
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
      ? (searchResult.matches[activeMatchIdx]?.line_number ?? null)
      : null;

  return {
    filterToSession,
    setFilterToSession,
    timestampInput,
    setTimestampInput,
    setTimestampDirty,
    windowSec,
    setWindowSec,
    aroundTimeLoading,
    aroundTimeInfo,
    aroundTimeError,
    setAroundTimeError,
    aroundTimeRange,
    searchPattern,
    setSearchPattern,
    isRegex,
    setIsRegex,
    caseSensitive,
    setCaseSensitive,
    isSearching,
    searchResult,
    activeMatchIdx,
    searchError,
    setSearchError,
    matchedLineSet,
    activeMatchLine,
    handleJumpAroundTime,
    handleSearchSubmit,
    goToMatch,
    clearSearch,
    dismissAroundTimeInfo,
    jumpToCrashTime,
    jumpSessionLastTs,
    searchSessionId,
  };
}
