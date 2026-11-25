import { Brain, Cpu, Eye, EyeOff, RotateCcw } from "lucide-react";
import type {
  LLMProviderId,
  LLMProviderPreset,
  LLMThinkingLevel,
} from "../../api/types";
import { useAppI18n } from "../../i18n";
import { CUSTOM_MODEL_SENTINEL, THINKING_LEVELS } from "./helpers";

interface LlmProviderFieldsProps {
  providerPresets: readonly LLMProviderPreset[];
  activeProviderPreset: LLMProviderPreset;
  llmProvider: LLMProviderId;
  llmBaseUrl: string;
  llmModel: string;
  presetSelectValue: string;
  llmThinkingLevel: LLMThinkingLevel;
  llmApiKeyInput: string;
  showLlmApiKey: boolean;
  llmApiKeySet: boolean;
  llmApiKeyMasked?: string | null;
  onProviderChange: (nextId: LLMProviderId) => void;
  onBaseUrlChange: (val: string) => void;
  onModelSelectChange: (val: string) => void;
  onModelChange: (val: string) => void;
  onThinkingLevelChange: (level: LLMThinkingLevel) => void;
  onApiKeyInputChange: (val: string) => void;
  onToggleShowApiKey: () => void;
}

export function LlmProviderFields({
  providerPresets,
  activeProviderPreset,
  llmProvider,
  llmBaseUrl,
  llmModel,
  presetSelectValue,
  llmThinkingLevel,
  llmApiKeyInput,
  showLlmApiKey,
  llmApiKeySet,
  llmApiKeyMasked,
  onProviderChange,
  onBaseUrlChange,
  onModelSelectChange,
  onModelChange,
  onThinkingLevelChange,
  onApiKeyInputChange,
  onToggleShowApiKey,
}: LlmProviderFieldsProps) {
  const { t, formatThinkingLevel } = useAppI18n();

  const providerLabel = (id: LLMProviderId, fallbackName: string): string => {
    const key = `admin.system.providers.${id}` as const;
    return t(key, { defaultValue: fallbackName });
  };

  const thinkingDescKey = `thinkingLevel.desc.${llmThinkingLevel}` as const;

  return (
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
            onChange={(e) => onProviderChange(e.target.value as LLMProviderId)}
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
            {llmBaseUrl.trim() !== activeProviderPreset.default_base_url && (
              <button
                type="button"
                onClick={() =>
                  onBaseUrlChange(activeProviderPreset.default_base_url)
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
            onChange={(e) => onBaseUrlChange(e.target.value)}
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
                onChange={(e) => onModelSelectChange(e.target.value)}
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
                  onChange={(e) => onModelChange(e.target.value)}
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
              onChange={(e) => onModelChange(e.target.value)}
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
              {llmApiKeySet
                ? t("admin.system.apiKeySet", {
                    masked: llmApiKeyMasked ?? "****",
                  })
                : t("admin.system.apiKeyNotSet")}
            </span>
          </div>
          <div className="relative mt-1">
            <input
              id="admin-llm-api-key"
              type={showLlmApiKey ? "text" : "password"}
              value={llmApiKeyInput}
              onChange={(e) => onApiKeyInputChange(e.target.value)}
              placeholder={
                activeProviderPreset.requires_api_key
                  ? t("admin.system.apiKeyPlaceholder")
                  : t("admin.system.apiKeyOptionalPlaceholder")
              }
              className="h-9 w-full rounded-md border border-slate-300 bg-white pl-3 pr-9 font-mono text-xs text-slate-900"
            />
            <button
              type="button"
              onClick={onToggleShowApiKey}
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
                onClick={() => onThinkingLevelChange(level)}
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

        <p className="mt-2 text-xs text-slate-600">{t(thinkingDescKey)}</p>
        <p className="mt-1 text-[11px] text-slate-500">
          {t("admin.system.thinkingCompatibilityHint")}
        </p>
      </div>
    </div>
  );
}
