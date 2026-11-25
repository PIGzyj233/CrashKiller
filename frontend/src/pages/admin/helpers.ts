import type {
  AITriageMode,
  GCReport,
  LLMProviderPreset,
  LLMThinkingLevel,
} from "../../api/types";

export const AI_TRIAGE_MODES: readonly AITriageMode[] = [
  "SMART_TRIAGE",
  "ALWAYS",
  "MANUAL",
];

export const THINKING_LEVELS: readonly LLMThinkingLevel[] = [
  "auto",
  "off",
  "low",
  "medium",
  "high",
];

export const CUSTOM_MODEL_SENTINEL = "__custom__";

export const FALLBACK_PROVIDER_PRESETS: readonly LLMProviderPreset[] = [
  {
    id: "openai",
    name: "OpenAI",
    protocol: "openai_compatible",
    default_base_url: "https://api.openai.com/v1",
    default_model: "gpt-4o-mini",
    models: [
      "gpt-5",
      "gpt-5-mini",
      "o4-mini",
      "o3",
      "gpt-4.1",
      "gpt-4o",
      "gpt-4o-mini",
    ],
    requires_api_key: true,
    supports_thinking: true,
  },
  {
    id: "anthropic",
    name: "Anthropic (Claude)",
    protocol: "anthropic_messages",
    default_base_url: "https://api.anthropic.com",
    default_model: "claude-sonnet-4-5",
    models: [
      "claude-opus-4-1",
      "claude-sonnet-4-5",
      "claude-sonnet-4-0",
      "claude-3-7-sonnet-latest",
      "claude-3-5-haiku-latest",
    ],
    requires_api_key: true,
    supports_thinking: true,
  },
  {
    id: "gemini",
    name: "Google Gemini",
    protocol: "openai_compatible",
    default_base_url:
      "https://generativelanguage.googleapis.com/v1beta/openai/",
    default_model: "gemini-2.5-flash",
    models: ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"],
    requires_api_key: true,
    supports_thinking: true,
  },
  {
    id: "deepseek",
    name: "DeepSeek",
    protocol: "openai_compatible",
    default_base_url: "https://api.deepseek.com/v1",
    default_model: "deepseek-chat",
    models: ["deepseek-chat", "deepseek-reasoner"],
    requires_api_key: true,
    supports_thinking: true,
  },
  {
    id: "openrouter",
    name: "OpenRouter",
    protocol: "openai_compatible",
    default_base_url: "https://openrouter.ai/api/v1",
    default_model: "openai/gpt-5-mini",
    models: [
      "openai/gpt-5",
      "openai/gpt-5-mini",
      "anthropic/claude-sonnet-4.5",
      "google/gemini-2.5-pro",
      "deepseek/deepseek-r1",
    ],
    requires_api_key: true,
    supports_thinking: true,
  },
  {
    id: "qwen",
    name: "Alibaba DashScope (Qwen)",
    protocol: "openai_compatible",
    default_base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    default_model: "qwen-plus",
    models: ["qwen3-max", "qwen-plus", "qwen-turbo", "qwq-plus"],
    requires_api_key: true,
    supports_thinking: true,
  },
  {
    id: "zhipu",
    name: "Zhipu AI (GLM)",
    protocol: "openai_compatible",
    default_base_url: "https://open.bigmodel.cn/api/paas/v4",
    default_model: "glm-4.5",
    models: ["glm-4.5", "glm-4.5-air", "glm-4-plus"],
    requires_api_key: true,
    supports_thinking: true,
  },
  {
    id: "ollama",
    name: "Ollama (Local)",
    protocol: "openai_compatible",
    default_base_url: "http://host.docker.internal:11434/v1",
    default_model: "qwen3:14b",
    models: ["qwen3:14b", "deepseek-r1:14b", "llama3.1:8b"],
    requires_api_key: false,
    supports_thinking: true,
  },
  {
    id: "custom",
    name: "Custom OpenAI-Compatible",
    protocol: "openai_compatible",
    default_base_url: "https://api.openai.com/v1",
    default_model: "gpt-4o-mini",
    models: [],
    requires_api_key: false,
    supports_thinking: true,
  },
];

export interface RevealedApiKeyInfo {
  projectKey: string;
  projectName: string;
  apiKey: string;
  action: "created" | "rotated";
}

export function parseListInput(raw: string): string[] {
  return raw
    .split(/[\r\n,]+/)
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
}

export function parseLineInput(raw: string): string[] {
  return raw
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
}

export function maskProjectApiKey(rawKey: string): string {
  const trimmed = rawKey.trim();
  if (!trimmed) {
    return "";
  }
  if (trimmed.length <= 10) {
    return "•".repeat(Math.max(8, trimmed.length));
  }
  return `${trimmed.slice(0, 6)}${"•".repeat(14)}${trimmed.slice(-4)}`;
}

export async function copyTextToClipboard(text: string): Promise<boolean> {
  if (!text) {
    return false;
  }
  if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Fall through to execCommand fallback for non-HTTPS origins
    }
  }
  if (typeof document === "undefined") {
    return false;
  }
  try {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.top = "-9999px";
    textarea.style.left = "-9999px";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    textarea.setSelectionRange(0, text.length);
    const ok = document.execCommand("copy");
    document.body.removeChild(textarea);
    return ok;
  } catch {
    return false;
  }
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) {
    return "0 B";
  }
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  if (bytes < 1024 ** 3) {
    return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
  }
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

export interface NormalizedGCPartitionRow {
  name: string;
  usedBytes?: number;
  quotaBytes?: number;
  usagePercent?: number;
  fileCount?: number;
  freedBytes?: number;
  deletedFilesCount?: number;
}

function readOptionalNumber(
  obj: Record<string, unknown>,
  key: string,
): number | undefined {
  const val = obj[key];
  return typeof val === "number" && Number.isFinite(val) ? val : undefined;
}

export function normalizeGCPartitions(
  partitions: GCReport["partitions"] | null | undefined,
): NormalizedGCPartitionRow[] {
  if (!partitions || typeof partitions !== "object") {
    return [];
  }
  if (Array.isArray(partitions)) {
    return partitions.map((entry, idx) => {
      const record =
        typeof entry === "object" && entry !== null
          ? (entry as Record<string, unknown>)
          : {};
      return {
        name:
          typeof record.name === "string"
            ? record.name
            : typeof record.partition === "string"
              ? record.partition
              : `partition-${idx}`,
        usedBytes: readOptionalNumber(record, "used_bytes"),
        quotaBytes: readOptionalNumber(record, "quota_bytes"),
        usagePercent: readOptionalNumber(record, "usage_percent"),
        fileCount: readOptionalNumber(record, "file_count"),
        freedBytes: readOptionalNumber(record, "freed_bytes"),
        deletedFilesCount: readOptionalNumber(record, "deleted_files_count"),
      };
    });
  }

  return Object.entries(partitions).map(([name, val]) => {
    const record =
      typeof val === "object" && val !== null
        ? (val as Record<string, unknown>)
        : {};
    return {
      name,
      usedBytes: readOptionalNumber(record, "used_bytes"),
      quotaBytes: readOptionalNumber(record, "quota_bytes"),
      usagePercent: readOptionalNumber(record, "usage_percent"),
      fileCount: readOptionalNumber(record, "file_count"),
      freedBytes: readOptionalNumber(record, "freed_bytes"),
      deletedFilesCount: readOptionalNumber(record, "deleted_files_count"),
    };
  });
}
