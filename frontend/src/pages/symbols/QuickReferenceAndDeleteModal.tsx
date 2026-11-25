import { Link } from "react-router-dom";
import { Check, Copy, Terminal, Trash2 } from "lucide-react";
import { ApiError } from "../../api/client";
import type { SymbolRead } from "../../api/types";
import { useAppI18n } from "../../i18n";

interface SymbolQuickReferenceCardProps {
  windbgSymPath: string;
  cliSnippet: string;
  copiedText: string | null;
  onCopy: (text: string) => void;
}

export function SymbolQuickReferenceCard({
  windbgSymPath,
  cliSnippet,
  copiedText,
  onCopy,
}: SymbolQuickReferenceCardProps) {
  const { t } = useAppI18n();

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex items-center gap-2">
        <Terminal className="h-4 w-4 text-indigo-600" />
        <h2 className="text-sm font-semibold text-slate-900">
          {t("symbols.quickRefTitleBefore")}
          <code className="font-mono">/symsrv</code>
          {t("symbols.quickRefTitleAfter")}
        </h2>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <div className="rounded-lg border border-slate-200 bg-slate-50 p-3.5">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-800">
              {t("symbols.windbgPathTitle")}
            </span>
            <button
              type="button"
              onClick={() => onCopy(windbgSymPath)}
              className="inline-flex items-center gap-1 rounded border border-slate-200 bg-white px-2 py-0.5 text-[11px] font-medium text-slate-700 hover:bg-slate-100"
            >
              {copiedText === windbgSymPath ? (
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
          <pre className="mt-2 overflow-x-auto rounded bg-slate-950 p-2.5 font-mono text-xs text-slate-100">
            {windbgSymPath}
          </pre>
          <p className="mt-2 text-[11px] text-slate-500">
            {t("symbols.windbgHintBefore")}
            <Link to="/admin" className="text-indigo-600 hover:underline">
              {t("symbols.windbgHintLink")}
            </Link>
            {t("symbols.windbgHintMiddle")}
            <code className="font-mono">archive_raw_symbols</code>
            {t("symbols.windbgHintAfter")}
          </p>
        </div>

        <div className="rounded-lg border border-slate-200 bg-slate-50 p-3.5">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-800">
              {t("symbols.cliTitleBefore")}
              <code className="font-mono">crashkiller_cli.py</code>
              {t("symbols.cliTitleAfter")}
            </span>
            <button
              type="button"
              onClick={() => onCopy(cliSnippet)}
              className="inline-flex items-center gap-1 rounded border border-slate-200 bg-white px-2 py-0.5 text-[11px] font-medium text-slate-700 hover:bg-slate-100"
            >
              {copiedText === cliSnippet ? (
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
          <pre className="mt-2 overflow-x-auto rounded bg-slate-950 p-2.5 font-mono text-xs text-slate-100">
            {cliSnippet}
          </pre>
          <p className="mt-2 text-[11px] text-slate-500">
            {t("symbols.cliHintBefore")}
            <code className="font-mono">.pdb</code>
            {t("symbols.cliHintMid1")}
            <code className="font-mono">.exe/.dll</code>
            {t("symbols.cliHintMid2")}
            <code className="font-mono">has_pe_unwind</code>
            {t("symbols.cliHintAfter")}
          </p>
        </div>
      </div>
    </div>
  );
}

interface DeleteSymbolModalProps {
  symbol: SymbolRead;
  isPending: boolean;
  isError: boolean;
  error: unknown;
  onCancel: () => void;
  onConfirm: () => void;
}

export function DeleteSymbolModal({
  symbol,
  isPending,
  isError,
  error,
  onCancel,
  onConfirm,
}: DeleteSymbolModalProps) {
  const { t } = useAppI18n();

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="delete-symbol-modal-title"
    >
      <div className="w-full max-w-md rounded-xl border border-slate-200 bg-white p-6 shadow-xl">
        <h2
          id="delete-symbol-modal-title"
          className="text-base font-semibold text-slate-900"
        >
          {t("symbols.deleteModalTitle")}
        </h2>
        <p className="mt-2 text-sm text-slate-600">
          {t("symbols.deleteModalDescBefore")}
          <code className="font-mono font-semibold text-slate-900">
            {symbol.debug_file}
          </code>{" "}
          (<code className="font-mono text-xs">{symbol.debug_id}</code>)
          {t("symbols.deleteModalDescAfter")}
        </p>

        {isError && (
          <div className="mt-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
            {error instanceof ApiError
              ? error.message
              : t("symbols.deleteFailed")}
          </div>
        )}

        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
          >
            {t("common.cancel")}
          </button>
          <button
            type="button"
            disabled={isPending}
            onClick={onConfirm}
            className="inline-flex items-center gap-1.5 rounded-md bg-rose-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-rose-700 disabled:opacity-50"
          >
            <Trash2 className="h-4 w-4" />
            {isPending ? t("common.deleting") : t("symbols.confirmDelete")}
          </button>
        </div>
      </div>
    </div>
  );
}
