import { useRef, useState, type ChangeEvent, type DragEvent } from "react";
import { AlertTriangle, CheckCircle2, FileCode2, Trash2 } from "lucide-react";
import type { SymbolScope } from "../../api/types";
import { useAppI18n } from "../../i18n";
import type { UploadSymbolTargetHint } from "../UploadModal";
import { formatFileSize } from "./utils";

export interface SymbolScopeSelectorProps {
  symbolScope: SymbolScope;
  isPublicSymbolMode: boolean;
  symbolTargetHint: UploadSymbolTargetHint | null;
  isUploading: boolean;
  onSelectScope: (scope: SymbolScope) => void;
}

export function SymbolScopeSelector({
  symbolScope,
  isPublicSymbolMode,
  symbolTargetHint,
  isUploading,
  onSelectScope,
}: SymbolScopeSelectorProps) {
  const { t } = useAppI18n();

  return (
    <div className="space-y-2.5">
      <label className="block text-xs font-medium text-slate-700">
        {t("upload.symbolScopeLabel")}
      </label>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <button
          type="button"
          disabled={isUploading}
          onClick={() => onSelectScope("public")}
          className={`rounded-lg border px-3 py-2 text-left text-xs font-medium transition-colors ${
            symbolScope === "public"
              ? "border-indigo-500 bg-indigo-50 text-indigo-800 ring-1 ring-indigo-500"
              : "border-slate-200 bg-white text-slate-700 hover:bg-slate-50"
          }`}
        >
          {t("upload.symbolScopePublic")}
        </button>
        <button
          type="button"
          disabled={isUploading}
          onClick={() => onSelectScope("project")}
          className={`rounded-lg border px-3 py-2 text-left text-xs font-medium transition-colors ${
            symbolScope === "project"
              ? "border-indigo-500 bg-indigo-50 text-indigo-800 ring-1 ring-indigo-500"
              : "border-slate-200 bg-white text-slate-700 hover:bg-slate-50"
          }`}
        >
          {t("upload.symbolScopeProject")}
        </button>
      </div>

      {symbolTargetHint?.debug_file && symbolTargetHint?.debug_id && (
        <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 font-mono text-[11px] text-slate-700">
          {t("upload.publicTargetHint", {
            file: symbolTargetHint.debug_file,
            debugId: symbolTargetHint.debug_id,
          })}
        </div>
      )}

      {isPublicSymbolMode && (
        <div className="rounded-lg border border-indigo-200 bg-indigo-50/70 px-3.5 py-2 text-xs text-indigo-900">
          {t("upload.publicScopeHint")}
        </div>
      )}
    </div>
  );
}

export interface SymbolUploadFieldsProps {
  isPublicSymbolMode: boolean;
  isDirectSymSelected: boolean;
  pdbFile: File | null;
  peFile: File | null;
  autoResymbolicate: boolean;
  isUploading: boolean;
  onPdbFileSelected: (files: FileList | null) => void;
  onClearPdbFile: () => void;
  onPeFileSelected: (files: FileList | null) => void;
  onClearPeFile: () => void;
  onAutoResymbolicateChange: (checked: boolean) => void;
}

