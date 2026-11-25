import type { AIAnalysisRunRead } from "./ai";
import type {
  AIVerdict,
  CrashStatus,
  IssueStatus,
  ReprocessMode,
  VerdictAction,
} from "./common";
import type { CrashClusterRead, IssueRead } from "./issues";
import type { CrashLogRead } from "./logs";
import type { MissingSymbolInfo, ParsedStacks, ParsedSummary } from "./stacks";

// ============================================================================
// Crashes & SSE (/api/v1/crashes)
// ============================================================================

export interface CrashSummary {
  id: string;
  project_id: string;
  cluster_id: string | null;
  issue_id: number | null;
  status: CrashStatus;
  app_version: string;
  build_id: string | null;
  os_version: string | null;
  session_id: string | null;
  dump_raw_size: number;
  dump_compressed_size: number;
  crash_reason: string | null;
  crash_address: string | null;
  ai_verdict: AIVerdict | null;
  ai_confidence: number | null;
  is_human_verified: boolean;
  is_pinned: boolean;
  crash_time: string | null;
  created_at: string;
}

export interface CrashListResponse {
  items: CrashSummary[];
  total: number;
}

export interface CrashDetailRead extends CrashSummary {
  dump_path?: string | null;
  parsed_summary_json: ParsedSummary | null;
  parsed_stacks_json: ParsedStacks | null;
  missing_symbols_json: MissingSymbolInfo[] | null;
  logs: CrashLogRead[];
  cluster: CrashClusterRead | null;
  issue: IssueRead | null;
  ai_runs: AIAnalysisRunRead[];
  related_session_crashes: CrashSummary[];
  related_session_crash_count: number;
}

export interface CrashSSEEvent {
  crash_id: string;
  status: CrashStatus;
  stage?: string | null;
  ai_verdict?: AIVerdict | null;
  error?: string | null;
}

export interface CrashUploadResponse {
  crash_id: string;
  status: CrashStatus;
  log_ids: string[];
}

export interface CrashReprocessRequest {
  mode: ReprocessMode;
}

export interface CrashReprocessResponse {
  crash_id: string;
  status: CrashStatus;
}

export interface CrashVerdictNewIssue {
  title: string;
  component?: string;
  root_cause?: string;
  status?: IssueStatus;
}

export interface CrashVerdictRequest {
  action: VerdictAction;
  issue_id?: number;
  new_issue?: CrashVerdictNewIssue;
  root_cause?: string;
  add_log_patterns?: string[];
  is_ambiguous_stack?: boolean;
}

export interface CrashPinResponse {
  crash_id: string;
  is_pinned: boolean;
}
