// ============================================================================
// Core Enums & Literal Unions
// ============================================================================

export type CrashStatus =
  | "PENDING"
  | "PARSING"
  | "TRIAGING"
  | "ANALYZED"
  | "FAILED";

export type AIVerdict =
  | "KNOWN_ISSUE"
  | "NEW_ISSUE"
  | "REGRESSION"
  | "NEEDS_REVIEW";

export type IssueStatus =
  | "NEW"
  | "KNOWN"
  | "FIXED"
  | "WONTFIX"
  | "REGRESSION";

export type AITriageMode = "SMART_TRIAGE" | "ALWAYS" | "MANUAL";

export type AIOutputLanguage = "zh-CN" | "en-US";

export type LLMProviderId =
  | "openai"
  | "anthropic"
  | "gemini"
  | "deepseek"
  | "openrouter"
  | "qwen"
  | "zhipu"
  | "ollama"
  | "custom";

export type LLMThinkingLevel = "auto" | "off" | "low" | "medium" | "high";

export type LLMProtocol = "openai_compatible" | "anthropic_messages";

export interface LLMProviderPreset {
  id: LLMProviderId;
  name: string;
  protocol: LLMProtocol;
  default_base_url: string;
  default_model: string;
  models: string[];
  requires_api_key: boolean;
  supports_thinking: boolean;
}

export type AIRunStatus = "RUNNING" | "COMPLETED" | "FAILED";

export type ReprocessMode = "full" | "triage";

export type VerdictAction = "confirm" | "assign_issue" | "create_issue";

export type ArtifactKind = "dump" | "stacks_json";

// ============================================================================
// Health
// ============================================================================

export interface HealthResponse {
  status: string;
}

// ============================================================================
// Projects (/api/v1/projects)
// ============================================================================

export interface ProjectSettings {
  ai_triage_mode?: AITriageMode | null;
  noise_modules?: string[];
  noise_functions?: string[];
  custom_crash_handlers?: string[];
  log_retention_days?: number;
  dump_retention_days?: number;
  session_id_label?: string | null;
  [key: string]: unknown;
}

export interface ProjectRead {
  id: string;
  key: string;
  name: string;
  settings_json: ProjectSettings;
  created_at: string;
}

export interface ProjectCreate {
  key: string;
  name: string;
  settings_json?: ProjectSettings;
}

export interface ProjectCreateResponse extends ProjectRead {
  api_key: string;
}

export interface ProjectUpdate {
  name?: string;
  settings_json?: ProjectSettings;
}

export interface ProjectRotateKeyResponse {
  id: string;
  key: string;
  api_key: string;
}

export interface ProjectApiKeyResponse {
  id: string;
  key: string;
  api_key: string | null;
}

export interface ProjectApiKeySyncRequest {
  api_key: string;
}

// ============================================================================
// Symbols (/api/v1/symbols)
// ============================================================================

export type SymbolScope = "project" | "public";

export interface SymbolRead {
  id: string;
  project_id: string | null;
  app_version: string | null;
  scope?: SymbolScope;
  source?: string;
  module_name: string;
  debug_file: string;
  debug_id: string;
  code_file: string | null;
  code_id: string | null;
  has_pe_unwind: boolean;
  sym_size: number;
  raw_size: number;
  last_used_at: string | null;
  created_at?: string;
  sym_path?: string | null;
  raw_archive_path?: string | null;
}

export interface SymbolUploadResponse extends SymbolRead {
  deduplicated: boolean;
  upgraded?: boolean;
  resymbolicate_queued_count?: number;
  resymbolicate_crash_ids?: string[];
  resymbolicate_skipped_active_count?: number;
  resymbolicate_active_crash_ids?: string[];
}

export interface SymbolListResponse {
  items: SymbolRead[];
  total: number;
}

export interface MissingSymbolListResult {
  items: MissingSymbolQueueItem[];
  total: number;
}

export interface MissingSymbolQueueItem {
  debug_file: string;
  debug_id: string;
  module_name: string;
  code_file: string | null;
  code_id: string | null;
  crash_count: number;
  latest_crash_at: string;
  affected_crash_ids: string[];
  is_windows_system?: boolean;
  is_available_locally?: boolean;
  local_scope?: SymbolScope | null;
  msdl_negative_cached?: boolean;
  msdl_last_reason?: string | null;
}

