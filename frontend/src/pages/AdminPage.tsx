import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Brain,
  Check,
  CheckCircle2,
  Copy,
  Cpu,
  Eye,
  EyeOff,
  HardDrive,
  KeyRound,
  Loader2,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  ShieldCheck,
  Sliders,
  Trash2,
  X,
} from "lucide-react";
import {
  ApiError,
  createProject,
  getProjectApiKey,
  getStoredProjectApiKey,
  getSystemSettings,
  listLLMProviders,
  removeStoredProjectApiKey,
  rotateProjectKey,
  setStoredProjectApiKey,
  syncProjectApiKey,
  triggerGC,
  updateProject,
  updateSystemSettings,
} from "../api/client";
import type {
  AIOutputLanguage,
  AITriageMode,
  GCReport,
  LLMProviderId,
  LLMProviderPreset,
  LLMThinkingLevel,
  ProjectRead,
  ProjectSettings,
  SystemSettingsUpdate,
} from "../api/types";
import { useProjectContext } from "../components/Layout";
import { useAppI18n } from "../i18n";

const AI_TRIAGE_MODES: readonly AITriageMode[] = [
  "SMART_TRIAGE",
  "ALWAYS",
  "MANUAL",
];

const THINKING_LEVELS: readonly LLMThinkingLevel[] = [
  "auto",
  "off",
  "low",
  "medium",
  "high",
];

const CUSTOM_MODEL_SENTINEL = "__custom__";

