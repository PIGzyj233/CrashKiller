import { Check, Copy, KeyRound, X } from "lucide-react";
import { useAppI18n } from "../../i18n";
import type { RevealedApiKeyInfo } from "./helpers";

interface RevealedApiKeyBannerProps {
  revealedKey: RevealedApiKeyInfo;
  copiedKey: boolean;
  onCopy: () => void;
  onDismiss: () => void;
}

export function RevealedApiKeyBanner({
  revealedKey,
  copiedKey,
  onCopy,
  onDismiss,
}: RevealedApiKeyBannerProps) {
  const { t } = useAppI18n();

  return (
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
          onClick={onDismiss}
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
          onClick={onCopy}
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
  );
}
