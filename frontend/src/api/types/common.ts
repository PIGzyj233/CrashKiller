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

export type IssueStatusEventSource =
  | "manual"
  | "batch"
  | "form_edit"
  | "ai_triage"
  | "ai_regression"
  | "human_verdict"
  | "split";

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