export interface PublicSymbolSyncTarget {
  module_name?: string | null;
  debug_file?: string | null;
  debug_id: string;
  age?: string | number | null;
  code_file?: string | null;
  code_id?: string | null;
}

export interface PublicSymbolSyncRequest {
  mode: "targets" | "missing_windows";
  targets?: PublicSymbolSyncTarget[];
  project_id?: string;
  force?: boolean;
  auto_resymbolicate?: boolean;
  max_targets?: number;
}

export type PublicSymbolSyncItemStatus =
  | "imported"
  | "upgraded"
  | "cached_hit"
  | "not_found"
  | "network_error"
  | "skipped"
  | "failed";

export interface PublicSymbolSyncItemResult {
  module_name: string;
  debug_file: string;
  debug_id: string;
  status: PublicSymbolSyncItemStatus;
  detail: string | null;
  has_pe_unwind: boolean;
  symbol_id: string | null;
}

export interface PublicSymbolSyncResponse {
  attempted: number;
  imported: number;
  upgraded: number;
  cached_hit: number;
  not_found: number;
  skipped: number;
  failed: number;
  circuit_open: boolean;
  resymbolicate_queued_count: number;
  resymbolicate_crash_ids: string[];
  resymbolicate_skipped_active_count?: number;
  resymbolicate_active_crash_ids?: string[];
  results: PublicSymbolSyncItemResult[];
}

export interface PublicSymbolSyncStreamCurrent {
  module_name: string;
  debug_file: string;
  debug_id: string;
}

export interface PublicSymbolSyncStreamCounters {
  total: number;
  completed: number;
  already_cached: number;
  imported: number;
  upgraded: number;
  cached_hit: number;
  not_found: number;
  skipped: number;
  failed: number;
  circuit_open?: boolean;
  active_modules?: string[];
}

export type PublicSymbolSyncStreamEvent =
  | ({ type: "start" } & PublicSymbolSyncStreamCounters)
  | ({
      type: "item_start";
      current: PublicSymbolSyncStreamCurrent;
    } & PublicSymbolSyncStreamCounters)
  | ({
      type: "progress";
      item: PublicSymbolSyncItemResult;
    } & PublicSymbolSyncStreamCounters)
  | {
      type: "complete";
      total: number;
      completed: number;
      already_cached: number;
      summary: PublicSymbolSyncResponse;
    }
  | {
      type: "error";
      detail: string;
    };

export interface ResymbolicateRequest {
  project_id?: string;
  debug_id?: string;
  crash_ids?: string[];
  missing_only?: boolean;
}

export interface ResymbolicateResponse {
  queued_count: number;
  crash_ids: string[];
  skipped_active_count?: number;
  active_crash_ids?: string[];
}

export interface ResymbolicateStatusResponse {
  total: number;
  pending: number;
  parsing: number;
  triaging: number;
  analyzed: number;
  failed: number;
  completed: number;
  in_progress: number;
  done: boolean;
}

// ============================================================================
// Parsed Stacks & Missing Symbols (CrashReport JSONB payloads)
// ============================================================================

export interface InlineFrame {
  function?: string | null;
  module?: string | null;
  file?: string | null;
  line?: number | null;
  function_offset?: string | number | null;
  missing_symbols?: boolean;
}

export interface StackFrame {
  frame_index: number;
  frame?: number;
  ip?: string | null;
  module?: string | null;
  module_offset?: string | number | null;
  function?: string | null;
  function_offset?: string | number | null;
  file?: string | null;
  line?: number | null;
  trust?: string | null;
  missing_symbols?: boolean;
  is_inlined?: boolean;
  is_noise?: boolean;
  inlines?: InlineFrame[];
  registers?: Record<string, string> | null;
}

export interface StackThread {
  thread_index: number;
  id?: number;
  thread_id?: number | null;
  thread_name?: string | null;
  is_crashing?: boolean;
  frames: StackFrame[];
  registers?: Record<string, string> | null;
}

export interface LoadedModule {
  filename: string;
  module_name?: string | null;
  base_addr?: string | null;
  end_addr?: string | null;
  version?: string | null;
  debug_file?: string | null;
  debug_id?: string | null;
  code_file?: string | null;
  code_id?: string | null;
  loaded_symbols?: boolean;
  corrupt_symbols?: boolean;
  missing_symbols?: boolean;
}

