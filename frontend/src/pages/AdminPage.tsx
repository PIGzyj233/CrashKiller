import { useState } from "react";
import { ShieldCheck } from "lucide-react";
import { useProjectContext } from "../components/Layout";
import { useAppI18n } from "../i18n";
import { GarbageCollectionSection } from "./admin/GarbageCollectionSection";
import { copyTextToClipboard, type RevealedApiKeyInfo } from "./admin/helpers";
import { ProjectManagementSection } from "./admin/ProjectManagementSection";
import { RevealedApiKeyBanner } from "./admin/RevealedApiKeyBanner";
import { SystemSettingsSection } from "./admin/SystemSettingsSection";

export function AdminPage() {
  const { t } = useAppI18n();
  const { projects } = useProjectContext();

  const [revealedKey, setRevealedKey] = useState<RevealedApiKeyInfo | null>(
    null,
  );
  const [copiedKey, setCopiedKey] = useState(false);

  const handleKeyRevealed = (info: RevealedApiKeyInfo) => {
    setRevealedKey(info);
    setCopiedKey(false);
  };

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
        <RevealedApiKeyBanner
          revealedKey={revealedKey}
          copiedKey={copiedKey}
          onCopy={handleCopyRevealedKey}
          onDismiss={() => setRevealedKey(null)}
        />
      )}

      {/* SECTION 1: GLOBAL SYSTEM & LLM PROVIDER SETTINGS */}
      <SystemSettingsSection />

      {/* SECTION 2: PROJECT MANAGEMENT */}
      <ProjectManagementSection onKeyRevealed={handleKeyRevealed} />

      {/* SECTION 3: MANUAL GARBAGE COLLECTION & GCREPORT */}
      <GarbageCollectionSection />
    </div>
  );
}

export default AdminPage;
