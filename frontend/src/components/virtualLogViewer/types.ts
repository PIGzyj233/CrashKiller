export const PAGE_SIZE = 200;
// Bound the scroll height and virtualizer bookkeeping, even for very large logs.
export const WINDOW_LINES = 20_000;
export const ROW_HEIGHT = 22;
export const OVERSCAN = 20;
export const MAX_CACHED_PAGES_PER_LOG = 100;
export const SEARCH_MAX_MATCHES = 200;
export const AROUND_TIME_MAX_LINES = 500;

export interface LogHighlightRange {
  logId: string;
  startLine: number;
  endLine: number;
  nonce?: number;
}

export interface LogScrollTarget {
  logId: string;
  mode: "full" | "session";
  lineNumber: number;
  align: "start" | "center";
}

export interface LogGcError {
  status: number;
  message: string;
}

export interface AroundTimeInfo {
  logId: string;
  text: string;
}

export interface AroundTimeRange {
  logId: string;
  startLine: number;
  endLine: number;
}

export type SidebarTab = "session_errors" | "session_tail" | "errors" | "tail";
