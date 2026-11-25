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