export function SymbolUploadFields({
  isPublicSymbolMode,
  isDirectSymSelected,
  pdbFile,
  peFile,
  autoResymbolicate,
  isUploading,
  onPdbFileSelected,
  onClearPdbFile,
  onPeFileSelected,
  onClearPeFile,
  onAutoResymbolicateChange,
}: SymbolUploadFieldsProps) {
  const { t } = useAppI18n();
  const [isPdbDragOver, setIsPdbDragOver] = useState(false);
  const [isPeDragOver, setIsPeDragOver] = useState(false);
  const pdbInputRef = useRef<HTMLInputElement | null>(null);
  const peInputRef = useRef<HTMLInputElement | null>(null);

  return (
    <>
      {/* .pdb (or .sym for public) Dropzone */}
      <div>
        <label className="mb-1 block text-xs font-medium text-slate-700">
          {isPublicSymbolMode
            ? t("upload.publicPdbOrSymLabel")
            : t("upload.pdbFileLabel")}{" "}
          <span className="text-rose-500">*</span>
        </label>
        <input
          ref={pdbInputRef}
          type="file"
          accept={isPublicSymbolMode ? ".pdb,.sym" : ".pdb"}
          className="hidden"
          onChange={(e: ChangeEvent<HTMLInputElement>) => {
            onPdbFileSelected(e.target.files);
            e.target.value = "";
          }}
        />
        <div
          role="button"
          tabIndex={0}
          onClick={() => {
            if (!isUploading) pdbInputRef.current?.click();
          }}
          onKeyDown={(e) => {
            if ((e.key === "Enter" || e.key === " ") && !isUploading) {
              e.preventDefault();
              pdbInputRef.current?.click();
            }
          }}
          onDragOver={(e: DragEvent<HTMLDivElement>) => {
            e.preventDefault();
            if (!isUploading) setIsPdbDragOver(true);
          }}
          onDragLeave={() => setIsPdbDragOver(false)}
          onDrop={(e: DragEvent<HTMLDivElement>) => {
            e.preventDefault();
            setIsPdbDragOver(false);
            if (!isUploading) {
              onPdbFileSelected(e.dataTransfer.files);
            }
          }}
          className={`flex cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed px-4 py-5 text-center transition-colors ${
            isPdbDragOver
              ? "border-indigo-500 bg-indigo-50/70"
              : pdbFile
                ? "border-emerald-300 bg-emerald-50/40"
                : "border-slate-300 bg-slate-50/60 hover:border-indigo-400 hover:bg-slate-50"
          }`}
        >
          {pdbFile ? (
            <div className="flex w-full items-center justify-between gap-3">
              <div className="flex min-w-0 items-center gap-2.5 text-left">
                <FileCode2 className="h-5 w-5 shrink-0 text-emerald-600" />
                <div className="min-w-0">
                  <p className="truncate font-mono text-xs font-semibold text-slate-900">
                    {pdbFile.name}
                  </p>
                  <p className="font-mono text-[11px] text-slate-500">
                    {formatFileSize(pdbFile.size)}
                  </p>
                </div>
              </div>
              <button
                type="button"
                disabled={isUploading}
                onClick={(e) => {
                  e.stopPropagation();
                  onClearPdbFile();
                }}
                className="rounded p-1 text-slate-400 hover:bg-slate-200 hover:text-slate-700"
                aria-label={t("upload.removePdbAria")}
              >
                <Trash2 className="h-4 w-4" />
              </button>
            </div>
          ) : (
            <>
              <FileCode2 className="h-6 w-6 text-slate-400" />
              <p className="mt-1.5 text-xs font-medium text-slate-700">
                {isPublicSymbolMode ? (
                  <>
                    {t("upload.dropPublicPdbTextBefore")}
                    <span className="font-mono">.pdb / .sym</span>
                    {t("upload.dropPublicPdbTextAfter")}
                  </>
                ) : (
                  <>
                    {t("upload.dropPdbTextBefore")}
                    <span className="font-mono">.pdb</span>
                    {t("upload.dropPdbTextAfter")}
                  </>
                )}
              </p>
            </>
          )}
        </div>
      </div>

      {/* Optional PE Companion (.exe / .dll) */}
      {!isDirectSymSelected && (
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-700">
            {t("upload.peFileLabel")}
          </label>
          <input
            ref={peInputRef}
            type="file"
            accept=".exe,.dll"
            className="hidden"
            onChange={(e: ChangeEvent<HTMLInputElement>) => {
              onPeFileSelected(e.target.files);
              e.target.value = "";
            }}
          />
          <div
            role="button"
            tabIndex={0}
            onClick={() => {
              if (!isUploading) peInputRef.current?.click();
            }}
            onKeyDown={(e) => {
              if ((e.key === "Enter" || e.key === " ") && !isUploading) {
                e.preventDefault();
                peInputRef.current?.click();
              }
            }}
            onDragOver={(e: DragEvent<HTMLDivElement>) => {
              e.preventDefault();
              if (!isUploading) setIsPeDragOver(true);
            }}
            onDragLeave={() => setIsPeDragOver(false)}
            onDrop={(e: DragEvent<HTMLDivElement>) => {
              e.preventDefault();
              setIsPeDragOver(false);
              if (!isUploading) {
                onPeFileSelected(e.dataTransfer.files);
              }
            }}
            className={`flex cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed px-4 py-4 text-center transition-colors ${
              isPeDragOver
                ? "border-indigo-500 bg-indigo-50/70"
                : peFile
                  ? "border-emerald-300 bg-emerald-50/40"
                  : "border-slate-300 bg-slate-50/60 hover:border-indigo-400 hover:bg-slate-50"
            }`}
          >
            {peFile ? (
              <div className="flex w-full items-center justify-between gap-3">
                <div className="flex min-w-0 items-center gap-2.5 text-left">
                  <CheckCircle2 className="h-5 w-5 shrink-0 text-emerald-600" />
                  <div className="min-w-0">
                    <p className="truncate font-mono text-xs font-semibold text-slate-900">
                      {peFile.name}
                    </p>
                    <p className="font-mono text-[11px] text-slate-500">
                      {formatFileSize(peFile.size)}
                    </p>
                  </div>
                </div>
                <button
                  type="button"
                  disabled={isUploading}
                  onClick={(e) => {
                    e.stopPropagation();
                    onClearPeFile();
                  }}
                  className="rounded p-1 text-slate-400 hover:bg-slate-200 hover:text-slate-700"
                  aria-label={t("upload.removePeAria")}
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            ) : (
              <p className="text-xs text-slate-600">
                {t("upload.dropPeTextBefore")}
                <span className="font-mono">.exe</span>
                {t("upload.dropPeTextMid")}
                <span className="font-mono">.dll</span>
                {t("upload.dropPeTextAfter")}
              </p>
            )}
          </div>
        </div>
      )}

      {/* Mandatory PE Unwind Warning or Confirmation */}
      {!isDirectSymSelected &&
        (!peFile ? (
          <div
            role="alert"
            data-testid="pe-unwind-warning"
            className="flex items-start gap-2.5 rounded-lg border border-amber-200 bg-amber-50 px-3.5 py-2.5 text-xs text-amber-900"
          >
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
            <div>
              <p className="font-semibold">{t("upload.peWarningTitle")}</p>
              <p className="mt-0.5 text-amber-800">{t("upload.peWarningDesc")}</p>
            </div>
          </div>
        ) : (
          <div className="flex items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3.5 py-2 text-xs text-emerald-800">
            <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600" />
            <span>{t("upload.peAttachedOk")}</span>
          </div>
        ))}

      {isPublicSymbolMode && (
        <label className="flex cursor-pointer items-start gap-2 text-xs text-slate-700">
          <input
            type="checkbox"
            checked={autoResymbolicate}
            disabled={isUploading}
            onChange={(e) => onAutoResymbolicateChange(e.target.checked)}
            className="mt-0.5 h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
          />
          <span>{t("upload.autoResymAfterUpload")}</span>
        </label>
      )}
    </>
  );
}
