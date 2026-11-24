import { useEffect, useState, type FormEvent } from "react";
import { AlertTriangle, CheckCircle2, CloudDownload, Loader2, X } from "lucide-react";
import { ApiError, syncPublicSymbols } from "../api/client";
import type { PublicSymbolSyncResponse } from "../api/types";
import { useAppI18n } from "../i18n";

export interface PublicSymbolSyncModalProps {
  isOpen: boolean;
  onClose: () => void;
  initialName?: string;
  initialDebugId?: string;
  initialCodeId?: string;
  onSynced?: (result: PublicSymbolSyncResponse) => void;
}

export function PublicSymbolSyncModal({
  isOpen,
  onClose,
  initialName = "",
  initialDebugId = "",
  initialCodeId = "",
  onSynced,
}: PublicSymbolSyncModalProps) {
  const { t } = useAppI18n();
  const [nameInput, setNameInput] = useState(initialName);
  const [debugIdInput, setDebugIdInput] = useState(initialDebugId);
  const [codeIdInput, setCodeIdInput] = useState(initialCodeId);
  const [force, setForce] = useState(true);
  const [autoResym, setAutoResym] = useState(true);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [lastResult, setLastResult] = useState<PublicSymbolSyncResponse | null>(null);

  useEffect(() => {
    if (!isOpen) return;
    setNameInput(initialName);
    setDebugIdInput(initialDebugId);
    setCodeIdInput(initialCodeId);
    setForce(true);
    setAutoResym(true);
    setIsSubmitting(false);
    setErrorMessage(null);
    setLastResult(null);
  }, [isOpen, initialName, initialDebugId, initialCodeId]);

  useEffect(() => {
    if (!isOpen) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !isSubmitting) {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [isOpen, isSubmitting, onClose]);

  if (!isOpen) {
    return null;
  }

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (isSubmitting) return;

    const trimmedName = nameInput.trim();
    const trimmedDebugId = debugIdInput.trim();
    const trimmedCodeId = codeIdInput.trim();

    if (!trimmedName) {
      setErrorMessage(t("symbols.syncModalErrName"));
      return;
    }
    if (trimmedDebugId.length < 32) {
      setErrorMessage(t("symbols.syncModalErrDebugId"));
      return;
    }

    setIsSubmitting(true);
    setErrorMessage(null);
    setLastResult(null);

    try {
      const res = await syncPublicSymbols({
        mode: "targets",
        targets: [
          {
            module_name: trimmedName,
            debug_id: trimmedDebugId,
            code_id: trimmedCodeId || undefined,
          },
        ],
        force,
        auto_resymbolicate: autoResym,
      });
      setLastResult(res);
      onSynced?.(res);
    } catch (err) {
      setErrorMessage(
        err instanceof ApiError ? err.message : t("symbols.syncFailed"),
      );
    } finally {
      setIsSubmitting(false);
    }
  };

  const firstItem = lastResult?.results[0];
  const isSuccess =
    firstItem &&
    (firstItem.status === "imported" ||
      firstItem.status === "upgraded" ||
      firstItem.status === "cached_hit");

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="public-sync-modal-title"
    >
      <div
        className="fixed inset-0 bg-slate-900/60 backdrop-blur-[1px]"
        onClick={() => {
          if (!isSubmitting) onClose();
        }}
        aria-hidden="true"
      />

      <div className="relative z-10 w-full max-w-lg overflow-hidden rounded-xl border border-slate-200 bg-white shadow-xl">
        <div className="flex items-center justify-between border-b border-slate-200 bg-slate-50 px-5 py-3.5">
          <div className="flex items-center gap-2.5">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-indigo-600 text-white">
              <CloudDownload className="h-4 w-4" />
            </div>
            <div>
              <h2
                id="public-sync-modal-title"
                className="text-sm font-semibold text-slate-900"
              >
                {t("symbols.syncModalTitle")}
              </h2>
              <p className="text-xs text-slate-500">
                {t("symbols.syncModalSubtitle")}
              </p>
            </div>
          </div>
          <button
            type="button"
            disabled={isSubmitting}
            onClick={onClose}
            className="rounded-md p-1.5 text-slate-400 hover:bg-slate-200/70 hover:text-slate-700 disabled:opacity-50"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <form onSubmit={(e) => void handleSubmit(e)}>
          <div className="space-y-4 px-5 py-4">
            {errorMessage && (
              <div
                role="alert"
                className="flex items-start gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3.5 py-2.5 text-xs text-rose-800"
              >
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-rose-600" />
                <span className="break-words">{errorMessage}</span>
              </div>
            )}

            {firstItem && (
              <div
                className={`flex items-start gap-2 rounded-lg border px-3.5 py-2.5 text-xs ${
                  isSuccess
                    ? "border-emerald-200 bg-emerald-50 text-emerald-900"
                    : "border-amber-200 bg-amber-50 text-amber-900"
                }`}
              >
                {isSuccess ? (
                  <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
                ) : (
                  <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
                )}
                <div className="space-y-0.5">
                  <div className="font-semibold">
                    {firstItem.debug_file} ({firstItem.debug_id}) —{" "}
                    {firstItem.status}
                  </div>
                  {firstItem.detail && <div>{firstItem.detail}</div>}
                  {isSuccess && (
                    <div>
                      {firstItem.has_pe_unwind
                        ? t("symbols.peUnwindOk")
                        : t("symbols.peUnwindMissing")}
                      {lastResult && lastResult.resymbolicate_queued_count > 0
                        ? ` · ${t("symbols.resymQueuedPrefix")}${lastResult.resymbolicate_queued_count}${t("symbols.resymQueuedSuffix")}`
                        : ""}
                    </div>
                  )}
                </div>
              </div>
            )}

            <div>
              <label
                htmlFor="msdl-sync-name"
                className="mb-1 block text-xs font-medium text-slate-700"
              >
                {t("symbols.syncModalNameLabel")}{" "}
                <span className="text-rose-500">*</span>
              </label>
              <input
                id="msdl-sync-name"
                type="text"
                value={nameInput}
                disabled={isSubmitting}
                onChange={(e) => setNameInput(e.target.value)}
                placeholder={t("symbols.syncModalNamePlaceholder")}
                className="h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 font-mono text-xs text-slate-800 placeholder:font-sans placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
              />
            </div>

            <div>
              <label
                htmlFor="msdl-sync-debug-id"
                className="mb-1 block text-xs font-medium text-slate-700"
              >
                {t("symbols.syncModalDebugIdLabel")}{" "}
                <span className="text-rose-500">*</span>
              </label>
              <input
                id="msdl-sync-debug-id"
                type="text"
                value={debugIdInput}
                disabled={isSubmitting}
                onChange={(e) => setDebugIdInput(e.target.value)}
                placeholder={t("symbols.syncModalDebugIdPlaceholder")}
                className="h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 font-mono text-xs text-slate-800 placeholder:font-sans placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
              />
            </div>

            <div>
              <label
                htmlFor="msdl-sync-code-id"
                className="mb-1 block text-xs font-medium text-slate-700"
              >
                {t("symbols.syncModalCodeIdLabel")}
              </label>
              <input
                id="msdl-sync-code-id"
                type="text"
                value={codeIdInput}
                disabled={isSubmitting}
                onChange={(e) => setCodeIdInput(e.target.value)}
                placeholder={t("symbols.syncModalCodeIdPlaceholder")}
                className="h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 font-mono text-xs text-slate-800 placeholder:font-sans placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
              />
            </div>

            <div className="space-y-2 pt-1">
              <label className="flex cursor-pointer items-start gap-2 text-xs text-slate-700">
                <input
                  type="checkbox"
                  checked={force}
                  disabled={isSubmitting}
                  onChange={(e) => setForce(e.target.checked)}
                  className="mt-0.5 h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
                />
                <span>{t("symbols.syncModalForceLabel")}</span>
              </label>

              <label className="flex cursor-pointer items-start gap-2 text-xs text-slate-700">
                <input
                  type="checkbox"
                  checked={autoResym}
                  disabled={isSubmitting}
                  onChange={(e) => setAutoResym(e.target.checked)}
                  className="mt-0.5 h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
                />
                <span>{t("symbols.syncModalAutoResymLabel")}</span>
              </label>
            </div>
          </div>

          <div className="flex items-center justify-end gap-2.5 border-t border-slate-200 bg-slate-50 px-5 py-3">
            <button
              type="button"
              disabled={isSubmitting}
              onClick={onClose}
              className="rounded-md border border-slate-300 bg-white px-3.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-100 disabled:opacity-50"
            >
              {t("common.cancel")}
            </button>
            <button
              type="submit"
              disabled={isSubmitting}
              className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-4 py-1.5 text-xs font-medium text-white shadow-sm hover:bg-indigo-700 disabled:opacity-60"
            >
              {isSubmitting ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  <span>{t("symbols.syncingBtn")}</span>
                </>
              ) : (
                <>
                  <CloudDownload className="h-3.5 w-3.5" />
                  <span>{t("symbols.syncModalSubmitBtn")}</span>
                </>
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default PublicSymbolSyncModal;
