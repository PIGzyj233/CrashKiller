import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  ArrowRight,
  CheckCircle2,
  Globe,
  Loader2,
  Settings,
  ShieldCheck,
} from "lucide-react";
import { updateSystemSettings } from "../api/client";
import { useAppI18n, type Locale } from "../i18n";

export function SettingsPage() {
  const { t, locale, setLocale, formatDateTime, formatNumber } = useAppI18n();
  const queryClient = useQueryClient();
  const [syncStatus, setSyncStatus] = useState<
    "idle" | "syncing" | "synced" | "error"
  >("idle");

  const syncAiLanguageMutation = useMutation({
    mutationFn: (nextLocale: Locale) =>
      updateSystemSettings({ ai_output_language: nextLocale }),
    onMutate: () => {
      setSyncStatus("syncing");
    },
    onSuccess: (updated) => {
      queryClient.setQueryData(["system-settings"], updated);
      setSyncStatus("synced");
    },
    onError: () => {
      setSyncStatus("error");
    },
  });

  const handleSelectLocale = (nextLocale: Locale) => {
    setLocale(nextLocale);
    syncAiLanguageMutation.mutate(nextLocale);
  };

  const languageOptions: Array<{
    value: Locale;
    label: string;
    sublabel: string;
    code: string;
  }> = [
    {
      value: "zh-CN",
      label: t("settings.zhLabel"),
      sublabel: t("settings.zhSublabel"),
      code: "zh-CN",
    },
    {
      value: "en-US",
      label: t("settings.enLabel"),
      sublabel: t("settings.enSublabel"),
      code: "en-US",
    },
  ];

  const sampleTimestamp = new Date().toISOString();

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-indigo-50 text-indigo-600">
            <Settings className="h-5 w-5" />
          </div>
          <div>
            <h1 className="text-xl font-semibold text-slate-900">
              {t("settings.title")}
            </h1>
            <p className="text-sm text-slate-500">{t("settings.subtitle")}</p>
          </div>
        </div>
      </div>

      {/* Interface Language Section */}
      <section className="space-y-5 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="flex items-start gap-2.5">
          <Globe className="mt-0.5 h-5 w-5 shrink-0 text-indigo-600" />
          <div>
            <h2 className="text-base font-semibold text-slate-900">
              {t("settings.languageSectionTitle")}
            </h2>
            <p className="mt-0.5 text-xs text-slate-500">
              {t("settings.languageSectionDesc")}
            </p>
          </div>
        </div>

        <div
          className="grid gap-3 sm:grid-cols-2"
          role="radiogroup"
          aria-label={t("settings.languageSectionTitle")}
        >
          {languageOptions.map((opt) => {
            const isActive = locale === opt.value;
            return (
              <button
                key={opt.value}
                type="button"
                role="radio"
                aria-checked={isActive}
                onClick={() => handleSelectLocale(opt.value)}
                data-testid={`locale-option-${opt.value}`}
                className={`flex items-start justify-between gap-3 rounded-xl border p-4 text-left transition-all ${
                  isActive
                    ? "border-indigo-500 bg-indigo-50/60 ring-1 ring-indigo-500"
                    : "border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50"
                }`}
              >
                <div>
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-semibold text-slate-900">
                      {opt.label}
                    </span>
                    <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-600">
                      {opt.code}
                    </span>
                  </div>
                  <p className="mt-1 text-xs text-slate-500">{opt.sublabel}</p>
                </div>
                {isActive && (
                  <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-indigo-200 bg-white px-2 py-0.5 text-[11px] font-medium text-indigo-700">
                    <CheckCircle2 className="h-3.5 w-3.5" />
                    {t("settings.currentBadge")}
                  </span>
                )}
              </button>
            );
          })}
        </div>

        {/* Live Format Preview */}
        <div className="rounded-lg border border-slate-200 bg-slate-50 p-4">
          <div className="text-xs font-semibold uppercase tracking-wider text-slate-600">
            {t("settings.previewTitle")}
          </div>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <div className="rounded-md border border-slate-200 bg-white px-3 py-2">
              <div className="text-[11px] text-slate-500">
                {t("settings.previewDateLabel")}
              </div>
              <div className="mt-0.5 font-mono text-xs font-medium text-slate-800">
                {formatDateTime(sampleTimestamp)}
              </div>
            </div>
            <div className="rounded-md border border-slate-200 bg-white px-3 py-2">
              <div className="text-[11px] text-slate-500">
                {t("settings.previewNumberLabel")}
              </div>
              <div className="mt-0.5 font-mono text-xs font-medium text-slate-800">
                {formatNumber(1234567)}
              </div>
            </div>
          </div>
          <p className="mt-3 text-xs text-slate-500">
            {t("settings.previewSavedHint")}
          </p>
          {syncStatus !== "idle" && (
            <p
              className={`mt-1.5 inline-flex items-center gap-1.5 text-xs ${
                syncStatus === "error"
                  ? "text-amber-700"
                  : syncStatus === "synced"
                    ? "text-emerald-700"
                    : "text-slate-500"
              }`}
            >
              {syncStatus === "syncing" && (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              )}
              {syncStatus === "synced" && (
                <CheckCircle2 className="h-3.5 w-3.5" />
              )}
              <span>
                {syncStatus === "syncing"
                  ? t("settings.aiLanguageSyncing")
                  : syncStatus === "synced"
                    ? t("settings.aiLanguageSynced")
                    : t("settings.aiLanguageSyncFailed")}
              </span>
            </p>
          )}
        </div>
      </section>

      {/* Shortcut to Admin Console */}
      <section className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="flex items-start gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-slate-100 text-slate-700">
            <ShieldCheck className="h-5 w-5" />
          </div>
          <div>
            <h2 className="text-base font-semibold text-slate-900">
              {t("settings.adminCardTitle")}
            </h2>
            <p className="mt-0.5 text-xs text-slate-500">
              {t("settings.adminCardDesc")}
            </p>
          </div>
        </div>
        <Link
          to="/admin"
          className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3.5 py-2 text-xs font-medium text-white shadow-sm hover:bg-indigo-700"
        >
          <span>{t("settings.goToAdmin")}</span>
          <ArrowRight className="h-3.5 w-3.5" />
        </Link>
      </section>
    </div>
  );
}

export default SettingsPage;
