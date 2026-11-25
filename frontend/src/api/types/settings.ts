import type {
  AIOutputLanguage,
  AITriageMode,
  LLMProviderId,
  LLMThinkingLevel,
} from "./common";

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
    | Record<
        string,
        { freed_bytes?: number; deleted_files_count?: number; [key: string]: unknown }
      >
    | Array<Record<string, unknown>>;
}