const FALLBACK_PROVIDER_PRESETS: readonly LLMProviderPreset[] = [
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

function parseListInput(raw: string): string[] {
  return raw
    .split(/[\r\n,]+/)
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
}

function parseLineInput(raw: string): string[] {
  return raw
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
}

function maskProjectApiKey(rawKey: string): string {
  const trimmed = rawKey.trim();
  if (!trimmed) {
    return "";
  }
  if (trimmed.length <= 10) {
    return "•".repeat(Math.max(8, trimmed.length));
  }
  return `${trimmed.slice(0, 6)}${"•".repeat(14)}${trimmed.slice(-4)}`;
}

async function copyTextToClipboard(text: string): Promise<boolean> {
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

function formatBytes(bytes: number): string {
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

interface NormalizedGCPartitionRow {
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

function normalizeGCPartitions(
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

export function AdminPage() {
  const { t, formatNumber, formatTriageMode, formatThinkingLevel } =
    useAppI18n();
  const { projects, selectedProjectId, refreshProjects } = useProjectContext();
  const queryClient = useQueryClient();

  // Revealed once API Key banner
  const [revealedKey, setRevealedKey] = useState<{
    projectKey: string;
    projectName: string;
    apiKey: string;
    action: "created" | "rotated";
  } | null>(null);
  const [copiedKey, setCopiedKey] = useState(false);

  // Create Project form
  const [newProjectKey, setNewProjectKey] = useState("");
  const [newProjectName, setNewProjectName] = useState("");
  const [newProjectTriageMode, setNewProjectTriageMode] = useState<
    AITriageMode | ""
  >("");
  const [newProjectSessionIdLabel, setNewProjectSessionIdLabel] = useState("");

  // Edit Project state
  const [editingProjectId, setEditingProjectId] = useState<string>("");
  const [confirmRotateProjectId, setConfirmRotateProjectId] = useState<
    string | null
  >(null);
  const [showEditorApiKey, setShowEditorApiKey] = useState(false);
  const [visibleCardKeyIds, setVisibleCardKeyIds] = useState<
    Record<string, boolean>
  >({});
  const [resolvedProjectKeys, setResolvedProjectKeys] = useState<
    Record<string, string | null>
  >({});
  const [loadingCardKeyId, setLoadingCardKeyId] = useState<string | null>(null);
  const [copiedProjectKeyTarget, setCopiedProjectKeyTarget] = useState<
    string | null
  >(null);
  const [projectKeyError, setProjectKeyError] = useState<{
    projectId: string;
    message: string;
  } | null>(null);
  const [editName, setEditName] = useState("");
  const [editAiTriageMode, setEditAiTriageMode] = useState<AITriageMode | "">(
    "",
  );
  const [editSessionIdLabel, setEditSessionIdLabel] = useState("");
  const [editNoiseModulesText, setEditNoiseModulesText] = useState("");
  const [editNoiseFunctionsText, setEditNoiseFunctionsText] = useState("");
  const [editCustomHandlersText, setEditCustomHandlersText] = useState("");
  const [editLogRetentionDays, setEditLogRetentionDays] = useState("");
  const [editDumpRetentionDays, setEditDumpRetentionDays] = useState("");
  const [projectSavedMsg, setProjectSavedMsg] = useState<string | null>(null);

  // System Settings & LLM Providers queries
  const settingsQuery = useQuery({
    queryKey: ["system-settings"],
    queryFn: getSystemSettings,
  });

  const providersQuery = useQuery({
    queryKey: ["llm-providers"],
    queryFn: listLLMProviders,
    staleTime: 5 * 60_000,
  });

  const providerPresets = useMemo<readonly LLMProviderPreset[]>(
    () =>
      providersQuery.data && providersQuery.data.length > 0
        ? providersQuery.data
        : FALLBACK_PROVIDER_PRESETS,
    [providersQuery.data],
  );

  // LLM & AI Settings state
  const [llmProvider, setLlmProvider] = useState<LLMProviderId>("openai");
  const [llmBaseUrl, setLlmBaseUrl] = useState("https://api.openai.com/v1");
  const [llmModel, setLlmModel] = useState("gpt-4o-mini");
  const [useCustomModelInput, setUseCustomModelInput] = useState(false);
  const [llmThinkingLevel, setLlmThinkingLevel] =
    useState<LLMThinkingLevel>("auto");
  const [llmApiKeyInput, setLlmApiKeyInput] = useState("");
  const [showLlmApiKey, setShowLlmApiKey] = useState(false);
  const [aiTriageMode, setAiTriageMode] =
    useState<AITriageMode>("SMART_TRIAGE");
  const [aiOutputLanguage, setAiOutputLanguage] =
    useState<AIOutputLanguage>("zh-CN");
  const [aiMaxSteps, setAiMaxSteps] = useState("10");
  const [aiTimeoutSeconds, setAiTimeoutSeconds] = useState("120");
  const [aiToolOutputMaxChars, setAiToolOutputMaxChars] = useState("8000");

  // Quotas & Retention state
  const [totalDiskQuotaGb, setTotalDiskQuotaGb] = useState("150");
  const [hostSafetyFloorGb, setHostSafetyFloorGb] = useState("50");
  const [gcThresholdPercent, setGcThresholdPercent] = useState("85");
  const [dbQuotaGb, setDbQuotaGb] = useState("15");
  const [symbolsSymQuotaGb, setSymbolsSymQuotaGb] = useState("15");
  const [symbolsPublicCacheQuotaGb, setSymbolsPublicCacheQuotaGb] =
    useState("5");
  const [symbolsRawQuotaGb, setSymbolsRawQuotaGb] = useState("15");
  const [logsQuotaGb, setLogsQuotaGb] = useState("55");
  const [dumpsQuotaGb, setDumpsQuotaGb] = useState("35");
  const [stagingQuotaGb, setStagingQuotaGb] = useState("10");

  const [logRetentionDays, setLogRetentionDays] = useState("30");
  const [dumpRetentionDays, setDumpRetentionDays] = useState("14");
  const [stagingTtlHours, setStagingTtlHours] = useState("6");
  const [archiveRawSymbols, setArchiveRawSymbols] = useState(false);
  const [enablePublicSymbols, setEnablePublicSymbols] = useState(true);
  const [publicSymbolUrlsText, setPublicSymbolUrlsText] = useState("");
  const [defaultNoiseModulesText, setDefaultNoiseModulesText] = useState("");
  const [defaultNoiseFunctionsText, setDefaultNoiseFunctionsText] =
    useState("");
  const [settingsSavedMsg, setSettingsSavedMsg] = useState<string | null>(null);
  const [settingsValidationError, setSettingsValidationError] = useState<
    string | null
  >(null);

  // Manual GC state
  const [forceEmergencyGc, setForceEmergencyGc] = useState(false);
  const [gcReport, setGcReport] = useState<GCReport | null>(null);

  const activeProviderPreset = useMemo<LLMProviderPreset>(
    () =>
      providerPresets.find((p) => p.id === llmProvider) ?? providerPresets[0],
    [providerPresets, llmProvider],
  );

  // Pick initial editing project
  useEffect(() => {
    if (projects.length === 0) {
      setEditingProjectId("");
      return;
    }
    if (
      !editingProjectId ||
      !projects.some((p) => p.id === editingProjectId)
    ) {
      setEditingProjectId(
        selectedProjectId && projects.some((p) => p.id === selectedProjectId)
          ? selectedProjectId
          : projects[0].id,
      );
    }
  }, [projects, selectedProjectId, editingProjectId]);

  const editingProject = useMemo(
    () => projects.find((p) => p.id === editingProjectId),
    [projects, editingProjectId],
  );

  const fetchAndResolveProjectApiKey = async (
    proj: ProjectRead,
  ): Promise<string | null> => {
    const localKey = getStoredProjectApiKey(proj.key);
    try {
      const res = await getProjectApiKey(proj.id);
      if (res.api_key) {
        setStoredProjectApiKey(proj.key, res.api_key);
        return res.api_key;
      }
      if (localKey) {
        try {
          const synced = await syncProjectApiKey(proj.id, localKey);
          if (synced.api_key) {
            setStoredProjectApiKey(proj.key, synced.api_key);
            return synced.api_key;
          }
        } catch (syncErr) {
          if (syncErr instanceof ApiError && syncErr.status === 400) {
            removeStoredProjectApiKey(proj.key);
            return null;
          }
          return localKey;
        }
      }
      return null;
    } catch (err) {
      if (
        localKey &&
        err instanceof ApiError &&
        (err.status === 404 || err.status === 405)
      ) {
        return localKey;
      }
      throw err;
    }
  };

  const editingProjectKeyQuery = useQuery({
    queryKey: ["project-api-key", editingProject?.id],
    queryFn: () => fetchAndResolveProjectApiKey(editingProject!),
    enabled: Boolean(editingProject),
    staleTime: 60_000,
  });

  useEffect(() => {
    if (!editingProject || editingProjectKeyQuery.data === undefined) {
      return;
    }
    setResolvedProjectKeys((prev) =>
      prev[editingProject.id] === editingProjectKeyQuery.data
        ? prev
        : { ...prev, [editingProject.id]: editingProjectKeyQuery.data },
    );
  }, [editingProject, editingProjectKeyQuery.data]);

  const syncedProjectIdRef = useRef<string | null>(null);

  const syncProjectForm = (proj: ProjectRead) => {
    const s = proj.settings_json ?? {};
    setEditName(proj.name);
    setEditAiTriageMode(s.ai_triage_mode ?? "");
    setEditSessionIdLabel(
      typeof s.session_id_label === "string" ? s.session_id_label : "",
    );
    setEditNoiseModulesText((s.noise_modules ?? []).join("\n"));
    setEditNoiseFunctionsText((s.noise_functions ?? []).join("\n"));
    setEditCustomHandlersText((s.custom_crash_handlers ?? []).join("\n"));
    setEditLogRetentionDays(
      typeof s.log_retention_days === "number"
        ? String(s.log_retention_days)
        : "",
    );
    setEditDumpRetentionDays(
      typeof s.dump_retention_days === "number"
        ? String(s.dump_retention_days)
        : "",
    );
  };

  useEffect(() => {
    if (!editingProject) {
      syncedProjectIdRef.current = null;
      setProjectSavedMsg(null);
      return;
    }
    if (syncedProjectIdRef.current !== editingProject.id) {
      syncedProjectIdRef.current = editingProject.id;
      syncProjectForm(editingProject);
      setProjectSavedMsg(null);
      setShowEditorApiKey(false);
    }
  }, [editingProject]);

  // Hydrate global settings form
  useEffect(() => {
    const s = settingsQuery.data;
    if (!s) {
      return;
    }
    const providerId = s.llm_provider ?? "openai";
    setLlmProvider(providerId);
    setLlmBaseUrl(s.llm_base_url);
    setLlmModel(s.llm_model);
    setLlmThinkingLevel(s.llm_thinking_level ?? "auto");

    const preset = providerPresets.find((p) => p.id === providerId);
    setUseCustomModelInput(
      !preset || preset.models.length === 0 || !preset.models.includes(s.llm_model),
    );

    setAiTriageMode(s.ai_triage_mode);
    setAiOutputLanguage(s.ai_output_language ?? "zh-CN");
    setAiMaxSteps(String(s.ai_max_steps));
    setAiTimeoutSeconds(String(s.ai_timeout_seconds));
    setAiToolOutputMaxChars(String(s.ai_tool_output_max_chars));
    setTotalDiskQuotaGb(String(s.total_disk_quota_gb));
    setHostSafetyFloorGb(String(s.host_safety_floor_gb));
    setGcThresholdPercent(String(s.gc_threshold_percent));
    setDbQuotaGb(String(s.db_quota_gb));
    setSymbolsSymQuotaGb(String(s.symbols_sym_quota_gb));
    setSymbolsPublicCacheQuotaGb(String(s.symbols_public_cache_quota_gb));
    setSymbolsRawQuotaGb(String(s.symbols_raw_quota_gb));
    setLogsQuotaGb(String(s.logs_quota_gb));
    setDumpsQuotaGb(String(s.dumps_quota_gb));
    setStagingQuotaGb(String(s.staging_quota_gb));
    setLogRetentionDays(String(s.log_retention_days));
    setDumpRetentionDays(String(s.dump_retention_days));
    setStagingTtlHours(String(s.staging_ttl_hours));
    setArchiveRawSymbols(s.archive_raw_symbols);
    setEnablePublicSymbols(s.enable_public_symbols);
    setPublicSymbolUrlsText((s.public_symbol_urls ?? []).join("\n"));
    setDefaultNoiseModulesText((s.default_noise_modules ?? []).join("\n"));
    setDefaultNoiseFunctionsText((s.default_noise_functions ?? []).join("\n"));
  }, [settingsQuery.data, providerPresets]);

  const handleProviderChange = (nextId: LLMProviderId) => {
    setLlmProvider(nextId);
    const nextPreset = providerPresets.find((p) => p.id === nextId);
    if (!nextPreset) {
      return;
    }
    setLlmBaseUrl(nextPreset.default_base_url);
    if (nextPreset.models.length > 0) {
      if (!nextPreset.models.includes(llmModel)) {
        setLlmModel(nextPreset.default_model);
      }
      setUseCustomModelInput(false);
    } else {
      setUseCustomModelInput(true);
    }
  };

  const handleModelSelectChange = (val: string) => {
    if (val === CUSTOM_MODEL_SENTINEL) {
      setUseCustomModelInput(true);
      return;
    }
    setUseCustomModelInput(false);
    setLlmModel(val);
  };

  const createProjectMutation = useMutation({
    mutationFn: createProject,
    onSuccess: (created) => {
      setStoredProjectApiKey(created.key, created.api_key);
      queryClient.setQueryData(["project-api-key", created.id], created.api_key);
      setResolvedProjectKeys((prev) => ({
        ...prev,
        [created.id]: created.api_key,
      }));
      setRevealedKey({
        projectKey: created.key,
        projectName: created.name,
        apiKey: created.api_key,
        action: "created",
      });
      setCopiedKey(false);
      setNewProjectKey("");
      setNewProjectName("");
      setNewProjectTriageMode("");
      setNewProjectSessionIdLabel("");
      setEditingProjectId(created.id);
      refreshProjects();
    },
  });

  const updateProjectMutation = useMutation({
    mutationFn: ({
      projectId,
      name,
      settings_json,
    }: {
      projectId: string;
      name: string;
      settings_json: ProjectSettings;
    }) => updateProject(projectId, { name, settings_json }),
    onSuccess: (updated) => {
      if (syncedProjectIdRef.current === updated.id) {
        syncProjectForm(updated);
        setProjectSavedMsg(
          t("admin.projects.savedMsg", { name: updated.name }),
        );
      }
      refreshProjects();
    },
  });

  const rotateKeyMutation = useMutation({
    mutationFn: (projectId: string) => rotateProjectKey(projectId),
    onSuccess: (rotated) => {
      setStoredProjectApiKey(rotated.key, rotated.api_key);
      queryClient.setQueryData(["project-api-key", rotated.id], rotated.api_key);
      setResolvedProjectKeys((prev) => ({
        ...prev,
        [rotated.id]: rotated.api_key,
      }));
      setProjectKeyError((prev) =>
        prev?.projectId === rotated.id ? null : prev,
      );
      const matched = projects.find((p) => p.id === rotated.id);
      setRevealedKey({
        projectKey: rotated.key,
        projectName: matched?.name ?? rotated.key,
        apiKey: rotated.api_key,
        action: "rotated",
      });
      setCopiedKey(false);
      setConfirmRotateProjectId(null);
      refreshProjects();
    },
  });

  const saveSettingsMutation = useMutation({
    mutationFn: (payload: SystemSettingsUpdate) =>
      updateSystemSettings(payload),
    onSuccess: (updated) => {
      queryClient.setQueryData(["system-settings"], updated);
      setLlmApiKeyInput("");
      setSettingsValidationError(null);
      setSettingsSavedMsg(t("admin.system.savedMsg"));
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  const gcMutation = useMutation({
    mutationFn: (forceEmergency: boolean) => triggerGC(forceEmergency),
    onSuccess: (report) => {
      setGcReport(report);
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  const handleCreateProject = (e: FormEvent) => {
    e.preventDefault();
    const key = newProjectKey.trim();
    const name = newProjectName.trim();
    if (!key || !name) {
      return;
    }
    const settings_json: ProjectSettings = {};
    if (newProjectTriageMode) {
      settings_json.ai_triage_mode = newProjectTriageMode;
    }
    if (newProjectSessionIdLabel.trim()) {
      settings_json.session_id_label = newProjectSessionIdLabel.trim();
    }
    createProjectMutation.mutate({ key, name, settings_json });
  };

  const handleSaveProject = (e: FormEvent) => {
    e.preventDefault();
    if (!editingProject || !editName.trim()) {
      return;
    }
    const nextSettings: ProjectSettings = {
      ...(editingProject.settings_json ?? {}),
      ai_triage_mode: editAiTriageMode || null,
      noise_modules: parseListInput(editNoiseModulesText),
      noise_functions: parseLineInput(editNoiseFunctionsText),
      custom_crash_handlers: parseLineInput(editCustomHandlersText),
    };

    if (editSessionIdLabel.trim()) {
      nextSettings.session_id_label = editSessionIdLabel.trim();
    } else {
      delete nextSettings.session_id_label;
    }

    const logRet = Number(editLogRetentionDays.trim());
    if (editLogRetentionDays.trim() && Number.isInteger(logRet) && logRet > 0) {
      nextSettings.log_retention_days = logRet;
    } else {
      delete nextSettings.log_retention_days;
    }

    const dumpRet = Number(editDumpRetentionDays.trim());
    if (
      editDumpRetentionDays.trim() &&
      Number.isInteger(dumpRet) &&
      dumpRet > 0
    ) {
      nextSettings.dump_retention_days = dumpRet;
    } else {
      delete nextSettings.dump_retention_days;
    }

    setProjectSavedMsg(null);
    updateProjectMutation.mutate({
      projectId: editingProject.id,
      name: editName.trim(),
      settings_json: nextSettings,
    });
  };

  const handleSaveGlobalSettings = (e: FormEvent) => {
    e.preventDefault();
    setSettingsSavedMsg(null);
    setSettingsValidationError(null);

    if (!llmBaseUrl.trim()) {
      setSettingsValidationError(t("admin.system.validationBaseUrlRequired"));
      return;
    }
    if (!llmModel.trim()) {
      setSettingsValidationError(t("admin.system.validationModelRequired"));
      return;
    }

    const numericFieldSpecs: Array<{
      field: string;
      raw: string;
      min: number;
      max?: number;
      exclusiveMin?: boolean;
      integer?: boolean;
    }> = [
      { field: "ai_max_steps", raw: aiMaxSteps, min: 1, max: 50, integer: true },
      {
        field: "ai_timeout_seconds",
        raw: aiTimeoutSeconds,
        min: 1,
        max: 3600,
        integer: true,
      },
      {
        field: "ai_tool_output_max_chars",
        raw: aiToolOutputMaxChars,
        min: 100,
        max: 200000,
        integer: true,
      },
      { field: "total_disk_quota_gb", raw: totalDiskQuotaGb, min: 0 },
      { field: "host_safety_floor_gb", raw: hostSafetyFloorGb, min: 0 },
      {
        field: "gc_threshold_percent",
        raw: gcThresholdPercent,
        min: 0,
        max: 100,
        exclusiveMin: true,
      },
      { field: "db_quota_gb", raw: dbQuotaGb, min: 0 },
      { field: "symbols_sym_quota_gb", raw: symbolsSymQuotaGb, min: 0 },
      {
        field: "symbols_public_cache_quota_gb",
        raw: symbolsPublicCacheQuotaGb,
        min: 0,
      },
      { field: "symbols_raw_quota_gb", raw: symbolsRawQuotaGb, min: 0 },
      { field: "logs_quota_gb", raw: logsQuotaGb, min: 0 },
      { field: "dumps_quota_gb", raw: dumpsQuotaGb, min: 0 },
      { field: "staging_quota_gb", raw: stagingQuotaGb, min: 0 },
      {
        field: "log_retention_days",
        raw: logRetentionDays,
        min: 1,
        max: 3650,
        integer: true,
      },
      {
        field: "dump_retention_days",
        raw: dumpRetentionDays,
        min: 1,
        max: 3650,
        integer: true,
      },
      {
        field: "staging_ttl_hours",
        raw: stagingTtlHours,
        min: 1,
        max: 720,
        integer: true,
      },
    ];

    const parsedNums: Record<string, number> = {};
    for (const {
      field,
      raw,
      min,
      max,
      exclusiveMin,
      integer,
    } of numericFieldSpecs) {
      if (raw.trim() === "") {
        setSettingsValidationError(
          t("admin.system.validationRequired", { field }),
        );
        return;
      }
      const val = Number(raw);
      if (!Number.isFinite(val)) {
        setSettingsValidationError(
          t("admin.system.validationFinite", { field }),
        );
        return;
      }
      if (integer && !Number.isInteger(val)) {
        setSettingsValidationError(
          t("admin.system.validationInteger", { field }),
        );
        return;
      }
      const belowMin = exclusiveMin ? val <= min : val < min;
      if (belowMin || (max !== undefined && val > max)) {
        const minOp = exclusiveMin ? ">" : ">=";
        setSettingsValidationError(
          max !== undefined
            ? t("admin.system.validationRange", { field, minOp, min, max })
            : t("admin.system.validationMin", { field, minOp, min }),
        );
        return;
      }
      parsedNums[field] = val;
    }

    const payload: SystemSettingsUpdate = {
      llm_provider: llmProvider,
      llm_base_url: llmBaseUrl.trim(),
      llm_model: llmModel.trim(),
      llm_thinking_level: llmThinkingLevel,
      ai_triage_mode: aiTriageMode,
      ai_output_language: aiOutputLanguage,
      ai_max_steps: parsedNums.ai_max_steps,
      ai_timeout_seconds: parsedNums.ai_timeout_seconds,
      ai_tool_output_max_chars: parsedNums.ai_tool_output_max_chars,
      total_disk_quota_gb: parsedNums.total_disk_quota_gb,
      host_safety_floor_gb: parsedNums.host_safety_floor_gb,
      gc_threshold_percent: parsedNums.gc_threshold_percent,
      db_quota_gb: parsedNums.db_quota_gb,
      symbols_sym_quota_gb: parsedNums.symbols_sym_quota_gb,
      symbols_public_cache_quota_gb: parsedNums.symbols_public_cache_quota_gb,
      symbols_raw_quota_gb: parsedNums.symbols_raw_quota_gb,
      logs_quota_gb: parsedNums.logs_quota_gb,
      dumps_quota_gb: parsedNums.dumps_quota_gb,
      staging_quota_gb: parsedNums.staging_quota_gb,
      log_retention_days: parsedNums.log_retention_days,
      dump_retention_days: parsedNums.dump_retention_days,
      staging_ttl_hours: parsedNums.staging_ttl_hours,
      archive_raw_symbols: archiveRawSymbols,
      enable_public_symbols: enablePublicSymbols,
      public_symbol_urls: parseLineInput(publicSymbolUrlsText),
      default_noise_modules: parseListInput(defaultNoiseModulesText),
      default_noise_functions: parseLineInput(defaultNoiseFunctionsText),
    };

    if (llmApiKeyInput.trim().length > 0) {
      payload.llm_api_key = llmApiKeyInput.trim();
    }

    saveSettingsMutation.mutate(payload);
  };

  const partitionQuotaSum = useMemo(() => {
    const vals = [
      dbQuotaGb,
      symbolsSymQuotaGb,
      symbolsPublicCacheQuotaGb,
      symbolsRawQuotaGb,
      logsQuotaGb,
      dumpsQuotaGb,
      stagingQuotaGb,
    ].map((v) => Number(v) || 0);
    return vals.reduce((acc, n) => acc + n, 0);
  }, [
    dbQuotaGb,
    symbolsSymQuotaGb,
    symbolsPublicCacheQuotaGb,
    symbolsRawQuotaGb,
    logsQuotaGb,
    dumpsQuotaGb,
    stagingQuotaGb,
  ]);

  const parsedTotalQuota = Number(totalDiskQuotaGb) || 0;
  const parsedFloorGb = Number(hostSafetyFloorGb) || 0;

  const gcPartitionRows = useMemo(
    () => (gcReport ? normalizeGCPartitions(gcReport.partitions) : []),
    [gcReport],
  );

  const handleCopyRevealedKey = () => {
    if (!revealedKey) {
      return;
    }
    void copyTextToClipboard(revealedKey.apiKey).then((ok) => {
      if (ok) {
        setCopiedKey(true);
        window.setTimeout(() => setCopiedKey(false), 1800);
      }
    });
  };

  const ensureProjectApiKey = async (
    proj: ProjectRead,
  ): Promise<string | null> => {
    setProjectKeyError((prev) => (prev?.projectId === proj.id ? null : prev));
    setLoadingCardKeyId(proj.id);
    try {
      const key = await queryClient.fetchQuery({
        queryKey: ["project-api-key", proj.id],
        queryFn: () => fetchAndResolveProjectApiKey(proj),
        staleTime: 60_000,
      });
      setResolvedProjectKeys((prev) => ({ ...prev, [proj.id]: key }));
      return key;
    } catch (err) {
      setProjectKeyError({
        projectId: proj.id,
        message:
          err instanceof ApiError
            ? err.message
            : t("admin.projects.loadKeyFailed"),
      });
      return null;
    } finally {
      setLoadingCardKeyId((prev) => (prev === proj.id ? null : prev));
    }
  };

  const handleToggleCardApiKey = async (proj: ProjectRead) => {
    const isCurrentlyVisible = Boolean(visibleCardKeyIds[proj.id]);
    if (isCurrentlyVisible) {
      setVisibleCardKeyIds((prev) => ({ ...prev, [proj.id]: false }));
      return;
    }
    setVisibleCardKeyIds((prev) => ({ ...prev, [proj.id]: true }));
    if (resolvedProjectKeys[proj.id] === undefined) {
      await ensureProjectApiKey(proj);
    }
  };

  const handleCopyProjectApiKey = async (
    proj: ProjectRead,
    targetKey: string,
  ) => {
    let keyToCopy =
      proj.id === editingProject?.id && editingProjectKeyQuery.data !== undefined
        ? editingProjectKeyQuery.data
        : resolvedProjectKeys[proj.id];

    if (keyToCopy === undefined) {
      keyToCopy = await ensureProjectApiKey(proj);
    }
    if (!keyToCopy) {
      setVisibleCardKeyIds((prev) => ({ ...prev, [proj.id]: true }));
      return;
    }
    const ok = await copyTextToClipboard(keyToCopy);
    if (ok) {
      setProjectKeyError((prev) => (prev?.projectId === proj.id ? null : prev));
      setCopiedProjectKeyTarget(targetKey);
      window.setTimeout(() => {
        setCopiedProjectKeyTarget((curr) =>
          curr === targetKey ? null : curr,
        );
      }, 1800);
    } else {
      setProjectKeyError({
        projectId: proj.id,
        message: t("admin.projects.copyKeyFailed"),
      });
      if (targetKey.startsWith("editor:")) {
        setShowEditorApiKey(true);
      } else {
        setVisibleCardKeyIds((prev) => ({ ...prev, [proj.id]: true }));
      }
    }
  };

  const providerLabel = (id: LLMProviderId, fallbackName: string): string => {
    const key = `admin.system.providers.${id}` as const;
    return t(key, { defaultValue: fallbackName });
  };

  const thinkingDescKey = `thinkingLevel.desc.${llmThinkingLevel}` as const;
  const presetSelectValue =
    !useCustomModelInput && activeProviderPreset.models.includes(llmModel)
      ? llmModel
      : CUSTOM_MODEL_SENTINEL;

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-indigo-50 text-indigo-600">
            <ShieldCheck className="h-5 w-5" />
          </div>
          <div>
            <h1 className="text-xl font-semibold text-slate-900">
              {t("admin.title")}
            </h1>
            <p className="text-sm text-slate-500">{t("admin.subtitle")}</p>
          </div>
        </div>
        <span className="rounded-full border border-slate-200 bg-white px-3 py-1 text-xs text-slate-600">
          {t("admin.projectsCount", { count: projects.length })}
        </span>
      </div>

      {/* Show-Once API Key Banner */}
      {revealedKey && (
        <div
          role="status"
          className="rounded-xl border border-emerald-300 bg-emerald-50 p-4 shadow-sm"
        >
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="space-y-1">
              <div className="flex items-center gap-2 text-sm font-semibold text-emerald-900">
                <KeyRound className="h-4 w-4 text-emerald-700" />
                <span>
                  {revealedKey.action === "created"
                    ? t("admin.revealedKey.createdTitle", {
                        name: revealedKey.projectName,
                        key: revealedKey.projectKey,
                      })
                    : t("admin.revealedKey.rotatedTitle", {
                        name: revealedKey.projectName,
                        key: revealedKey.projectKey,
                      })}
                </span>
              </div>
              <p className="text-xs text-emerald-800">
                {t("admin.revealedKey.descBefore")}
                <code className="font-mono">localStorage</code>
                {t("admin.revealedKey.descAfter")}
              </p>
            </div>
            <button
              type="button"
              onClick={() => setRevealedKey(null)}
              className="rounded p-1 text-emerald-700 hover:bg-emerald-100"
              aria-label={t("admin.revealedKey.dismissAria")}
            >
              <X className="h-4 w-4" />
            </button>
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-2">
            <code className="flex-1 break-all rounded-md border border-emerald-200 bg-white px-3 py-2 font-mono text-xs text-slate-900">
              {revealedKey.apiKey}
            </code>
            <button
              type="button"
              onClick={handleCopyRevealedKey}
              className="inline-flex items-center gap-1.5 rounded-md bg-emerald-700 px-3 py-2 text-xs font-medium text-white hover:bg-emerald-800"
            >
              {copiedKey ? (
                <>
                  <Check className="h-3.5 w-3.5" />
                  {t("common.copied")}
                </>
              ) : (
                <>
                  <Copy className="h-3.5 w-3.5" />
                  {t("admin.revealedKey.copyKey")}
                </>
              )}
            </button>
          </div>
        </div>
      )}

      {/* SECTION 1: GLOBAL SYSTEM & LLM PROVIDER SETTINGS */}
      <section className="space-y-5 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Sliders className="h-5 w-5 text-indigo-600" />
            <div>
              <h2 className="text-base font-semibold text-slate-900">
                {t("admin.system.title")}
              </h2>
              <p className="text-xs text-slate-500">
                {t("admin.system.subtitle")}
              </p>
            </div>
          </div>

          {settingsSavedMsg && (
            <span className="inline-flex items-center gap-1 text-xs font-medium text-emerald-700">
              <CheckCircle2 className="h-4 w-4" />
              {settingsSavedMsg}
            </span>
          )}
        </div>

        {settingsQuery.isPending && (
          <div className="h-40 animate-pulse rounded-lg bg-slate-100" />
        )}

        {settingsQuery.isError && (
          <div className="flex items-center justify-between rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
            <span>
              {settingsQuery.error instanceof ApiError
                ? settingsQuery.error.message
                : t("admin.system.loadFailed")}
            </span>
            <button
              type="button"
              onClick={() => void settingsQuery.refetch()}
              className="rounded border border-rose-300 bg-white px-2.5 py-1 text-xs font-medium text-rose-700"
            >
              {t("common.retry")}
            </button>
          </div>
        )}

        {settingsQuery.data && (
          <form onSubmit={handleSaveGlobalSettings} className="space-y-6">
            {(settingsValidationError || saveSettingsMutation.isError) && (
              <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                {settingsValidationError ||
                  (saveSettingsMutation.error instanceof ApiError
                    ? saveSettingsMutation.error.message
                    : t("admin.system.saveFailed"))}
              </div>
            )}

            {/* LLM Model Provider, Model & Thinking Level */}
            <div className="space-y-4 rounded-lg border border-indigo-200/80 bg-indigo-50/20 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <Cpu className="h-4 w-4 text-indigo-600" />
                  <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-700">
                    {t("admin.system.llmSectionTitle")}
                  </h3>
                </div>
                <span
                  className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-[11px] font-medium ${
                    activeProviderPreset.protocol === "anthropic_messages"
                      ? "border-amber-200 bg-amber-50 text-amber-800"
                      : "border-indigo-200 bg-indigo-50 text-indigo-700"
                  }`}
                >
                  {activeProviderPreset.protocol === "anthropic_messages"
                    ? t("admin.system.protocolAnthropic")
                    : t("admin.system.protocolOpenAI")}
                </span>
              </div>

              {/* Row 1: Provider + Base URL */}
              <div className="grid gap-3 sm:grid-cols-2">
                <div>
                  <label
                    htmlFor="admin-llm-provider"
                    className="block text-xs font-medium text-slate-700"
                  >
                    {t("admin.system.providerLabel")}
                  </label>
                  <select
                    id="admin-llm-provider"
                    value={llmProvider}
                    onChange={(e) =>
                      handleProviderChange(e.target.value as LLMProviderId)
                    }
                    className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-900"
                  >
                    {providerPresets.map((preset) => (
                      <option key={preset.id} value={preset.id}>
                        {providerLabel(preset.id, preset.name)}
                      </option>
                    ))}
                  </select>
                  <p className="mt-1 text-[11px] text-slate-500">
                    {activeProviderPreset.requires_api_key
                      ? t("admin.system.providerHintKeyRequired")
                      : t("admin.system.providerHintKeyOptional")}
                  </p>
                </div>

                <div>
                  <div className="flex items-center justify-between gap-2">
                    <label
                      htmlFor="admin-llm-base-url"
                      className="block text-xs font-medium text-slate-700"
                    >
                      {t("admin.system.baseUrlLabel")}
                    </label>
                    {llmBaseUrl.trim() !==
                      activeProviderPreset.default_base_url && (
                      <button
                        type="button"
                        onClick={() =>
                          setLlmBaseUrl(activeProviderPreset.default_base_url)
                        }
                        className="inline-flex items-center gap-1 text-[11px] font-medium text-indigo-600 hover:underline"
                      >
                        <RotateCcw className="h-3 w-3" />
                        {t("admin.system.resetDefaultUrl")}
                      </button>
                    )}
                  </div>
                  <input
                    id="admin-llm-base-url"
                    type="text"
                    required
                    value={llmBaseUrl}
                    onChange={(e) => setLlmBaseUrl(e.target.value)}
                    placeholder={activeProviderPreset.default_base_url}
                    className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-3 font-mono text-xs text-slate-900"
                  />
                </div>
              </div>

              {/* Row 2: Model (Preset + Custom) + API Key */}
              <div className="grid gap-3 sm:grid-cols-2">
                <div>
                  <label
                    htmlFor="admin-llm-model-select"
                    className="block text-xs font-medium text-slate-700"
                  >
                    {t("admin.system.modelLabel")}
                  </label>
                  {activeProviderPreset.models.length > 0 ? (
                    <div className="mt-1 space-y-2">
                      <select
                        id="admin-llm-model-select"
                        value={presetSelectValue}
                        onChange={(e) =>
                          handleModelSelectChange(e.target.value)
                        }
                        className="h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 font-mono text-xs text-slate-900"
                      >
                        <optgroup label={t("admin.system.modelPresetLabel")}>
                          {activeProviderPreset.models.map((m) => (
                            <option key={m} value={m}>
                              {m}
                            </option>
                          ))}
                        </optgroup>
                        <option value={CUSTOM_MODEL_SENTINEL}>
                          {t("admin.system.modelCustomOption")}
                        </option>
                      </select>

                      {presetSelectValue === CUSTOM_MODEL_SENTINEL && (
                        <input
                          id="admin-llm-model-custom"
                          type="text"
                          required
                          value={llmModel}
                          onChange={(e) => setLlmModel(e.target.value)}
                          placeholder={t("admin.system.modelCustomPlaceholder")}
                          className="h-9 w-full rounded-md border border-slate-300 bg-white px-3 font-mono text-xs text-slate-900"
                        />
                      )}
                    </div>
                  ) : (
                    <input
                      id="admin-llm-model-select"
                      type="text"
                      required
                      value={llmModel}
                      onChange={(e) => setLlmModel(e.target.value)}
                      placeholder={t("admin.system.modelCustomPlaceholder")}
                      className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-3 font-mono text-xs text-slate-900"
                    />
                  )}
                </div>

                <div>
                  <div className="flex items-center justify-between">
                    <label
                      htmlFor="admin-llm-api-key"
                      className="block text-xs font-medium text-slate-700"
                    >
                      {t("admin.system.apiKeyLabel")}
                    </label>
                    <span className="font-mono text-[11px] text-slate-500">
                      {settingsQuery.data.llm_api_key_set
                        ? t("admin.system.apiKeySet", {
                            masked:
                              settingsQuery.data.llm_api_key_masked ?? "****",
                          })
                        : t("admin.system.apiKeyNotSet")}
                    </span>
                  </div>
                  <div className="relative mt-1">
                    <input
                      id="admin-llm-api-key"
                      type={showLlmApiKey ? "text" : "password"}
                      value={llmApiKeyInput}
                      onChange={(e) => setLlmApiKeyInput(e.target.value)}
                      placeholder={
                        activeProviderPreset.requires_api_key
                          ? t("admin.system.apiKeyPlaceholder")
                          : t("admin.system.apiKeyOptionalPlaceholder")
                      }
                      className="h-9 w-full rounded-md border border-slate-300 bg-white pl-3 pr-9 font-mono text-xs text-slate-900"
                    />
                    <button
                      type="button"
                      onClick={() => setShowLlmApiKey((prev) => !prev)}
                      className="absolute right-2 top-2 text-slate-400 hover:text-slate-700"
                      aria-label={
                        showLlmApiKey
                          ? t("admin.system.hideApiKey")
                          : t("admin.system.showApiKey")
                      }
                    >
                      {showLlmApiKey ? (
                        <EyeOff className="h-4 w-4" />
                      ) : (
                        <Eye className="h-4 w-4" />
                      )}
                    </button>
                  </div>
                </div>
              </div>

              {/* Row 3: Thinking Level */}
              <div className="rounded-lg border border-slate-200 bg-white p-3.5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <label className="inline-flex items-center gap-1.5 text-xs font-semibold text-slate-800">
                    <Brain className="h-4 w-4 text-indigo-600" />
                    {t("admin.system.thinkingLevelLabel")}
                  </label>
                  <span className="font-mono text-[11px] text-indigo-700">
                    {formatThinkingLevel(llmThinkingLevel)}
                  </span>
                </div>

                <div
                  className="mt-2.5 grid grid-cols-2 gap-2 sm:grid-cols-5"
                  role="radiogroup"
                  aria-label={t("admin.system.thinkingLevelLabel")}
                >
                  {THINKING_LEVELS.map((level) => {
                    const selected = llmThinkingLevel === level;
                    return (
                      <button
                        key={level}
                        type="button"
                        role="radio"
                        aria-checked={selected}
                        data-testid={`thinking-level-${level}`}
                        onClick={() => setLlmThinkingLevel(level)}
                        className={`rounded-md border px-2.5 py-2 text-xs font-medium transition-colors ${
                          selected
                            ? "border-indigo-600 bg-indigo-600 text-white shadow-sm"
                            : "border-slate-200 bg-slate-50 text-slate-700 hover:border-slate-300 hover:bg-slate-100"
                        }`}
                      >
                        {formatThinkingLevel(level)}
                      </button>
                    );
                  })}
                </div>

                <p className="mt-2 text-xs text-slate-600">
                  {t(thinkingDescKey)}
                </p>
                <p className="mt-1 text-[11px] text-slate-500">
                  {t("admin.system.thinkingCompatibilityHint")}
                </p>
              </div>
            </div>

            {/* AI Triage Guardrails */}
            <div className="space-y-3 rounded-lg border border-slate-200 p-4">
              <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-600">
                {t("admin.system.guardrailsTitle")}
              </h3>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    {t("admin.system.defaultTriageMode")}
                  </label>
                  <select
                    value={aiTriageMode}
                    onChange={(e) =>
                      setAiTriageMode(e.target.value as AITriageMode)
                    }
                    className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-900"
                  >
                    {AI_TRIAGE_MODES.map((m) => (
                      <option key={m} value={m}>
                        {formatTriageMode(m)}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    {t("admin.system.aiOutputLanguage")}
                  </label>
                  <select
                    value={aiOutputLanguage}
                    onChange={(e) =>
                      setAiOutputLanguage(e.target.value as AIOutputLanguage)
                    }
                    className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-900"
                  >
                    <option value="zh-CN">
                      {t("admin.system.aiOutputLanguageZh")}
                    </option>
                    <option value="en-US">
                      {t("admin.system.aiOutputLanguageEn")}
                    </option>
                  </select>
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    {t("admin.system.aiMaxSteps")}
                  </label>
                  <input
                    type="number"
                    required
                    min={1}
                    max={50}
                    step={1}
                    value={aiMaxSteps}
                    onChange={(e) => setAiMaxSteps(e.target.value)}
                    className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 font-mono text-sm text-slate-900"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    {t("admin.system.aiTimeoutSeconds")}
                  </label>
                  <input
                    type="number"
                    required
                    min={1}
                    max={3600}
                    step={1}
                    value={aiTimeoutSeconds}
                    onChange={(e) => setAiTimeoutSeconds(e.target.value)}
                    className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 font-mono text-sm text-slate-900"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    {t("admin.system.toolOutputMaxChars")}
                  </label>
                  <input
                    type="number"
                    required
                    min={100}
                    max={200000}
                    step={1}
                    value={aiToolOutputMaxChars}
                    onChange={(e) => setAiToolOutputMaxChars(e.target.value)}
                    className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 font-mono text-sm text-slate-900"
                  />
                </div>
              </div>
            </div>

            {/* Disk Quotas & Host Safety Floor */}
            <div className="space-y-3 rounded-lg border border-slate-200 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-600">
                  {t("admin.system.quotasTitle")}
                </h3>
                <span
                  className={`font-mono text-xs ${
                    partitionQuotaSum > parsedTotalQuota
                      ? "font-semibold text-amber-700"
                      : "text-slate-500"
                  }`}
                >
                  {t("admin.system.partitionSum", {
                    sum: partitionQuotaSum.toFixed(1),
                    total: parsedTotalQuota.toFixed(1),
                  })}
                </span>
              </div>

              <div className="grid gap-3 sm:grid-cols-3">
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    {t("admin.system.totalDiskQuota")}
                  </label>
                  <input
                    type="number"
                    required
                    step="0.5"
                    min={0}
                    value={totalDiskQuotaGb}
                    onChange={(e) => setTotalDiskQuotaGb(e.target.value)}
                    className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 font-mono text-sm text-slate-900"
                  />
                </div>
                <div>
                  <div className="flex items-center justify-between">
                    <label className="block text-xs font-medium text-slate-700">
                      {t("admin.system.hostSafetyFloor")}
                    </label>
                    {parsedFloorGb < 50 && (
                      <span className="text-[11px] font-medium text-amber-700">
                        {t("admin.system.recommendedFloor")}
                      </span>
                    )}
                  </div>
                  <input
                    type="number"
                    required
                    step="0.5"
                    min={0}
                    value={hostSafetyFloorGb}
                    onChange={(e) => setHostSafetyFloorGb(e.target.value)}
                    className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 font-mono text-sm text-slate-900"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    {t("admin.system.gcThreshold")}
                  </label>
                  <input
                    type="number"
                    required
                    step="any"
                    min={0.1}
                    max={100}
                    value={gcThresholdPercent}
                    onChange={(e) => setGcThresholdPercent(e.target.value)}
                    className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 font-mono text-sm text-slate-900"
                  />
                </div>
              </div>

              <div className="grid gap-3 sm:grid-cols-4 lg:grid-cols-7">
                <div>
                  <label className="block text-[11px] font-medium text-slate-700">
                    {t("admin.system.quotaDb")}
                  </label>
                  <input
                    type="number"
                    required
                    step="0.5"
                    min={0}
                    value={dbQuotaGb}
                    onChange={(e) => setDbQuotaGb(e.target.value)}
                    className="mt-1 h-8 w-full rounded-md border border-slate-300 px-2 font-mono text-xs text-slate-900"
                  />
                </div>
                <div>
                  <label className="block text-[11px] font-medium text-slate-700">
                    {t("admin.system.quotaSym")}
                  </label>
                  <input
                    type="number"
                    required
                    step="0.5"
                    min={0}
                    value={symbolsSymQuotaGb}
                    onChange={(e) => setSymbolsSymQuotaGb(e.target.value)}
                    className="mt-1 h-8 w-full rounded-md border border-slate-300 px-2 font-mono text-xs text-slate-900"
                  />
                </div>
                <div>
                  <label className="block text-[11px] font-medium text-slate-700">
                    {t("admin.system.quotaPublicSym")}
                  </label>
                  <input
                    type="number"
                    required
                    step="0.5"
                    min={0}
                    value={symbolsPublicCacheQuotaGb}
                    onChange={(e) =>
                      setSymbolsPublicCacheQuotaGb(e.target.value)
                    }
                    className="mt-1 h-8 w-full rounded-md border border-slate-300 px-2 font-mono text-xs text-slate-900"
                  />
                </div>
                <div>
                  <label className="block text-[11px] font-medium text-slate-700">
                    {t("admin.system.quotaRawSym")}
                  </label>
                  <input
                    type="number"
                    required
                    step="0.5"
                    min={0}
                    value={symbolsRawQuotaGb}
                    onChange={(e) => setSymbolsRawQuotaGb(e.target.value)}
                    className="mt-1 h-8 w-full rounded-md border border-slate-300 px-2 font-mono text-xs text-slate-900"
                  />
                </div>
                <div>
                  <label className="block text-[11px] font-medium text-slate-700">
                    {t("admin.system.quotaLogs")}
                  </label>
                  <input
                    type="number"
                    required
                    step="0.5"
                    min={0}
                    value={logsQuotaGb}
                    onChange={(e) => setLogsQuotaGb(e.target.value)}
                    className="mt-1 h-8 w-full rounded-md border border-slate-300 px-2 font-mono text-xs text-slate-900"
                  />
                </div>
                <div>
                  <label className="block text-[11px] font-medium text-slate-700">
                    {t("admin.system.quotaDumps")}
                  </label>
                  <input
                    type="number"
                    required
                    step="0.5"
                    min={0}
                    value={dumpsQuotaGb}
                    onChange={(e) => setDumpsQuotaGb(e.target.value)}
                    className="mt-1 h-8 w-full rounded-md border border-slate-300 px-2 font-mono text-xs text-slate-900"
                  />
                </div>
                <div>
                  <label className="block text-[11px] font-medium text-slate-700">
                    {t("admin.system.quotaStaging")}
                  </label>
                  <input
                    type="number"
                    required
                    step="0.5"
                    min={0}
                    value={stagingQuotaGb}
                    onChange={(e) => setStagingQuotaGb(e.target.value)}
                    className="mt-1 h-8 w-full rounded-md border border-slate-300 px-2 font-mono text-xs text-slate-900"
                  />
                </div>
              </div>
            </div>

            {/* Retention, Symbols & Default Noise Rules */}
            <div className="space-y-4 rounded-lg border border-slate-200 p-4">
              <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-600">
                {t("admin.system.retentionTitle")}
              </h3>

              <div className="grid gap-3 sm:grid-cols-3">
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    {t("admin.system.logRetentionDays")}
                  </label>
                  <input
                    type="number"
                    required
                    min={1}
                    max={3650}
                    step={1}
                    value={logRetentionDays}
                    onChange={(e) => setLogRetentionDays(e.target.value)}
                    className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 font-mono text-sm text-slate-900"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    {t("admin.system.dumpRetentionDays")}
                  </label>
                  <input
                    type="number"
                    required
                    min={1}
                    max={3650}
                    step={1}
                    value={dumpRetentionDays}
                    onChange={(e) => setDumpRetentionDays(e.target.value)}
                    className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 font-mono text-sm text-slate-900"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    {t("admin.system.stagingTtlHours")}
                  </label>
                  <input
                    type="number"
                    required
                    min={1}
                    max={720}
                    step={1}
                    value={stagingTtlHours}
                    onChange={(e) => setStagingTtlHours(e.target.value)}
                    className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 font-mono text-sm text-slate-900"
                  />
                </div>
              </div>

              <div className="flex flex-wrap gap-6">
                <label className="inline-flex cursor-pointer items-center gap-2 text-xs text-slate-700">
                  <input
                    type="checkbox"
                    checked={archiveRawSymbols}
                    onChange={(e) => setArchiveRawSymbols(e.target.checked)}
                    className="h-4 w-4 rounded border-slate-300 text-indigo-600"
                  />
                  {t("admin.system.archiveRawSymbols")}
                </label>

                <label className="inline-flex cursor-pointer items-center gap-2 text-xs text-slate-700">
                  <input
                    type="checkbox"
                    checked={enablePublicSymbols}
                    onChange={(e) => setEnablePublicSymbols(e.target.checked)}
                    className="h-4 w-4 rounded border-slate-300 text-indigo-600"
                  />
                  {t("admin.system.enablePublicSymbols")}
                </label>
              </div>

              <div className="grid gap-3 sm:grid-cols-3">
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    {t("admin.system.publicSymbolUrls")}
                  </label>
                  <textarea
                    rows={3}
                    value={publicSymbolUrlsText}
                    onChange={(e) => setPublicSymbolUrlsText(e.target.value)}
                    placeholder="https://msdl.microsoft.com/download/symbols"
                    className="mt-1 w-full rounded-md border border-slate-300 p-2 font-mono text-xs text-slate-900"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    {t("admin.system.defaultNoiseModules")}
                  </label>
                  <textarea
                    rows={3}
                    value={defaultNoiseModulesText}
                    onChange={(e) => setDefaultNoiseModulesText(e.target.value)}
                    className="mt-1 w-full rounded-md border border-slate-300 p-2 font-mono text-xs text-slate-900"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    {t("admin.system.defaultNoiseFunctions")}
                  </label>
                  <textarea
                    rows={3}
                    value={defaultNoiseFunctionsText}
                    onChange={(e) =>
                      setDefaultNoiseFunctionsText(e.target.value)
                    }
                    className="mt-1 w-full rounded-md border border-slate-300 p-2 font-mono text-xs text-slate-900"
                  />
                </div>
              </div>
            </div>

            <div className="flex justify-end">
              <button
                type="submit"
                disabled={saveSettingsMutation.isPending}
                className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-indigo-700 disabled:opacity-50"
              >
                <Save className="h-4 w-4" />
                {saveSettingsMutation.isPending
                  ? t("common.saving")
                  : t("admin.system.saveGlobalButton")}
              </button>
            </div>
          </form>
        )}
      </section>

      {/* SECTION 2: PROJECT MANAGEMENT */}
      <section className="space-y-5 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="flex items-center gap-2">
          <KeyRound className="h-5 w-5 text-indigo-600" />
          <div>
            <h2 className="text-base font-semibold text-slate-900">
              {t("admin.projects.title")}
            </h2>
            <p className="text-xs text-slate-500">
              {t("admin.projects.subtitle")}
            </p>
          </div>
        </div>

        {/* Create Project Form */}
        <form
          onSubmit={handleCreateProject}
          className="rounded-lg border border-slate-200 bg-slate-50 p-4"
        >
          <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-600">
            {t("admin.projects.createTitle")}
          </h3>
          {createProjectMutation.isError && (
            <div className="mt-2 rounded border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
              {createProjectMutation.error instanceof ApiError
                ? createProjectMutation.error.message
                : t("admin.projects.createFailed")}
            </div>
          )}
          <div className="mt-3 grid gap-3 sm:grid-cols-4">
            <div>
              <label className="block text-xs font-medium text-slate-700">
                {t("admin.projects.projectKey")}{" "}
                <span className="text-rose-500">*</span>
              </label>
              <input
                type="text"
                required
                value={newProjectKey}
                onChange={(e) => setNewProjectKey(e.target.value)}
                placeholder={t("admin.projects.projectKeyPlaceholder")}
                className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-3 font-mono text-xs text-slate-900"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-700">
                {t("admin.projects.displayName")}{" "}
                <span className="text-rose-500">*</span>
              </label>
              <input
                type="text"
                required
                value={newProjectName}
                onChange={(e) => setNewProjectName(e.target.value)}
                placeholder={t("admin.projects.displayNamePlaceholder")}
                className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-700">
                {t("admin.projects.aiTriageMode")}
              </label>
              <select
                value={newProjectTriageMode}
                onChange={(e) =>
                  setNewProjectTriageMode(e.target.value as AITriageMode | "")
                }
                className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-900"
              >
                <option value="">{t("admin.projects.inheritGlobal")}</option>
                {AI_TRIAGE_MODES.map((m) => (
                  <option key={m} value={m}>
                    {formatTriageMode(m)}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-700">
                {t("admin.projects.sessionIdLabel")}
              </label>
              <input
                type="text"
                maxLength={64}
                value={newProjectSessionIdLabel}
                onChange={(e) => setNewProjectSessionIdLabel(e.target.value)}
                placeholder={t("admin.projects.sessionIdLabelPlaceholder")}
                className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900"
              />
            </div>
            <div className="flex items-end">
              <button
                type="submit"
                disabled={
                  createProjectMutation.isPending ||
                  !newProjectKey.trim() ||
                  !newProjectName.trim()
                }
                className="inline-flex h-9 w-full items-center justify-center gap-1.5 rounded-md bg-indigo-600 px-3 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
              >
                <Plus className="h-4 w-4" />
                {createProjectMutation.isPending
                  ? t("common.creating")
                  : t("admin.projects.createButton")}
              </button>
            </div>
          </div>
        </form>

        {rotateKeyMutation.isError && (
          <div className="flex items-center justify-between rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
            <span>
              {rotateKeyMutation.error instanceof ApiError
                ? rotateKeyMutation.error.message
                : t("admin.projects.rotateFailed")}
            </span>
            <button
              type="button"
              onClick={() => rotateKeyMutation.reset()}
              className="rounded p-0.5 text-rose-600 hover:bg-rose-100"
              aria-label={t("admin.projects.dismissRotateError")}
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        )}

        {/* Projects List + Per-Project Settings Editor */}
        {projects.length === 0 ? (
          <p className="text-sm text-slate-500">{t("admin.projects.empty")}</p>
        ) : (
          <div className="grid gap-6 lg:grid-cols-12">
            <div className="overflow-hidden rounded-lg border border-slate-200 lg:col-span-5">
              <div className="divide-y divide-slate-200">
                {projects.map((proj) => {
                  const isSelected = proj.id === editingProjectId;
                  const resolvedKey =
                    proj.id === editingProject?.id &&
                    editingProjectKeyQuery.data !== undefined
                      ? editingProjectKeyQuery.data
                      : resolvedProjectKeys[proj.id];
                  const hasStoredKey = Boolean(
                    resolvedKey || getStoredProjectApiKey(proj.key),
                  );
                  const isConfirmingRotate =
                    confirmRotateProjectId === proj.id;
                  const isCardKeyVisible = Boolean(visibleCardKeyIds[proj.id]);
                  const isCardLoading =
                    loadingCardKeyId === proj.id ||
                    (isSelected && editingProjectKeyQuery.isLoading);
                  const cardCopyTarget = `card:${proj.id}`;
                  const isCardCopied = copiedProjectKeyTarget === cardCopyTarget;
                  const cardErr =
                    projectKeyError?.projectId === proj.id
                      ? projectKeyError.message
                      : null;

                  return (
                    <div
                      key={proj.id}
                      className={`p-3.5 transition-colors ${
                        isSelected ? "bg-indigo-50/50" : "bg-white"
                      }`}
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div>
                          <div className="font-medium text-slate-900">
                            {proj.name}
                          </div>
                          <div className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-slate-500">
                            <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-slate-700">
                              {proj.key}
                            </code>
                            {hasStoredKey && (
                              <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10px] font-medium text-emerald-700">
                                {t("admin.projects.keyInBrowser")}
                              </span>
                            )}
                          </div>
                        </div>

                        <button
                          type="button"
                          onClick={() => setEditingProjectId(proj.id)}
                          className={`rounded-md border px-2.5 py-1 text-xs font-medium ${
                            isSelected
                              ? "border-indigo-500 bg-indigo-600 text-white"
                              : "border-slate-300 bg-white text-slate-700 hover:bg-slate-50"
                          }`}
                        >
                          {isSelected ? t("common.editing") : t("common.edit")}
                        </button>
                      </div>

                      {isCardKeyVisible && (
                        <div className="mt-2.5 rounded-md border border-slate-200 bg-slate-50 p-2 text-xs">
                          {isCardLoading ? (
                            <div className="flex items-center gap-1.5 text-slate-500">
                              <Loader2 className="h-3.5 w-3.5 animate-spin" />
                              <span>{t("admin.projects.loadingKey")}</span>
                            </div>
                          ) : resolvedKey ? (
                            <div className="flex items-center gap-1.5">
                              <code
                                data-testid={`project-card-api-key-${proj.key}`}
                                className="flex-1 select-all break-all rounded border border-slate-200 bg-white px-2 py-1 font-mono text-[11px] text-slate-900"
                              >
                                {resolvedKey}
                              </code>
                              <button
                                type="button"
                                onClick={() =>
                                  void handleCopyProjectApiKey(
                                    proj,
                                    cardCopyTarget,
                                  )
                                }
                                className="inline-flex shrink-0 items-center gap-1 rounded border border-slate-300 bg-white px-2 py-1 text-[11px] font-medium text-slate-700 hover:bg-slate-100"
                              >
                                {isCardCopied ? (
                                  <>
                                    <Check className="h-3 w-3 text-emerald-600" />
                                    {t("common.copied")}
                                  </>
                                ) : (
                                  <>
                                    <Copy className="h-3 w-3" />
                                    {t("common.copy")}
                                  </>
                                )}
                              </button>
                            </div>
                          ) : (
                            <p className="text-[11px] text-amber-800">
                              {t("admin.projects.keyUnavailable")}
                            </p>
                          )}
                        </div>
                      )}

                      {cardErr && (
                        <div className="mt-2 rounded border border-rose-200 bg-rose-50 px-2 py-1 text-[11px] text-rose-700">
                          {cardErr}
                        </div>
                      )}

                      <div className="mt-2.5 flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 pt-2 text-xs">
                        {isConfirmingRotate ? (
                          <div className="flex w-full items-center justify-between gap-2">
                            <span className="font-medium text-amber-800">
                              {t("admin.projects.confirmRotatePrompt")}
                            </span>
                            <div className="flex items-center gap-1.5">
                              <button
                                type="button"
                                onClick={() => setConfirmRotateProjectId(null)}
                                className="rounded border border-slate-300 bg-white px-2 py-0.5 text-slate-700"
                              >
                                {t("common.cancel")}
                              </button>
                              <button
                                type="button"
                                disabled={rotateKeyMutation.isPending}
                                onClick={() =>
                                  rotateKeyMutation.mutate(proj.id)
                                }
                                className="rounded bg-amber-600 px-2 py-0.5 font-medium text-white hover:bg-amber-700"
                              >
                                {t("admin.projects.confirmRotate")}
                              </button>
                            </div>
                          </div>
                        ) : (
                          <>
                            <div className="flex flex-wrap items-center gap-3">
                              <button
                                type="button"
                                disabled={isCardLoading}
                                onClick={() =>
                                  void handleToggleCardApiKey(proj)
                                }
                                className="inline-flex items-center gap-1 font-medium text-indigo-600 hover:text-indigo-800 disabled:opacity-50"
                              >
                                {isCardKeyVisible ? (
                                  <>
                                    <EyeOff className="h-3.5 w-3.5" />
                                    {t("admin.projects.hideKey")}
                                  </>
                                ) : (
                                  <>
                                    <Eye className="h-3.5 w-3.5" />
                                    {t("admin.projects.viewKey")}
                                  </>
                                )}
                              </button>
                              <button
                                type="button"
                                disabled={isCardLoading}
                                onClick={() =>
                                  void handleCopyProjectApiKey(
                                    proj,
                                    cardCopyTarget,
                                  )
                                }
                                className="inline-flex items-center gap-1 font-medium text-slate-600 hover:text-indigo-600 disabled:opacity-50"
                              >
                                {isCardCopied ? (
                                  <>
                                    <Check className="h-3.5 w-3.5 text-emerald-600" />
                                    <span className="text-emerald-700">
                                      {t("common.copied")}
                                    </span>
                                  </>
                                ) : (
                                  <>
                                    <Copy className="h-3.5 w-3.5" />
                                    {t("admin.projects.copyKey")}
                                  </>
                                )}
                              </button>
                            </div>
                            <button
                              type="button"
                              onClick={() => {
                                rotateKeyMutation.reset();
                                setConfirmRotateProjectId(proj.id);
                              }}
                              className="inline-flex items-center gap-1 font-medium text-slate-600 hover:text-amber-700"
                            >
                              <RefreshCw className="h-3 w-3" />
                              {t("admin.projects.rotateKey")}
                            </button>
                          </>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

            {editingProject && (
              <form
                onSubmit={handleSaveProject}
                className="space-y-4 rounded-lg border border-slate-200 p-4 lg:col-span-7"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h3 className="text-sm font-semibold text-slate-900">
                    {t("admin.projects.editorTitle")}
                    <code className="font-mono text-indigo-600">
                      {editingProject.key}
                    </code>
                  </h3>
                  {projectSavedMsg && (
                    <span className="inline-flex items-center gap-1 text-xs font-medium text-emerald-700">
                      <CheckCircle2 className="h-3.5 w-3.5" />
                      {projectSavedMsg}
                    </span>
                  )}
                </div>

                {updateProjectMutation.isError && (
                  <div className="rounded border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                    {updateProjectMutation.error instanceof ApiError
                      ? updateProjectMutation.error.message
                      : t("admin.projects.saveFailed")}
                  </div>
                )}

                {/* Project API Key Viewer & Copier */}
                <div className="rounded-lg border border-slate-200 bg-slate-50/80 p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-1.5 text-xs font-medium text-slate-700">
                      <KeyRound className="h-3.5 w-3.5 text-indigo-600" />
                      <span>{t("admin.projects.apiKeyLabel")}</span>
                    </div>
                    <span className="text-[11px] text-slate-500">
                      {t("admin.projects.apiKeyHeaderHint")}
                    </span>
                  </div>

                  {editingProjectKeyQuery.isLoading ? (
                    <div className="mt-2 flex items-center gap-1.5 text-xs text-slate-500">
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      <span>{t("admin.projects.loadingKey")}</span>
                    </div>
                  ) : editingProjectKeyQuery.isError ? (
                    <div className="mt-2 rounded border border-rose-200 bg-rose-50 px-2.5 py-1.5 text-xs text-rose-700">
                      {editingProjectKeyQuery.error instanceof ApiError
                        ? editingProjectKeyQuery.error.message
                        : t("admin.projects.loadKeyFailed")}
                    </div>
                  ) : editingProjectKeyQuery.data ? (
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      <input
                        type="text"
                        readOnly
                        aria-label={t("admin.projects.apiKeyLabel")}
                        data-testid="editor-project-api-key"
                        value={
                          showEditorApiKey
                            ? editingProjectKeyQuery.data
                            : maskProjectApiKey(editingProjectKeyQuery.data)
                        }
                        onFocus={(e) => {
                          if (showEditorApiKey) {
                            e.currentTarget.select();
                          }
                        }}
                        className="h-9 min-w-[200px] flex-1 rounded-md border border-slate-300 bg-white px-2.5 font-mono text-xs text-slate-900 focus:border-indigo-500 focus:outline-none"
                      />
                      <button
                        type="button"
                        onClick={() => setShowEditorApiKey((prev) => !prev)}
                        className="inline-flex h-9 items-center gap-1.5 rounded-md border border-slate-300 bg-white px-2.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
                      >
                        {showEditorApiKey ? (
                          <>
                            <EyeOff className="h-3.5 w-3.5" />
                            {t("admin.projects.hideKey")}
                          </>
                        ) : (
                          <>
                            <Eye className="h-3.5 w-3.5" />
                            {t("admin.projects.showKey")}
                          </>
                        )}
                      </button>
                      <button
                        type="button"
                        onClick={() =>
                          void handleCopyProjectApiKey(
                            editingProject,
                            `editor:${editingProject.id}`,
                          )
                        }
                        className="inline-flex h-9 items-center gap-1.5 rounded-md bg-indigo-600 px-3 text-xs font-medium text-white hover:bg-indigo-700"
                      >
                        {copiedProjectKeyTarget ===
                        `editor:${editingProject.id}` ? (
                          <>
                            <Check className="h-3.5 w-3.5" />
                            {t("common.copied")}
                          </>
                        ) : (
                          <>
                            <Copy className="h-3.5 w-3.5" />
                            {t("admin.projects.copyKey")}
                          </>
                        )}
                      </button>
                    </div>
                  ) : (
                    <div className="mt-2 rounded border border-amber-200 bg-amber-50 px-2.5 py-2 text-xs text-amber-800">
                      {t("admin.projects.keyUnavailable")}
                    </div>
                  )}

                  {projectKeyError?.projectId === editingProject.id && (
                    <div className="mt-2 rounded border border-rose-200 bg-rose-50 px-2.5 py-1.5 text-xs text-rose-700">
                      {projectKeyError.message}
                    </div>
                  )}
                </div>

                <div className="grid gap-3 sm:grid-cols-2">
                  <div>
                    <label className="block text-xs font-medium text-slate-700">
                      {t("admin.projects.projectName")}
                    </label>
                    <input
                      type="text"
                      required
                      value={editName}
                      onChange={(e) => setEditName(e.target.value)}
                      className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 text-sm text-slate-900"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-slate-700">
                      {t("admin.projects.triageModeOverride")}
                    </label>
                    <select
                      value={editAiTriageMode}
                      onChange={(e) =>
                        setEditAiTriageMode(
                          e.target.value as AITriageMode | "",
                        )
                      }
                      className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-900"
                    >
                      <option value="">
                        {t("admin.projects.inheritGlobal")}
                      </option>
                      {AI_TRIAGE_MODES.map((m) => (
                        <option key={m} value={m}>
                          {formatTriageMode(m)}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>

                <div className="grid gap-3 sm:grid-cols-3">
                  <div>
                    <label className="block text-xs font-medium text-slate-700">
                      {t("admin.projects.logRetentionOverride")}
                    </label>
                    <input
                      type="number"
                      min={1}
                      value={editLogRetentionDays}
                      onChange={(e) => setEditLogRetentionDays(e.target.value)}
                      placeholder={t("admin.projects.inheritGlobalPlaceholder")}
                      className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 font-mono text-sm text-slate-900"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-slate-700">
                      {t("admin.projects.dumpRetentionOverride")}
                    </label>
                    <input
                      type="number"
                      min={1}
                      value={editDumpRetentionDays}
                      onChange={(e) => setEditDumpRetentionDays(e.target.value)}
                      placeholder={t("admin.projects.inheritGlobalPlaceholder")}
                      className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 font-mono text-sm text-slate-900"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-slate-700">
                      {t("admin.projects.sessionIdLabel")}
                    </label>
                    <input
                      type="text"
                      maxLength={64}
                      value={editSessionIdLabel}
                      onChange={(e) => setEditSessionIdLabel(e.target.value)}
                      placeholder={t("admin.projects.sessionIdLabelPlaceholder")}
                      className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 text-sm text-slate-900"
                    />
                  </div>
                </div>

                <div className="grid gap-3 sm:grid-cols-3">
                  <div>
                    <label className="block text-xs font-medium text-slate-700">
                      {t("admin.projects.noiseModules")}
                    </label>
                    <textarea
                      rows={3}
                      value={editNoiseModulesText}
                      onChange={(e) => setEditNoiseModulesText(e.target.value)}
                      placeholder={"ntdll.dll\nkernel32.dll"}
                      className="mt-1 w-full rounded-md border border-slate-300 p-2 font-mono text-xs text-slate-900"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-slate-700">
                      {t("admin.projects.noiseFunctions")}
                    </label>
                    <textarea
                      rows={3}
                      value={editNoiseFunctionsText}
                      onChange={(e) =>
                        setEditNoiseFunctionsText(e.target.value)
                      }
                      placeholder={"abort\nraise"}
                      className="mt-1 w-full rounded-md border border-slate-300 p-2 font-mono text-xs text-slate-900"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-slate-700">
                      {t("admin.projects.customCrashHandlers")}
                    </label>
                    <textarea
                      rows={3}
                      value={editCustomHandlersText}
                      onChange={(e) =>
                        setEditCustomHandlersText(e.target.value)
                      }
                      placeholder="MyCrashHandler"
                      className="mt-1 w-full rounded-md border border-slate-300 p-2 font-mono text-xs text-slate-900"
                    />
                  </div>
                </div>

                <div className="flex justify-end">
                  <button
                    type="submit"
                    disabled={
                      updateProjectMutation.isPending || !editName.trim()
                    }
                    className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
                  >
                    <Save className="h-3.5 w-3.5" />
                    {updateProjectMutation.isPending
                      ? t("common.saving")
                      : t("admin.projects.saveProjectButton")}
                  </button>
                </div>
              </form>
            )}
          </div>
        )}
      </section>

      {/* SECTION 3: MANUAL GARBAGE COLLECTION & GCREPORT */}
      <section className="space-y-4 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="flex items-center gap-2">
            <HardDrive className="h-5 w-5 text-indigo-600" />
            <div>
              <h2 className="text-base font-semibold text-slate-900">
                {t("admin.gc.title")}
              </h2>
              <p className="text-xs text-slate-500">
                {t("admin.gc.subtitleBefore")}
                <code className="font-mono">is_pinned=true</code>
                {t("admin.gc.subtitleAfter")}
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <label className="inline-flex cursor-pointer items-center gap-2 text-xs text-slate-700">
              <input
                type="checkbox"
                checked={forceEmergencyGc}
                onChange={(e) => setForceEmergencyGc(e.target.checked)}
                className="h-4 w-4 rounded border-slate-300 text-rose-600"
              />
              {t("admin.gc.forceEmergency")}
            </label>

            <button
              type="button"
              disabled={gcMutation.isPending}
              onClick={() => gcMutation.mutate(forceEmergencyGc)}
              className="inline-flex items-center gap-1.5 rounded-md bg-slate-900 px-3.5 py-2 text-xs font-medium text-white shadow-sm hover:bg-slate-800 disabled:opacity-50"
            >
              {gcMutation.isPending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Trash2 className="h-3.5 w-3.5" />
              )}
              {gcMutation.isPending
                ? t("admin.gc.running")
                : t("admin.gc.triggerButton")}
            </button>
          </div>
        </div>

        {gcMutation.isError && (
          <div className="flex items-center gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
            <AlertTriangle className="h-4 w-4 shrink-0" />
            <span>
              {gcMutation.error instanceof ApiError
                ? gcMutation.error.message
                : t("admin.gc.failed")}
            </span>
          </div>
        )}

        {gcReport && (
          <div className="space-y-4 rounded-lg border border-slate-200 bg-slate-50 p-4">
            <div className="grid gap-3 sm:grid-cols-4">
              <div className="rounded-lg border border-slate-200 bg-white p-3">
                <div className="text-[11px] font-medium text-slate-500">
                  {t("admin.gc.freedSpace")}
                </div>
                <div className="mt-1 font-mono text-base font-bold text-slate-900">
                  {formatBytes(gcReport.freed_bytes)}
                </div>
                <div className="font-mono text-[11px] text-slate-400">
                  {t("common.bytes", {
                    value: formatNumber(gcReport.freed_bytes),
                  })}
                </div>
              </div>

              <div className="rounded-lg border border-slate-200 bg-white p-3">
                <div className="text-[11px] font-medium text-slate-500">
                  {t("admin.gc.deletedFiles")}
                </div>
                <div className="mt-1 font-mono text-base font-bold text-slate-900">
                  {formatNumber(gcReport.deleted_files_count)}
                </div>
              </div>

              <div className="rounded-lg border border-slate-200 bg-white p-3">
                <div className="text-[11px] font-medium text-slate-500">
                  {t("admin.gc.skippedPinned")}
                </div>
                <div className="mt-1 font-mono text-base font-bold text-amber-700">
                  {formatNumber(gcReport.skipped_pinned_count)}
                </div>
              </div>

              <div className="rounded-lg border border-slate-200 bg-white p-3">
                <div className="text-[11px] font-medium text-slate-500">
                  {t("admin.gc.emergencyMode")}
                </div>
                <div className="mt-1.5">
                  <span
                    className={`inline-flex rounded-full border px-2.5 py-0.5 text-xs font-medium ${
                      gcReport.emergency_triggered
                        ? "border-rose-200 bg-rose-50 text-rose-700"
                        : "border-emerald-200 bg-emerald-50 text-emerald-700"
                    }`}
                  >
                    {gcReport.emergency_triggered
                      ? t("admin.gc.triggered")
                      : t("admin.gc.standard")}
                  </span>
                </div>
              </div>
            </div>

            {gcPartitionRows.length > 0 && (
              <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
                <table className="min-w-full divide-y divide-slate-200 text-left text-xs">
                  <thead className="bg-slate-50 font-medium text-slate-500">
                    <tr>
                      <th className="px-3 py-2">{t("admin.gc.colPartition")}</th>
                      <th className="px-3 py-2">{t("admin.gc.colUsageQuota")}</th>
                      <th className="px-3 py-2">{t("admin.gc.colUsagePct")}</th>
                      <th className="px-3 py-2">{t("admin.gc.colFiles")}</th>
                      <th className="px-3 py-2">
                        {t("admin.gc.colFreedDeleted")}
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {gcPartitionRows.map((row) => (
                      <tr key={row.name}>
                        <td className="px-3 py-2 font-mono font-medium text-slate-800">
                          {row.name}
                        </td>
                        <td className="px-3 py-2 font-mono text-slate-600">
                          {row.usedBytes !== undefined &&
                          row.quotaBytes !== undefined
                            ? `${formatBytes(row.usedBytes)} / ${formatBytes(
                                row.quotaBytes,
                              )}`
                            : "—"}
                        </td>
                        <td className="px-3 py-2 font-mono text-slate-600">
                          {row.usagePercent !== undefined
                            ? `${row.usagePercent.toFixed(1)}%`
                            : "—"}
                        </td>
                        <td className="px-3 py-2 font-mono text-slate-600">
                          {row.fileCount !== undefined
                            ? formatNumber(row.fileCount)
                            : "—"}
                        </td>
                        <td className="px-3 py-2 font-mono text-slate-600">
                          {row.freedBytes !== undefined ||
                          row.deletedFilesCount !== undefined
                            ? `${
                                row.freedBytes !== undefined
                                  ? formatBytes(row.freedBytes)
                                  : "—"
                              } (${t("common.filesCount", {
                                count: row.deletedFilesCount ?? 0,
                              })})`
                            : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  );
}

export default AdminPage;
