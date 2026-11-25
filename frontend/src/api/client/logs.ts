import type {
  LogAroundTimeResponse,
  LogLinesResponse,
  LogSearchResponse,
  LogSessionLinesResponse,
  LogSummaryResponse,
} from "../types";
import { API_BASE, buildQueryString, requestJson } from "./http";

// ============================================================================
// Logs (/api/v1/logs)
// ============================================================================

export interface LogAroundTimeParams {
  timestamp: string;
  window_sec?: number;
  max_lines?: number;
  session_id?: string;
  only_session?: boolean;
}

export interface LogSessionLinesParams {
  session_id?: string;
  start?: number;
  end?: number;
}

export interface SearchLogParams {
  pattern: string;
  is_regex?: boolean;
  max_matches?: number;
  case_sensitive?: boolean;
  session_id?: string;
  only_session?: boolean;
}

export function getLogSummary(logId: string): Promise<LogSummaryResponse> {
  return requestJson<LogSummaryResponse>(
    `${API_BASE}/logs/${encodeURIComponent(logId)}/summary`,
  );
}

export function getLogLines(
  logId: string,
  start = 1,
  end = 200,
): Promise<LogLinesResponse> {
  const qs = buildQueryString({ start, end });
  return requestJson<LogLinesResponse>(
    `${API_BASE}/logs/${encodeURIComponent(logId)}/lines${qs}`,
  );
}

export function getLogSessionLines(
  logId: string,
  params?: LogSessionLinesParams,
): Promise<LogSessionLinesResponse> {
  const qs = buildQueryString({
    session_id: params?.session_id,
    start: params?.start ?? 1,
    end: params?.end ?? 200,
  });
  return requestJson<LogSessionLinesResponse>(
    `${API_BASE}/logs/${encodeURIComponent(logId)}/session-lines${qs}`,
  );
}

export function getLogAroundTime(
  logId: string,
  params: LogAroundTimeParams,
): Promise<LogAroundTimeResponse> {
  const qs = buildQueryString({
    timestamp: params.timestamp,
    window_sec: params.window_sec,
    max_lines: params.max_lines,
    session_id: params.session_id,
    only_session: params.only_session ? true : undefined,
  });
  return requestJson<LogAroundTimeResponse>(
    `${API_BASE}/logs/${encodeURIComponent(logId)}/around-time${qs}`,
  );
}

export function searchLog(
  logId: string,
  params: SearchLogParams,
): Promise<LogSearchResponse> {
  const qs = buildQueryString({
    pattern: params.pattern,
    is_regex: params.is_regex,
    max_matches: params.max_matches,
    case_sensitive: params.case_sensitive,
    session_id: params.session_id,
    only_session: params.only_session ? true : undefined,
  });
  return requestJson<LogSearchResponse>(
    `${API_BASE}/logs/${encodeURIComponent(logId)}/search${qs}`,
  );
}
