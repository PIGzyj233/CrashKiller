import { useRef, useState, type ChangeEvent, type DragEvent } from "react";
import { FileText, HardDriveUpload, Trash2, X } from "lucide-react";
import type { ProjectRead } from "../../api/types";
import { useAppI18n } from "../../i18n";
import { getSessionIdLabel } from "../../utils/sessionLabel";
import { formatFileSize, getFileDedupKey } from "./utils";

export interface CrashUploadFieldsProps {
  selectedProject: ProjectRead | undefined;
  dumpFile: File | null;
  logFiles: File[];
  sessionId: string;
  buildId: string;
  osVersion: string;
  crashTime: string;
  isUploading: boolean;
  onDumpFilesSelected: (files: FileList | null) => void;
  onClearDumpFile: () => void;
  onAppendLogFiles: (files: File[]) => void;
  onRemoveLogFile: (dedupKey: string) => void;
  onSessionIdChange: (value: string) => void;
  onBuildIdChange: (value: string) => void;
  onOsVersionChange: (value: string) => void;
  onCrashTimeChange: (value: string) => void;
}

export function CrashUploadFields({
  selectedProject,
  dumpFile,
  logFiles,
  sessionId,
  buildId,
  osVersion,
  crashTime,
  isUploading,
  onDumpFilesSelected,
  onClearDumpFile,
  onAppendLogFiles,
  onRemoveLogFile,
  onSessionIdChange,
  onBuildIdChange,
  onOsVersionChange,
  onCrashTimeChange,
}: CrashUploadFieldsProps) {
  const { t } = useAppI18n();
  const [isDumpDragOver, setIsDumpDragOver] = useState(false);
  const [isLogDragOver, setIsLogDragOver] = useState(false);
  const dumpInputRef = useRef<HTMLInputElement | null>(null);
  const logInputRef = useRef<HTMLInputElement | null>(null);

  return (
    <>
      {/* .dmp Dropzone */}
      <div>
        <label className="mb-1 block text-xs font-medium text-slate-700">
          {t("upload.dumpFileLabel")} <span className="text-rose-500">*</span>
        </label>
        <input
          ref={dumpInputRef}
          type="file"
          accept=".dmp"
          className="hidden"
          onChange={(e: ChangeEvent<HTMLInputElement>) => {
            onDumpFilesSelected(e.target.files);
            e.target.value = "";
          }}
        />
        <div
          role="button"
          tabIndex={0}
          onClick={() => {
            if (!isUploading) {
              dumpInputRef.current?.click();
            }
          }}
          onKeyDown={(e) => {
            if ((e.key === "Enter" || e.key === " ") && !isUploading) {
              e.preventDefault();
              dumpInputRef.current?.click();
            }
          }}
          onDragOver={(e: DragEvent<HTMLDivElement>) => {
            e.preventDefault();
            if (!isUploading) setIsDumpDragOver(true);
          }}
          onDragLeave={() => setIsDumpDragOver(false)}
          onDrop={(e: DragEvent<HTMLDivElement>) => {
            e.preventDefault();
            setIsDumpDragOver(false);
            if (!isUploading) {
              onDumpFilesSelected(e.dataTransfer.files);
            }
          }}
          className={`flex cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed px-4 py-5 text-center transition-colors ${
            isDumpDragOver
              ? "border-indigo-500 bg-indigo-50/70"
              : dumpFile
                ? "border-emerald-300 bg-emerald-50/40"
                : "border-slate-300 bg-slate-50/60 hover:border-indigo-400 hover:bg-slate-50"
          }`}
        >
          {dumpFile ? (
            <div className="flex w-full items-center justify-between gap-3">
              <div className="flex min-w-0 items-center gap-2.5 text-left">
                <HardDriveUpload className="h-5 w-5 shrink-0 text-emerald-600" />
                <div className="min-w-0">
                  <p className="truncate font-mono text-xs font-semibold text-slate-900">
                    {dumpFile.name}
                  </p>
                  <p className="font-mono text-[11px] text-slate-500">
                    {formatFileSize(dumpFile.size)}
                  </p>
                </div>
              </div>
              <button
                type="button"
                disabled={isUploading}
                onClick={(e) => {
                  e.stopPropagation();
                  onClearDumpFile();
                }}
                className="rounded p-1 text-slate-400 hover:bg-slate-200 hover:text-slate-700"
                aria-label={t("upload.removeDumpAria")}
              >
                <Trash2 className="h-4 w-4" />
              </button>
            </div>
          ) : (
            <>
              <HardDriveUpload className="h-6 w-6 text-slate-400" />
              <p className="mt-1.5 text-xs font-medium text-slate-700">
                {t("upload.dropDumpTextBefore")}
                <span className="font-mono">.dmp</span>
                {t("upload.dropDumpTextAfter")}
              </p>
              <p className="mt-0.5 text-[11px] text-slate-500">
                {t("upload.dropDumpTip")}
              </p>
            </>
          )}
        </div>
      </div>

      {/* Optional Log Files Dropzone */}
      <div>
        <label className="mb-1 block text-xs font-medium text-slate-700">
          {t("upload.logFilesLabel")}
        </label>
        <input
          ref={logInputRef}
          type="file"
          multiple
          accept=".log,.txt,.out"
          className="hidden"
          onChange={(e: ChangeEvent<HTMLInputElement>) => {
            if (e.target.files && e.target.files.length > 0) {
              onAppendLogFiles(Array.from(e.target.files));
            }
            e.target.value = "";
          }}
        />
        <div
          role="button"
          tabIndex={0}
          onClick={() => {
            if (!isUploading) {
              logInputRef.current?.click();
            }
          }}
          onKeyDown={(e) => {
            if ((e.key === "Enter" || e.key === " ") && !isUploading) {
              e.preventDefault();
              logInputRef.current?.click();
            }
          }}
          onDragOver={(e: DragEvent<HTMLDivElement>) => {
            e.preventDefault();
            if (!isUploading) setIsLogDragOver(true);
          }}
          onDragLeave={() => setIsLogDragOver(false)}
          onDrop={(e: DragEvent<HTMLDivElement>) => {
            e.preventDefault();
            setIsLogDragOver(false);
            if (!isUploading && e.dataTransfer.files.length > 0) {
              onAppendLogFiles(Array.from(e.dataTransfer.files));
            }
          }}
          className={`flex cursor-pointer items-center justify-center gap-2 rounded-lg border-2 border-dashed px-4 py-3 text-xs transition-colors ${
            isLogDragOver
              ? "border-indigo-500 bg-indigo-50/70 text-indigo-700"
              : "border-slate-300 bg-slate-50/60 text-slate-600 hover:border-indigo-400 hover:bg-slate-50"
          }`}
        >
          <FileText className="h-4 w-4 text-slate-400" />
          <span>
            {t("upload.dropLogsTextBefore")}
            <span className="font-mono">.log / .txt</span>
            {t("upload.dropLogsTextAfter", {
              count: logFiles.length,
            })}
          </span>
        </div>

        {logFiles.length > 0 && (
          <ul className="ck-scrollbar mt-2 max-h-36 divide-y divide-slate-100 overflow-y-auto rounded-lg border border-slate-200 bg-white">
            {logFiles.map((file) => {
              const key = getFileDedupKey(file);
              return (
                <li
                  key={key}
                  className="flex items-center justify-between gap-2 px-3 py-1.5 text-xs"
                >
                  <span className="min-w-0 truncate font-mono text-slate-800">
                    {file.name}
                  </span>
                  <div className="flex shrink-0 items-center gap-2">
                    <span className="font-mono text-[11px] text-slate-400">
                      {formatFileSize(file.size)}
                    </span>
                    <button
                      type="button"
                      disabled={isUploading}
                      onClick={() => onRemoveLogFile(key)}
                      aria-label={t("upload.removeFileAria", {
                        name: file.name,
                      })}
                      className="rounded p-0.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {/* Optional Crash Metadata */}
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <label
            htmlFor="upload-session-id"
            className="mb-1 block text-xs font-medium text-slate-700"
          >
            {t("upload.sessionIdLabel", {
              label: getSessionIdLabel(selectedProject, t("common.sessionId")),
            })}
          </label>
          <input
            id="upload-session-id"
            type="text"
            maxLength={128}
            value={sessionId}
            disabled={isUploading}
            onChange={(e) => onSessionIdChange(e.target.value)}
            placeholder={t("upload.sessionIdPlaceholder")}
            className="h-8 w-full rounded-md border border-slate-300 bg-white px-2.5 font-mono text-xs text-slate-800 placeholder:font-sans placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
          />
        </div>
        <div>
          <label
            htmlFor="upload-build-id"
            className="mb-1 block text-xs font-medium text-slate-700"
          >
            {t("upload.buildIdLabel")}
          </label>
          <input
            id="upload-build-id"
            type="text"
            value={buildId}
            disabled={isUploading}
            onChange={(e) => onBuildIdChange(e.target.value)}
            placeholder={t("upload.buildIdPlaceholder")}
            className="h-8 w-full rounded-md border border-slate-300 bg-white px-2.5 font-mono text-xs text-slate-800 placeholder:font-sans placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
          />
        </div>
        <div>
          <label
            htmlFor="upload-os-version"
            className="mb-1 block text-xs font-medium text-slate-700"
          >
            {t("upload.osVersionLabel")}
          </label>
          <input
            id="upload-os-version"
            type="text"
            value={osVersion}
            disabled={isUploading}
            onChange={(e) => onOsVersionChange(e.target.value)}
            placeholder={t("upload.osVersionPlaceholder")}
            className="h-8 w-full rounded-md border border-slate-300 bg-white px-2.5 text-xs text-slate-800 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
          />
        </div>
        <div>
          <label
            htmlFor="upload-crash-time"
            className="mb-1 block text-xs font-medium text-slate-700"
          >
            {t("upload.crashTimeLabel")}
          </label>
          <input
            id="upload-crash-time"
            type="datetime-local"
            value={crashTime}
            disabled={isUploading}
            onChange={(e) => onCrashTimeChange(e.target.value)}
            className="h-8 w-full rounded-md border border-slate-300 bg-white px-2 text-xs text-slate-800 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
          />
        </div>
      </div>
    </>
  );
}
