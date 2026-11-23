import i18n from "i18next";
import { initReactI18next, useTranslation } from "react-i18next";
import type {
  AIRunStatus,
  AITriageMode,
  AIVerdict,
  CrashStatus,
  IssueStatus,
  LLMThinkingLevel,
} from "../api/types";
import zhCN from "./locales/zh-CN";
import enUS from "./locales/en-US";

export type Locale = "zh-CN" | "en-US";

export const LOCALE_STORAGE_KEY = "crashkiller.locale";
export const DEFAULT_LOCALE: Locale = "zh-CN";

export function normalizeLocale(raw: string | null | undefined): Locale {
  if (!raw) {
    return DEFAULT_LOCALE;
  }
  const lower = raw.trim().toLowerCase();
  if (lower.startsWith("en")) {
    return "en-US";
  }
  return "zh-CN";
}

export function readStoredLocale(): Locale {
  if (typeof window === "undefined") {
    return DEFAULT_LOCALE;
  }
  try {
    const stored = window.localStorage.getItem(LOCALE_STORAGE_KEY);
    return stored ? normalizeLocale(stored) : DEFAULT_LOCALE;
  } catch {
    return DEFAULT_LOCALE;
  }
}

export function writeStoredLocale(locale: Locale): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, locale);
  } catch {
    // Ignore storage quota or privacy restrictions
  }
}

function syncDocumentMetadata(locale: Locale): void {
  if (typeof document === "undefined") {
    return;
  }
  document.documentElement.lang = locale;
  document.title = i18n.t("app.title", { lng: locale });
}

const initialLocale = readStoredLocale();

void i18n.use(initReactI18next).init({
  resources: {
    "zh-CN": { translation: zhCN },
    "en-US": { translation: enUS },
  },
  lng: initialLocale,
  fallbackLng: DEFAULT_LOCALE,
  interpolation: {
    escapeValue: false,
  },
});

syncDocumentMetadata(initialLocale);

i18n.on("languageChanged", (lng) => {
  const normalized = normalizeLocale(lng);
  writeStoredLocale(normalized);
  syncDocumentMetadata(normalized);
});

export function setAppLocale(locale: Locale): void {
  const normalized = normalizeLocale(locale);
  writeStoredLocale(normalized);
  void i18n.changeLanguage(normalized);
}

export function useAppI18n() {
  const { t, i18n: i18nInstance } = useTranslation();
  const locale = normalizeLocale(i18nInstance.resolvedLanguage ?? i18nInstance.language);

  const formatDateTime = (iso?: string | null): string => {
    if (!iso) {
      return "—";
    }
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) {
      return iso;
    }
    return d.toLocaleString(locale);
  };

  const formatNumber = (value: number): string => {
    if (!Number.isFinite(value)) {
      return "0";
    }
    return value.toLocaleString(locale);
  };

  const formatCrashStatus = (status: CrashStatus | string): string => {
    switch (status) {
      case "PENDING":
        return t("status.crash.PENDING");
      case "PARSING":
        return t("status.crash.PARSING");
      case "TRIAGING":
        return t("status.crash.TRIAGING");
      case "ANALYZED":
        return t("status.crash.ANALYZED");
      case "FAILED":
        return t("status.crash.FAILED");
      default:
        return status;
    }
  };

  const formatIssueStatus = (status: IssueStatus | string): string => {
    switch (status) {
      case "NEW":
        return t("status.issue.NEW");
      case "KNOWN":
        return t("status.issue.KNOWN");
      case "FIXED":
        return t("status.issue.FIXED");
      case "WONTFIX":
        return t("status.issue.WONTFIX");
      case "REGRESSION":
        return t("status.issue.REGRESSION");
      default:
        return status;
    }
  };

  const formatAIRunStatus = (status: AIRunStatus | string): string => {
    switch (status) {
      case "RUNNING":
        return t("status.aiRun.RUNNING");
      case "COMPLETED":
        return t("status.aiRun.COMPLETED");
      case "FAILED":
        return t("status.aiRun.FAILED");
      default:
        return status;
    }
  };

  const formatVerdict = (verdict?: AIVerdict | string | null): string => {
    switch (verdict) {
      case "KNOWN_ISSUE":
        return t("verdict.KNOWN_ISSUE");
      case "NEW_ISSUE":
        return t("verdict.NEW_ISSUE");
      case "REGRESSION":
        return t("verdict.REGRESSION");
      case "NEEDS_REVIEW":
        return t("verdict.NEEDS_REVIEW");
      case "UNANALYZED":
      case null:
      case undefined:
      case "":
        return t("verdict.UNANALYZED");
      default:
        return verdict;
    }
  };

  const formatTriageMode = (mode: AITriageMode | string): string => {
    switch (mode) {
      case "SMART_TRIAGE":
        return t("triageMode.SMART_TRIAGE");
      case "ALWAYS":
        return t("triageMode.ALWAYS");
      case "MANUAL":
        return t("triageMode.MANUAL");
      default:
        return mode;
    }
  };

  const formatThinkingLevel = (level: LLMThinkingLevel | string): string => {
    switch (level) {
      case "auto":
        return t("thinkingLevel.auto");
      case "off":
        return t("thinkingLevel.off");
      case "low":
        return t("thinkingLevel.low");
      case "medium":
        return t("thinkingLevel.medium");
      case "high":
        return t("thinkingLevel.high");
      default:
        return level;
    }
  };

  return {
    t,
    i18n: i18nInstance,
    locale,
    setLocale: setAppLocale,
    formatDateTime,
    formatNumber,
    formatCrashStatus,
    formatIssueStatus,
    formatAIRunStatus,
    formatVerdict,
    formatTriageMode,
    formatThinkingLevel,
  };
}

export { i18n };
export default i18n;
