import type { AIRunStatus, AIVerdict } from "./common";

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
