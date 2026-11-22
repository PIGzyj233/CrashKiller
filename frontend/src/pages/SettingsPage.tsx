import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  Copy,
  Eye,
  EyeOff,
  HardDrive,
  KeyRound,
  Loader2,
  Plus,
  RefreshCw,
  Save,
  Settings,
  Sliders,
  Trash2,
  X,
} from "lucide-react";
import {
  ApiError,
  createProject,
  getStoredProjectApiKey,
  getSystemSettings,
  rotateProjectKey,
  setStoredProjectApiKey,
  triggerGC,
  updateProject,
  updateSystemSettings,
} from "../api/client";
import type {
  AITriageMode,
  GCReport,
  ProjectRead,
  ProjectSettings,
  SystemSettingsUpdate,
} from "../api/types";
import { useProjectContext } from "../components/Layout";

const AI_TRIAGE_MODES: readonly AITriageMode[] = [
  "SMART_TRIAGE",
  "ALWAYS",
  "MANUAL",
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

export function SettingsPage() {
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

  // Edit Project state
  const [editingProjectId, setEditingProjectId] = useState<string>("");
  const [confirmRotateProjectId, setConfirmRotateProjectId] = useState<
    string | null
  >(null);
  const [editName, setEditName] = useState("");
  const [editAiTriageMode, setEditAiTriageMode] = useState<AITriageMode | "">(
    "",
  );
  const [editNoiseModulesText, setEditNoiseModulesText] = useState("");
  const [editNoiseFunctionsText, setEditNoiseFunctionsText] = useState("");
  const [editCustomHandlersText, setEditCustomHandlersText] = useState("");
  const [editLogRetentionDays, setEditLogRetentionDays] = useState("");
  const [editDumpRetentionDays, setEditDumpRetentionDays] = useState("");
  const [projectSavedMsg, setProjectSavedMsg] = useState<string | null>(null);

  // System Settings state
  const settingsQuery = useQuery({
    queryKey: ["system-settings"],
    queryFn: getSystemSettings,
  });

  const [llmBaseUrl, setLlmBaseUrl] = useState("");
  const [llmModel, setLlmModel] = useState("");
  const [llmApiKeyInput, setLlmApiKeyInput] = useState("");
  const [showLlmApiKey, setShowLlmApiKey] = useState(false);
  const [aiTriageMode, setAiTriageMode] =
    useState<AITriageMode>("SMART_TRIAGE");
  const [aiMaxSteps, setAiMaxSteps] = useState("10");
  const [aiTimeoutSeconds, setAiTimeoutSeconds] = useState("60");
  const [aiToolOutputMaxChars, setAiToolOutputMaxChars] = useState("12000");

  const [totalDiskQuotaGb, setTotalDiskQuotaGb] = useState("150");
  const [hostSafetyFloorGb, setHostSafetyFloorGb] = useState("50");
  const [gcThresholdPercent, setGcThresholdPercent] = useState("85");
  const [dbQuotaGb, setDbQuotaGb] = useState("10");
  const [symbolsSymQuotaGb, setSymbolsSymQuotaGb] = useState("20");
  const [symbolsPublicCacheQuotaGb, setSymbolsPublicCacheQuotaGb] =
    useState("10");
  const [symbolsRawQuotaGb, setSymbolsRawQuotaGb] = useState("30");
  const [logsQuotaGb, setLogsQuotaGb] = useState("40");
  const [dumpsQuotaGb, setDumpsQuotaGb] = useState("35");
  const [stagingQuotaGb, setStagingQuotaGb] = useState("5");

  const [logRetentionDays, setLogRetentionDays] = useState("30");
  const [dumpRetentionDays, setDumpRetentionDays] = useState("30");
  const [stagingTtlHours, setStagingTtlHours] = useState("24");
  const [archiveRawSymbols, setArchiveRawSymbols] = useState(true);
  const [enablePublicSymbols, setEnablePublicSymbols] = useState(false);
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

  const syncedProjectIdRef = useRef<string | null>(null);

  const syncProjectForm = (proj: ProjectRead) => {
    const s = proj.settings_json ?? {};
    setEditName(proj.name);
    setEditAiTriageMode(s.ai_triage_mode ?? "");
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

  // Sync project editor fields only when editingProject.id changes (preserve banner/draft across refetches)
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
    }
  }, [editingProject]);

  // Hydrate global settings form
  useEffect(() => {
    const s = settingsQuery.data;
    if (!s) {
      return;
    }
    setLlmBaseUrl(s.llm_base_url);
    setLlmModel(s.llm_model);
    setAiTriageMode(s.ai_triage_mode);
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
  }, [settingsQuery.data]);

  const createProjectMutation = useMutation({
    mutationFn: createProject,
    onSuccess: (created) => {
      setStoredProjectApiKey(created.key, created.api_key);
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
        setProjectSavedMsg(`Saved settings for ${updated.name}.`);
      }
      refreshProjects();
    },
  });

  const rotateKeyMutation = useMutation({
    mutationFn: (projectId: string) => rotateProjectKey(projectId),
    onSuccess: (rotated) => {
      setStoredProjectApiKey(rotated.key, rotated.api_key);
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
      setSettingsSavedMsg("Global system settings saved.");
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
          `Field "${field}" is required and cannot be blank.`,
        );
        return;
      }
      const val = Number(raw);
      if (!Number.isFinite(val)) {
        setSettingsValidationError(
          `Field "${field}" must be a valid finite number.`,
        );
        return;
      }
      if (integer && !Number.isInteger(val)) {
        setSettingsValidationError(
          `Field "${field}" must be a whole integer.`,
        );
        return;
      }
      const belowMin = exclusiveMin ? val <= min : val < min;
      if (belowMin || (max !== undefined && val > max)) {
        const minOp = exclusiveMin ? ">" : ">=";
        setSettingsValidationError(
          max !== undefined
            ? `Field "${field}" must be ${minOp} ${min} and <= ${max}.`
            : `Field "${field}" must be ${minOp} ${min}.`,
        );
        return;
      }
      parsedNums[field] = val;
    }

    const payload: SystemSettingsUpdate = {
      llm_base_url: llmBaseUrl.trim(),
      llm_model: llmModel.trim(),
      ai_triage_mode: aiTriageMode,
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
    if (!revealedKey || !navigator.clipboard) {
      return;
    }
    void navigator.clipboard.writeText(revealedKey.apiKey);
    setCopiedKey(true);
    window.setTimeout(() => setCopiedKey(false), 1800);
  };

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-indigo-50 text-indigo-600">
            <Settings className="h-5 w-5" />
          </div>
          <div>
            <h1 className="text-xl font-semibold text-slate-900">Settings</h1>
            <p className="text-sm text-slate-500">
              Projects, API keys, AI triage configuration, disk quotas, and
              manual GC
            </p>
          </div>
        </div>
        <span className="rounded-full border border-slate-200 bg-white px-3 py-1 text-xs text-slate-600">
          {projects.length} project(s) configured
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
                  Project API Key{" "}
                  {revealedKey.action === "created" ? "Created" : "Rotated"}:{" "}
                  {revealedKey.projectName} ({revealedKey.projectKey})
                </span>
              </div>
              <p className="text-xs text-emerald-800">
                Copy this API key now — it will not be shown again. It has also
                been saved to browser <code className="font-mono">localStorage</code>{" "}
                for UploadModal pre-fill.
              </p>
            </div>
            <button
              type="button"
              onClick={() => setRevealedKey(null)}
              className="rounded p-1 text-emerald-700 hover:bg-emerald-100"
              aria-label="Dismiss API key banner"
            >
              <X className="h-4 w-4" />
            </button>
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-2">
            <code className="flex-1 rounded-md border border-emerald-200 bg-white px-3 py-2 font-mono text-xs text-slate-900 break-all">
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
                  Copied
                </>
              ) : (
                <>
                  <Copy className="h-3.5 w-3.5" />
                  Copy API Key
                </>
              )}
            </button>
          </div>
        </div>
      )}

      {/* SECTION 1: PROJECT MANAGEMENT */}
      <section className="space-y-5 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="flex items-center gap-2">
          <KeyRound className="h-5 w-5 text-indigo-600" />
          <div>
            <h2 className="text-base font-semibold text-slate-900">
              Project Management &amp; API Keys
            </h2>
            <p className="text-xs text-slate-500">
              Create projects, rotate upload API keys, and override per-project
              triage &amp; clustering noise rules
            </p>
          </div>
        </div>

        {/* Create Project Form */}
        <form
          onSubmit={handleCreateProject}
          className="rounded-lg border border-slate-200 bg-slate-50 p-4"
        >
          <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-600">
            Create New Project
          </h3>
          {createProjectMutation.isError && (
            <div className="mt-2 rounded border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
              {createProjectMutation.error instanceof ApiError
                ? createProjectMutation.error.message
                : "Failed to create project."}
            </div>
          )}
          <div className="mt-3 grid gap-3 sm:grid-cols-4">
            <div>
              <label className="block text-xs font-medium text-slate-700">
                Project Key <span className="text-rose-500">*</span>
              </label>
              <input
                type="text"
                required
                value={newProjectKey}
                onChange={(e) => setNewProjectKey(e.target.value)}
                placeholder="e.g. engine-win64"
                className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-3 font-mono text-xs text-slate-900"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-700">
                Display Name <span className="text-rose-500">*</span>
              </label>
              <input
                type="text"
                required
                value={newProjectName}
                onChange={(e) => setNewProjectName(e.target.value)}
                placeholder="e.g. Game Engine Client"
                className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-700">
                AI Triage Mode
              </label>
              <select
                value={newProjectTriageMode}
                onChange={(e) =>
                  setNewProjectTriageMode(e.target.value as AITriageMode | "")
                }
                className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-900"
              >
                <option value="">Inherit Global Default</option>
                {AI_TRIAGE_MODES.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
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
                  ? "Creating..."
                  : "Create Project"}
              </button>
            </div>
          </div>
        </form>

        {rotateKeyMutation.isError && (
          <div className="flex items-center justify-between rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
            <span>
              {rotateKeyMutation.error instanceof ApiError
                ? rotateKeyMutation.error.message
                : "Failed to rotate project API key."}
            </span>
            <button
              type="button"
              onClick={() => rotateKeyMutation.reset()}
              className="rounded p-0.5 text-rose-600 hover:bg-rose-100"
              aria-label="Dismiss key rotation error"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        )}

        {/* Projects List + Per-Project Settings Editor */}
        {projects.length === 0 ? (
          <p className="text-sm text-slate-500">
            No projects created yet. Create a project above to receive an upload
            API key.
          </p>
        ) : (
          <div className="grid gap-6 lg:grid-cols-12">
            {/* List */}
            <div className="overflow-hidden rounded-lg border border-slate-200 lg:col-span-5">
              <div className="divide-y divide-slate-200">
                {projects.map((proj) => {
                  const isSelected = proj.id === editingProjectId;
                  const hasStoredKey = Boolean(getStoredProjectApiKey(proj.key));
                  const isConfirmingRotate =
                    confirmRotateProjectId === proj.id;

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
                                Key in Browser
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
                          {isSelected ? "Editing" : "Edit"}
                        </button>
                      </div>

                      <div className="mt-2.5 flex items-center justify-between border-t border-slate-100 pt-2 text-xs">
                        {isConfirmingRotate ? (
                          <div className="flex w-full items-center justify-between gap-2">
                            <span className="font-medium text-amber-800">
                              Invalidate old API key?
                            </span>
                            <div className="flex items-center gap-1.5">
                              <button
                                type="button"
                                onClick={() => setConfirmRotateProjectId(null)}
                                className="rounded border border-slate-300 bg-white px-2 py-0.5 text-slate-700"
                              >
                                Cancel
                              </button>
                              <button
                                type="button"
                                disabled={rotateKeyMutation.isPending}
                                onClick={() =>
                                  rotateKeyMutation.mutate(proj.id)
                                }
                                className="rounded bg-amber-600 px-2 py-0.5 font-medium text-white hover:bg-amber-700"
                              >
                                Confirm Rotate
                              </button>
                            </div>
                          </div>
                        ) : (
                          <button
                            type="button"
                            onClick={() => {
                              rotateKeyMutation.reset();
                              setConfirmRotateProjectId(proj.id);
                            }}
                            className="inline-flex items-center gap-1 font-medium text-slate-600 hover:text-indigo-600"
                          >
                            <RefreshCw className="h-3 w-3" />
                            Rotate API Key
                          </button>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Editor */}
            {editingProject && (
              <form
                onSubmit={handleSaveProject}
                className="space-y-4 rounded-lg border border-slate-200 p-4 lg:col-span-7"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h3 className="text-sm font-semibold text-slate-900">
                    Project Settings:{" "}
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
                      : "Failed to save project settings."}
                  </div>
                )}

                <div className="grid gap-3 sm:grid-cols-2">
                  <div>
                    <label className="block text-xs font-medium text-slate-700">
                      Project Name
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
                      AI Triage Mode Override
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
                      <option value="">Inherit Global Default</option>
                      {AI_TRIAGE_MODES.map((m) => (
                        <option key={m} value={m}>
                          {m}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>

                <div className="grid gap-3 sm:grid-cols-2">
                  <div>
                    <label className="block text-xs font-medium text-slate-700">
                      Log Retention Override (days)
                    </label>
                    <input
                      type="number"
                      min={1}
                      value={editLogRetentionDays}
                      onChange={(e) => setEditLogRetentionDays(e.target.value)}
                      placeholder="Inherit global"
                      className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 font-mono text-sm text-slate-900"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-slate-700">
                      Dump Retention Override (days)
                    </label>
                    <input
                      type="number"
                      min={1}
                      value={editDumpRetentionDays}
                      onChange={(e) => setEditDumpRetentionDays(e.target.value)}
                      placeholder="Inherit global"
                      className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 font-mono text-sm text-slate-900"
                    />
                  </div>
                </div>

                <div className="grid gap-3 sm:grid-cols-3">
                  <div>
                    <label className="block text-xs font-medium text-slate-700">
                      Noise Modules
                    </label>
                    <textarea
                      rows={3}
                      value={editNoiseModulesText}
                      onChange={(e) => setEditNoiseModulesText(e.target.value)}
                      placeholder="ntdll.dll&#10;kernel32.dll"
                      className="mt-1 w-full rounded-md border border-slate-300 p-2 font-mono text-xs text-slate-900"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-slate-700">
                      Noise Functions
                    </label>
                    <textarea
                      rows={3}
                      value={editNoiseFunctionsText}
                      onChange={(e) =>
                        setEditNoiseFunctionsText(e.target.value)
                      }
                      placeholder="abort&#10;raise"
                      className="mt-1 w-full rounded-md border border-slate-300 p-2 font-mono text-xs text-slate-900"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-slate-700">
                      Custom Crash Handlers
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
                      ? "Saving..."
                      : "Save Project Settings"}
                  </button>
                </div>
              </form>
            )}
          </div>
        )}
      </section>

      {/* SECTION 2: GLOBAL SYSTEM SETTINGS */}
      <section className="space-y-5 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Sliders className="h-5 w-5 text-indigo-600" />
            <div>
              <h2 className="text-base font-semibold text-slate-900">
                Global System Settings
              </h2>
              <p className="text-xs text-slate-500">
                OpenAI-compatible LLM triage config, AI guardrails, disk
                quotas, retention, and default clustering noise rules
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
                : "Failed to load global settings."}
            </span>
            <button
              type="button"
              onClick={() => void settingsQuery.refetch()}
              className="rounded border border-rose-300 bg-white px-2.5 py-1 text-xs font-medium text-rose-700"
            >
              Retry
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
                    : "Failed to save global settings.")}
              </div>
            )}

            {/* LLM & AI Guardrails */}
            <div className="space-y-3 rounded-lg border border-slate-200 p-4">
              <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-600">
                LLM &amp; AI Triage Guardrails
              </h3>

              <div className="grid gap-3 sm:grid-cols-3">
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    LLM Base URL
                  </label>
                  <input
                    type="text"
                    value={llmBaseUrl}
                    onChange={(e) => setLlmBaseUrl(e.target.value)}
                    placeholder="https://api.openai.com/v1"
                    className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 font-mono text-xs text-slate-900"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    LLM Model
                  </label>
                  <input
                    type="text"
                    value={llmModel}
                    onChange={(e) => setLlmModel(e.target.value)}
                    placeholder="gpt-4o-mini"
                    className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 font-mono text-xs text-slate-900"
                  />
                </div>
                <div>
                  <div className="flex items-center justify-between">
                    <label className="block text-xs font-medium text-slate-700">
                      LLM API Key
                    </label>
                    <span className="font-mono text-[11px] text-slate-500">
                      {settingsQuery.data.llm_api_key_set
                        ? `Set (${
                            settingsQuery.data.llm_api_key_masked ?? "masked"
                          })`
                        : "Not set"}
                    </span>
                  </div>
                  <div className="relative mt-1">
                    <input
                      type={showLlmApiKey ? "text" : "password"}
                      value={llmApiKeyInput}
                      onChange={(e) => setLlmApiKeyInput(e.target.value)}
                      placeholder="Leave blank to keep existing key"
                      className="h-9 w-full rounded-md border border-slate-300 pl-3 pr-9 font-mono text-xs text-slate-900"
                    />
                    <button
                      type="button"
                      onClick={() => setShowLlmApiKey((prev) => !prev)}
                      className="absolute right-2 top-2 text-slate-400 hover:text-slate-700"
                      aria-label={showLlmApiKey ? "Hide API key" : "Show API key"}
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

              <div className="grid gap-3 sm:grid-cols-4">
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    Default AI Triage Mode
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
                        {m}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    AI Max Steps
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
                    AI Timeout (sec)
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
                    Tool Output Max Chars
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
                  Disk Quotas &amp; 50GB Host Safety Floor
                </h3>
                <span
                  className={`font-mono text-xs ${
                    partitionQuotaSum > parsedTotalQuota
                      ? "font-semibold text-amber-700"
                      : "text-slate-500"
                  }`}
                >
                  Partition Sum: {partitionQuotaSum.toFixed(1)} GB / Total Cap:{" "}
                  {parsedTotalQuota.toFixed(1)} GB
                </span>
              </div>

              <div className="grid gap-3 sm:grid-cols-3">
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    Total Disk Quota (GB)
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
                      Host Safety Floor (GB)
                    </label>
                    {parsedFloorGb < 50 && (
                      <span className="text-[11px] font-medium text-amber-700">
                        50 GB recommended
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
                    GC Trigger Threshold (%)
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
                    DB (GB)
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
                    Sym .sym (GB)
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
                    Public Sym (GB)
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
                    Raw Sym (GB)
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
                    Logs (GB)
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
                    Dumps (GB)
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
                    Staging (GB)
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
                Retention, Symbols &amp; Default Noise Rules
              </h3>

              <div className="grid gap-3 sm:grid-cols-3">
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    Log Retention (days)
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
                    Dump Retention (days)
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
                    Staging TTL (hours)
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
                  Archive raw PDB/EXE/DLL (zstd) for WinDbg /symsrv
                </label>

                <label className="inline-flex cursor-pointer items-center gap-2 text-xs text-slate-700">
                  <input
                    type="checkbox"
                    checked={enablePublicSymbols}
                    onChange={(e) => setEnablePublicSymbols(e.target.checked)}
                    className="h-4 w-4 rounded border-slate-300 text-indigo-600"
                  />
                  Fetch missing Windows OS symbols from public symbol servers
                </label>
              </div>

              <div className="grid gap-3 sm:grid-cols-3">
                <div>
                  <label className="block text-xs font-medium text-slate-700">
                    Public Symbol URLs (one per line)
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
                    Default Noise Modules
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
                    Default Noise Functions
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
                  ? "Saving..."
                  : "Save Global Settings"}
              </button>
            </div>
          </form>
        )}
      </section>

      {/* SECTION 3: MANUAL GARBAGE COLLECTION & GCREPORT */}
      <section className="space-y-4 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="flex items-center gap-2">
            <HardDrive className="h-5 w-5 text-indigo-600" />
            <div>
              <h2 className="text-base font-semibold text-slate-900">
                Storage Garbage Collection (TTL / LRU)
              </h2>
              <p className="text-xs text-slate-500">
                Evict expired dumps, logs, and raw symbols while preserving all{" "}
                <code className="font-mono">is_pinned=true</code> crashes and
                issues.
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
              Force Emergency GC
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
              {gcMutation.isPending ? "Running GC..." : "Trigger Manual GC"}
            </button>
          </div>
        </div>

        {gcMutation.isError && (
          <div className="flex items-center gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
            <AlertTriangle className="h-4 w-4 shrink-0" />
            <span>
              {gcMutation.error instanceof ApiError
                ? gcMutation.error.message
                : "Manual GC run failed."}
            </span>
          </div>
        )}

        {gcReport && (
          <div className="space-y-4 rounded-lg border border-slate-200 bg-slate-50 p-4">
            <div className="grid gap-3 sm:grid-cols-4">
              <div className="rounded-lg border border-slate-200 bg-white p-3">
                <div className="text-[11px] font-medium text-slate-500">
                  Freed Space
                </div>
                <div className="mt-1 font-mono text-base font-bold text-slate-900">
                  {formatBytes(gcReport.freed_bytes)}
                </div>
                <div className="font-mono text-[11px] text-slate-400">
                  {gcReport.freed_bytes.toLocaleString()} bytes
                </div>
              </div>

              <div className="rounded-lg border border-slate-200 bg-white p-3">
                <div className="text-[11px] font-medium text-slate-500">
                  Deleted Files
                </div>
                <div className="mt-1 font-mono text-base font-bold text-slate-900">
                  {gcReport.deleted_files_count.toLocaleString()}
                </div>
              </div>

              <div className="rounded-lg border border-slate-200 bg-white p-3">
                <div className="text-[11px] font-medium text-slate-500">
                  Skipped (Pinned)
                </div>
                <div className="mt-1 font-mono text-base font-bold text-amber-700">
                  {gcReport.skipped_pinned_count.toLocaleString()}
                </div>
              </div>

              <div className="rounded-lg border border-slate-200 bg-white p-3">
                <div className="text-[11px] font-medium text-slate-500">
                  Emergency Mode
                </div>
                <div className="mt-1.5">
                  <span
                    className={`inline-flex rounded-full border px-2.5 py-0.5 text-xs font-medium ${
                      gcReport.emergency_triggered
                        ? "border-rose-200 bg-rose-50 text-rose-700"
                        : "border-emerald-200 bg-emerald-50 text-emerald-700"
                    }`}
                  >
                    {gcReport.emergency_triggered ? "Triggered" : "Standard"}
                  </span>
                </div>
              </div>
            </div>

            {gcPartitionRows.length > 0 && (
              <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
                <table className="min-w-full divide-y divide-slate-200 text-left text-xs">
                  <thead className="bg-slate-50 font-medium text-slate-500">
                    <tr>
                      <th className="px-3 py-2">Partition</th>
                      <th className="px-3 py-2">Usage / Quota</th>
                      <th className="px-3 py-2">Usage %</th>
                      <th className="px-3 py-2">Files</th>
                      <th className="px-3 py-2">Freed / Deleted</th>
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
                            ? row.fileCount.toLocaleString()
                            : "—"}
                        </td>
                        <td className="px-3 py-2 font-mono text-slate-600">
                          {row.freedBytes !== undefined ||
                          row.deletedFilesCount !== undefined
                            ? `${
                                row.freedBytes !== undefined
                                  ? formatBytes(row.freedBytes)
                                  : "—"
                              } (${row.deletedFilesCount ?? 0} files)`
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

export default SettingsPage;
