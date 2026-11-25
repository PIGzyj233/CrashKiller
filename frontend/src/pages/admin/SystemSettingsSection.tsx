import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Save, Sliders } from "lucide-react";
import {
  ApiError,
  getSystemSettings,
  listLLMProviders,
  updateSystemSettings,
} from "../../api/client";
import type {
  LLMProviderId,
  LLMProviderPreset,
  LLMThinkingLevel,
  SystemSettingsUpdate,
} from "../../api/types";
import { useAppI18n } from "../../i18n";
import {
  CUSTOM_MODEL_SENTINEL,
  FALLBACK_PROVIDER_PRESETS,
  parseLineInput,
  parseListInput,
} from "./helpers";
import { LlmProviderFields } from "./LlmProviderFields";
import {
  QuotaRetentionFields,
  type QuotaRetentionFormValues,
} from "./QuotaRetentionFields";

const INITIAL_QUOTA_RETENTION: QuotaRetentionFormValues = {
  aiTriageMode: "SMART_TRIAGE",
  aiOutputLanguage: "zh-CN",
  aiMaxSteps: "10",
  aiTimeoutSeconds: "120",
  aiToolOutputMaxChars: "8000",
  totalDiskQuotaGb: "150",
  hostSafetyFloorGb: "50",
  gcThresholdPercent: "85",
  dbQuotaGb: "15",
  symbolsSymQuotaGb: "15",
  symbolsPublicCacheQuotaGb: "5",
  symbolsRawQuotaGb: "15",
  logsQuotaGb: "55",
  dumpsQuotaGb: "35",
  stagingQuotaGb: "10",
  logRetentionDays: "30",
  dumpRetentionDays: "14",
  stagingTtlHours: "6",
  archiveRawSymbols: false,
  enablePublicSymbols: true,
  publicSymbolUrlsText: "",
  defaultNoiseModulesText: "",
  defaultNoiseFunctionsText: "",
};

