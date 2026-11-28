// ============================================================================
// Logs (/api/v1/logs)
// ============================================================================

export interface LogDigestErrorEntry {
  line_number?: number;
  line?: number;
  text: string;
  timestamp?: string | null;
  level?: string | null;
}

export interface LogSessionDigest {
  session_id: string;
  match_count: number;
  error_count: number;
  warn_count: number;
  first_line: number | null;
  last_line: number | null;
  first_ts?: string | null;
  last_ts?: string | null;
  trailing_lines_after_session?: number;
  tail_lines?: LogDigestErrorEntry[];
  recent_errors?: LogDigestErrorEntry[];
  post_session_lines?: LogDigestErrorEntry[];
}

export interface LogDigest {
  total_lines: number;
  error_count?: number;
  warn_count?: number;
  tail_lines?: LogDigestErrorEntry[];
  recent_errors?: LogDigestErrorEntry[];
  session?: LogSessionDigest | null;
}

export interface CrashLogRead {
  id: string;
  crash_id: string;
  filename: string;
  raw_size: number;
  compressed_size: number;
  total_lines: number;
  is_indexing?: boolean;
  digest_json?: LogDigest | null;
}

export interface LogSummaryResponse {
  id: string;
  crash_id: string;
  filename: string;
  raw_size: number;
  compressed_size: number;
  total_lines: number;
  is_indexing?: boolean;
  digest: LogDigest;
}

export interface LogLine {
  line_number: number;
  text: string;
  timestamp?: string | null;
  level?: string | null;
}

export interface LogLinesResponse {
  log_id: string;
  total_lines: number;
  start_line: number;
  end_line: number;
  lines: LogLine[];
}

export interface LogSessionLinesResponse {
  log_id: string;
  session_id: string;
  total_matches: number;
  start_index: number;
  end_index: number;
  lines: LogLine[];
}

export interface LogAroundTimeResponse {
  log_id: string;
  center_timestamp: string;
  window_sec: number;
  start_line: number;
  end_line: number;
  session_id?: string | null;
  lines: LogLine[];
}

export interface LogSearchMatch {
  line_number: number;
  text: string;
  timestamp?: string | null;
  level?: string | null;
}

export interface LogSearchResponse {
  log_id: string;
  pattern: string;
  total_matches: number;
  truncated: boolean;
  elapsed_ms: number;
  session_id?: string | null;
  matches: LogSearchMatch[];
}
