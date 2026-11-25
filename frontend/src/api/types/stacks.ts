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