export function SystemSettingsSection() {
  const { t } = useAppI18n();
  const queryClient = useQueryClient();

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

  // LLM settings state
  const [llmProvider, setLlmProvider] = useState<LLMProviderId>("openai");
  const [llmBaseUrl, setLlmBaseUrl] = useState("https://api.openai.com/v1");
  const [llmModel, setLlmModel] = useState("gpt-4o-mini");
  const [useCustomModelInput, setUseCustomModelInput] = useState(false);
  const [llmThinkingLevel, setLlmThinkingLevel] =
    useState<LLMThinkingLevel>("auto");
  const [llmApiKeyInput, setLlmApiKeyInput] = useState("");
  const [showLlmApiKey, setShowLlmApiKey] = useState(false);

  // Guardrails, quotas & retention state
  const [formValues, setFormValues] = useState<QuotaRetentionFormValues>(
    INITIAL_QUOTA_RETENTION,
  );
  const [settingsSavedMsg, setSettingsSavedMsg] = useState<string | null>(null);
  const [settingsValidationError, setSettingsValidationError] = useState<
    string | null
  >(null);

  const activeProviderPreset = useMemo<LLMProviderPreset>(
    () =>
      providerPresets.find((p) => p.id === llmProvider) ?? providerPresets[0],
    [providerPresets, llmProvider],
  );

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
      !preset ||
        preset.models.length === 0 ||
        !preset.models.includes(s.llm_model),
    );

    setFormValues({
      aiTriageMode: s.ai_triage_mode,
      aiOutputLanguage: s.ai_output_language ?? "zh-CN",
      aiMaxSteps: String(s.ai_max_steps),
      aiTimeoutSeconds: String(s.ai_timeout_seconds),
      aiToolOutputMaxChars: String(s.ai_tool_output_max_chars),
      totalDiskQuotaGb: String(s.total_disk_quota_gb),
      hostSafetyFloorGb: String(s.host_safety_floor_gb),
      gcThresholdPercent: String(s.gc_threshold_percent),
      dbQuotaGb: String(s.db_quota_gb),
      symbolsSymQuotaGb: String(s.symbols_sym_quota_gb),
      symbolsPublicCacheQuotaGb: String(s.symbols_public_cache_quota_gb),
      symbolsRawQuotaGb: String(s.symbols_raw_quota_gb),
      logsQuotaGb: String(s.logs_quota_gb),
      dumpsQuotaGb: String(s.dumps_quota_gb),
      stagingQuotaGb: String(s.staging_quota_gb),
      logRetentionDays: String(s.log_retention_days),
      dumpRetentionDays: String(s.dump_retention_days),
      stagingTtlHours: String(s.staging_ttl_hours),
      archiveRawSymbols: s.archive_raw_symbols,
      enablePublicSymbols: s.enable_public_symbols,
      publicSymbolUrlsText: (s.public_symbol_urls ?? []).join("\n"),
      defaultNoiseModulesText: (s.default_noise_modules ?? []).join("\n"),
      defaultNoiseFunctionsText: (s.default_noise_functions ?? []).join("\n"),
    });
  }, [settingsQuery.data, providerPresets]);

  const handleFieldChange = <K extends keyof QuotaRetentionFormValues>(
    field: K,
    value: QuotaRetentionFormValues[K],
  ) => {
    setFormValues((prev) => ({ ...prev, [field]: value }));
  };

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
      {
        field: "ai_max_steps",
        raw: formValues.aiMaxSteps,
        min: 1,
        max: 50,
        integer: true,
      },
      {
        field: "ai_timeout_seconds",
        raw: formValues.aiTimeoutSeconds,
        min: 1,
        max: 3600,
        integer: true,
      },
      {
        field: "ai_tool_output_max_chars",
        raw: formValues.aiToolOutputMaxChars,
        min: 100,
        max: 200000,
        integer: true,
      },
      {
        field: "total_disk_quota_gb",
        raw: formValues.totalDiskQuotaGb,
        min: 0,
      },
      {
        field: "host_safety_floor_gb",
        raw: formValues.hostSafetyFloorGb,
        min: 0,
      },
      {
        field: "gc_threshold_percent",
        raw: formValues.gcThresholdPercent,
        min: 0,
        max: 100,
        exclusiveMin: true,
      },
      { field: "db_quota_gb", raw: formValues.dbQuotaGb, min: 0 },
      {
        field: "symbols_sym_quota_gb",
        raw: formValues.symbolsSymQuotaGb,
        min: 0,
      },
      {
        field: "symbols_public_cache_quota_gb",
        raw: formValues.symbolsPublicCacheQuotaGb,
        min: 0,
      },
      {
        field: "symbols_raw_quota_gb",
        raw: formValues.symbolsRawQuotaGb,
        min: 0,
      },
      { field: "logs_quota_gb", raw: formValues.logsQuotaGb, min: 0 },
      { field: "dumps_quota_gb", raw: formValues.dumpsQuotaGb, min: 0 },
      { field: "staging_quota_gb", raw: formValues.stagingQuotaGb, min: 0 },
      {
        field: "log_retention_days",
        raw: formValues.logRetentionDays,
        min: 1,
        max: 3650,
        integer: true,
      },
      {
        field: "dump_retention_days",
        raw: formValues.dumpRetentionDays,
        min: 1,
        max: 3650,
        integer: true,
      },
      {
        field: "staging_ttl_hours",
        raw: formValues.stagingTtlHours,
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
      ai_triage_mode: formValues.aiTriageMode,
      ai_output_language: formValues.aiOutputLanguage,
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
      archive_raw_symbols: formValues.archiveRawSymbols,
      enable_public_symbols: formValues.enablePublicSymbols,
      public_symbol_urls: parseLineInput(formValues.publicSymbolUrlsText),
      default_noise_modules: parseListInput(formValues.defaultNoiseModulesText),
      default_noise_functions: parseLineInput(
        formValues.defaultNoiseFunctionsText,
      ),
    };

    if (llmApiKeyInput.trim().length > 0) {
      payload.llm_api_key = llmApiKeyInput.trim();
    }

    saveSettingsMutation.mutate(payload);
  };

  const partitionQuotaSum = useMemo(() => {
    const vals = [
      formValues.dbQuotaGb,
      formValues.symbolsSymQuotaGb,
      formValues.symbolsPublicCacheQuotaGb,
      formValues.symbolsRawQuotaGb,
      formValues.logsQuotaGb,
      formValues.dumpsQuotaGb,
      formValues.stagingQuotaGb,
    ].map((v) => Number(v) || 0);
    return vals.reduce((acc, n) => acc + n, 0);
  }, [
    formValues.dbQuotaGb,
    formValues.symbolsSymQuotaGb,
    formValues.symbolsPublicCacheQuotaGb,
    formValues.symbolsRawQuotaGb,
    formValues.logsQuotaGb,
    formValues.dumpsQuotaGb,
    formValues.stagingQuotaGb,
  ]);

  const parsedTotalQuota = Number(formValues.totalDiskQuotaGb) || 0;
  const parsedFloorGb = Number(formValues.hostSafetyFloorGb) || 0;

  const presetSelectValue =
    !useCustomModelInput && activeProviderPreset.models.includes(llmModel)
      ? llmModel
      : CUSTOM_MODEL_SENTINEL;

  return (
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

          <LlmProviderFields
            providerPresets={providerPresets}
            activeProviderPreset={activeProviderPreset}
            llmProvider={llmProvider}
            llmBaseUrl={llmBaseUrl}
            llmModel={llmModel}
            presetSelectValue={presetSelectValue}
            llmThinkingLevel={llmThinkingLevel}
            llmApiKeyInput={llmApiKeyInput}
            showLlmApiKey={showLlmApiKey}
            llmApiKeySet={settingsQuery.data.llm_api_key_set}
            llmApiKeyMasked={settingsQuery.data.llm_api_key_masked}
            onProviderChange={handleProviderChange}
            onBaseUrlChange={setLlmBaseUrl}
            onModelSelectChange={handleModelSelectChange}
            onModelChange={setLlmModel}
            onThinkingLevelChange={setLlmThinkingLevel}
            onApiKeyInputChange={setLlmApiKeyInput}
            onToggleShowApiKey={() => setShowLlmApiKey((prev) => !prev)}
          />

          <QuotaRetentionFields
            values={formValues}
            partitionQuotaSum={partitionQuotaSum}
            parsedTotalQuota={parsedTotalQuota}
            parsedFloorGb={parsedFloorGb}
            onChange={handleFieldChange}
          />

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
  );
}
