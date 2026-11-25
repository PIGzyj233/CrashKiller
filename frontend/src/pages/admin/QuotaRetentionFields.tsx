import type { AIOutputLanguage, AITriageMode } from "../../api/types";
import { useAppI18n } from "../../i18n";
import { AI_TRIAGE_MODES } from "./helpers";

export interface QuotaRetentionFormValues {
  aiTriageMode: AITriageMode;
  aiOutputLanguage: AIOutputLanguage;
  aiMaxSteps: string;
  aiTimeoutSeconds: string;
  aiToolOutputMaxChars: string;
  totalDiskQuotaGb: string;
  hostSafetyFloorGb: string;
  gcThresholdPercent: string;
  dbQuotaGb: string;
  symbolsSymQuotaGb: string;
  symbolsPublicCacheQuotaGb: string;
  symbolsRawQuotaGb: string;
  logsQuotaGb: string;
  dumpsQuotaGb: string;
  stagingQuotaGb: string;
  logRetentionDays: string;
  dumpRetentionDays: string;
  stagingTtlHours: string;
  archiveRawSymbols: boolean;
  enablePublicSymbols: boolean;
  publicSymbolUrlsText: string;
  defaultNoiseModulesText: string;
  defaultNoiseFunctionsText: string;
}

interface QuotaRetentionFieldsProps {
  values: QuotaRetentionFormValues;
  partitionQuotaSum: number;
  parsedTotalQuota: number;
  parsedFloorGb: number;
  onChange: <K extends keyof QuotaRetentionFormValues>(
    field: K,
    value: QuotaRetentionFormValues[K],
  ) => void;
}

export function QuotaRetentionFields({
  values,
  partitionQuotaSum,
  parsedTotalQuota,
  parsedFloorGb,
  onChange,
}: QuotaRetentionFieldsProps) {
  const { t, formatTriageMode } = useAppI18n();

  return (
    <>
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
              value={values.aiTriageMode}
              onChange={(e) =>
                onChange("aiTriageMode", e.target.value as AITriageMode)
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
              value={values.aiOutputLanguage}
              onChange={(e) =>
                onChange("aiOutputLanguage", e.target.value as AIOutputLanguage)
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
              value={values.aiMaxSteps}
              onChange={(e) => onChange("aiMaxSteps", e.target.value)}
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
              value={values.aiTimeoutSeconds}
              onChange={(e) => onChange("aiTimeoutSeconds", e.target.value)}
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
              value={values.aiToolOutputMaxChars}
              onChange={(e) => onChange("aiToolOutputMaxChars", e.target.value)}
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
              value={values.totalDiskQuotaGb}
              onChange={(e) => onChange("totalDiskQuotaGb", e.target.value)}
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
              value={values.hostSafetyFloorGb}
              onChange={(e) => onChange("hostSafetyFloorGb", e.target.value)}
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
              value={values.gcThresholdPercent}
              onChange={(e) => onChange("gcThresholdPercent", e.target.value)}
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
              value={values.dbQuotaGb}
              onChange={(e) => onChange("dbQuotaGb", e.target.value)}
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
              value={values.symbolsSymQuotaGb}
              onChange={(e) => onChange("symbolsSymQuotaGb", e.target.value)}
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
              value={values.symbolsPublicCacheQuotaGb}
              onChange={(e) =>
                onChange("symbolsPublicCacheQuotaGb", e.target.value)
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
              value={values.symbolsRawQuotaGb}
              onChange={(e) => onChange("symbolsRawQuotaGb", e.target.value)}
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
              value={values.logsQuotaGb}
              onChange={(e) => onChange("logsQuotaGb", e.target.value)}
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
              value={values.dumpsQuotaGb}
              onChange={(e) => onChange("dumpsQuotaGb", e.target.value)}
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
              value={values.stagingQuotaGb}
              onChange={(e) => onChange("stagingQuotaGb", e.target.value)}
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
              value={values.logRetentionDays}
              onChange={(e) => onChange("logRetentionDays", e.target.value)}
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
              value={values.dumpRetentionDays}
              onChange={(e) => onChange("dumpRetentionDays", e.target.value)}
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
              value={values.stagingTtlHours}
              onChange={(e) => onChange("stagingTtlHours", e.target.value)}
              className="mt-1 h-9 w-full rounded-md border border-slate-300 px-3 font-mono text-sm text-slate-900"
            />
          </div>
        </div>

        <div className="flex flex-wrap gap-6">
          <label className="inline-flex cursor-pointer items-center gap-2 text-xs text-slate-700">
            <input
              type="checkbox"
              checked={values.archiveRawSymbols}
              onChange={(e) => onChange("archiveRawSymbols", e.target.checked)}
              className="h-4 w-4 rounded border-slate-300 text-indigo-600"
            />
            {t("admin.system.archiveRawSymbols")}
          </label>

          <label className="inline-flex cursor-pointer items-center gap-2 text-xs text-slate-700">
            <input
              type="checkbox"
              checked={values.enablePublicSymbols}
              onChange={(e) =>
                onChange("enablePublicSymbols", e.target.checked)
              }
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
              value={values.publicSymbolUrlsText}
              onChange={(e) => onChange("publicSymbolUrlsText", e.target.value)}
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
              value={values.defaultNoiseModulesText}
              onChange={(e) =>
                onChange("defaultNoiseModulesText", e.target.value)
              }
              className="mt-1 w-full rounded-md border border-slate-300 p-2 font-mono text-xs text-slate-900"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-700">
              {t("admin.system.defaultNoiseFunctions")}
            </label>
            <textarea
              rows={3}
              value={values.defaultNoiseFunctionsText}
              onChange={(e) =>
                onChange("defaultNoiseFunctionsText", e.target.value)
              }
              className="mt-1 w-full rounded-md border border-slate-300 p-2 font-mono text-xs text-slate-900"
            />
          </div>
        </div>
      </div>
    </>
  );
}