export interface MissingSymbolInfo {
  module_name?: string | null;
  debug_file: string;
  debug_id: string;
  code_file?: string | null;
  code_id?: string | null;
}

export interface ParsedStacks {
  crash_reason?: string | null;
  crash_address?: string | null;
  os_version?: string | null;
  cpu_arch?: string | null;
  crashing_thread_index?: number;
  threads: StackThread[];
  modules?: LoadedModule[];
  missing_symbols?: MissingSymbolInfo[];
}

export interface ParsedSummary {
  crash_reason?: string | null;
  crash_address?: string | null;
  os_version?: string | null;
  cpu_arch?: string | null;
  crashing_thread_index?: number;
  top_frames?: StackFrame[];
  missing_symbol_count?: number;
  thread_count?: number;
  module_count?: number;
}

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
  digest_json?: LogDigest | null;
}

export interface LogSummaryResponse {
  id: string;
  crash_id: string;
  filename: string;
  raw_size: number;
  compressed_size: number;
  total_lines: number;
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

// ============================================================================
// AI Analysis
// ============================================================================

export interface EvidenceLogSnippet {
  log_id: string;
  start_line: number;
  end_line: number;
  relevance: string;
}

export interface AIAnalysisResult {
  verdict: AIVerdict;
  matched_issue_id: number | null;
  confidence: number | null;
  suggested_title: string;
  root_cause_analysis: string;
  reasoning_summary: string;
  evidence_log_snippets: EvidenceLogSnippet[];
  key_log_patterns: string[];
  suggested_component?: string | null;
}

export interface AITraceStep {
  step: number;
  role?: string;
  tool_name?: string | null;
  arguments?: Record<string, unknown> | string | null;
  output_preview?: string | null;
  error?: string | null;
  duration_ms?: number | null;
  message?: string | null;
}

export interface AIAnalysisRunRead {
  id: string;
  crash_id: string;
  model_name: string;
  status: AIRunStatus;
  result_json: AIAnalysisResult | null;
  trace_json: AITraceStep[];
  prompt_tokens: number;
  completion_tokens: number;
  duration_ms: number;
  created_at: string;
}

// ============================================================================
// Issues & Clusters (/api/v1/issues)
// ============================================================================

export type SignatureFrameItem =
  | string
  | {
      module?: string | null;
      function?: string | null;
      [key: string]: unknown;
    };

export interface CrashClusterRead {
  id: string;
  project_id: string;
  issue_id: number | null;
  signature_hash: string;
  signature_frames_json: SignatureFrameItem[];
  signature_text?: string | null;
  exception_type: string | null;
  crash_count: number;
}

export interface ClusterListResponse {
  items: CrashClusterRead[];
  total: number;
}

export interface IssueRead {
  id: number;
  project_id: string;
  title: string;
  status: IssueStatus;
  component: string | null;
  root_cause: string | null;
  log_patterns_json: string[];
  fixed_in_version: string | null;
  is_ambiguous_stack: boolean;
  is_pinned: boolean;
  crash_count: number;
  first_seen_at: string;
  last_seen_at: string;
}

export interface IssueListResponse {
  items: IssueRead[];
  total: number;
}

export interface IssueCreateRequest {
  project_id: string;
  title: string;
  status?: IssueStatus;
  component?: string | null;
  root_cause?: string | null;
  log_patterns_json?: string[];
  fixed_in_version?: string | null;
  is_ambiguous_stack?: boolean;
  is_pinned?: boolean;
}

export interface IssueUpdateRequest {
  title?: string;
  status?: IssueStatus;
  component?: string | null;
  root_cause?: string | null;
  log_patterns_json?: string[];
  fixed_in_version?: string | null;
  is_ambiguous_stack?: boolean;
  is_pinned?: boolean;
}

export interface IssueMergeRequest {
  target_issue_id: number;
  source_issue_ids: number[];
}

export interface IssueSplitRequest {
  cluster_id: string;
  target_issue_id?: number;
  new_issue_title?: string;
}

export interface IssueSplitResponse {
  source_issue: IssueRead;
  target_issue: IssueRead;
  cluster: CrashClusterRead;
}

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

export interface IssueDetailRead extends IssueRead {
  clusters: CrashClusterRead[];
  recent_crashes: CrashSummary[];
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

// ============================================================================
// Settings, Storage & GC (/api/v1/settings)
// ============================================================================

export interface SystemSettingsRead {
  llm_provider: LLMProviderId;
  llm_base_url: string;
  llm_api_key_set: boolean;
  llm_api_key_masked: string | null;
  llm_model: string;
  llm_thinking_level: LLMThinkingLevel;
  ai_triage_mode: AITriageMode;
  ai_output_language: AIOutputLanguage;
  ai_max_steps: number;
  ai_timeout_seconds: number;
  ai_tool_output_max_chars: number;
  total_disk_quota_gb: number;
  host_safety_floor_gb: number;
  db_quota_gb: number;
  symbols_sym_quota_gb: number;
  symbols_public_cache_quota_gb: number;
  symbols_raw_quota_gb: number;
  logs_quota_gb: number;
  dumps_quota_gb: number;
  staging_quota_gb: number;
  gc_threshold_percent: number;
  log_retention_days: number;
  dump_retention_days: number;
  staging_ttl_hours: number;
  archive_raw_symbols: boolean;
  enable_public_symbols: boolean;
  public_symbol_urls: string[];
  default_noise_modules: string[];
  default_noise_functions: string[];
}

export interface SystemSettingsUpdate {
  llm_provider?: LLMProviderId;
  llm_base_url?: string;
  llm_api_key?: string;
  llm_model?: string;
  llm_thinking_level?: LLMThinkingLevel;
  ai_triage_mode?: AITriageMode;
  ai_output_language?: AIOutputLanguage;
  ai_max_steps?: number;
  ai_timeout_seconds?: number;
  ai_tool_output_max_chars?: number;
  total_disk_quota_gb?: number;
  host_safety_floor_gb?: number;
  db_quota_gb?: number;
  symbols_sym_quota_gb?: number;
  symbols_public_cache_quota_gb?: number;
  symbols_raw_quota_gb?: number;
  logs_quota_gb?: number;
  dumps_quota_gb?: number;
  staging_quota_gb?: number;
  gc_threshold_percent?: number;
  log_retention_days?: number;
  dump_retention_days?: number;
  staging_ttl_hours?: number;
  archive_raw_symbols?: boolean;
  enable_public_symbols?: boolean;
  public_symbol_urls?: string[];
  default_noise_modules?: string[];
  default_noise_functions?: string[];
}

export interface StoragePartitionUsage {
  name?: string;
  used_bytes: number;
  quota_bytes: number;
  usage_percent: number;
  used_gb?: number;
  quota_gb?: number;
}

export interface StorageReport {
  partitions: Record<string, StoragePartitionUsage> | StoragePartitionUsage[];
  total_used_bytes: number;
  total_quota_bytes: number;
  total_usage_percent?: number;
  total_used_gb?: number;
  total_quota_gb?: number;
  host_free_bytes?: number;
  host_free_gb: number;
  host_total_gb?: number;
  host_safety_floor_gb?: number;
  safety_floor_breached: boolean;
}

export interface GCReport {
  freed_bytes: number;
  deleted_files_count: number;
  skipped_pinned_count: number;
  emergency_triggered: boolean;
  partitions:
    | Record<string, { freed_bytes?: number; deleted_files_count?: number; [key: string]: unknown }>
    | Array<Record<string, unknown>>;
}

// ============================================================================
// Dashboard (/api/v1/dashboard)
// ============================================================================

export interface DashboardKPIs {
  total_crashes: number;
  analyzed_crashes: number;
  pending_crashes: number;
  open_issues: number;
  missing_symbols_count: number;
  failed_crashes?: number;
  total_issues?: number;
  total_symbols?: number;
}

export interface CrashTrendPoint {
  date: string;
  count: number;
  analyzed?: number;
  failed?: number;
}

export interface VerdictDistributionItem {
  verdict: string;
  count: number;
}

export interface IssueStatusDistributionItem {
  status: string;
  count: number;
}

export interface DashboardResponse {
  kpis: DashboardKPIs;
  crash_trend: CrashTrendPoint[];
  verdict_distribution: VerdictDistributionItem[];
  issue_status_distribution: IssueStatusDistributionItem[];
  top_missing_symbols: MissingSymbolQueueItem[];
  storage: StorageReport;
}
