import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { AlertTriangle, Loader2, Upload, X } from "lucide-react";
import {
  getStoredProjectApiKey,
  removeStoredProjectApiKey,
  setStoredProjectApiKey,
  uploadCrash,
  uploadPublicSymbol,
  uploadSymbol,
} from "../api/client";
import type { ProjectRead, SymbolScope } from "../api/types";
import { useAppI18n } from "../i18n";
import { CrashUploadFields } from "./uploadModal/CrashUploadFields";
import { ProjectAuthFields } from "./uploadModal/ProjectAuthFields";
import {
  SymbolScopeSelector,
  SymbolUploadFields,
} from "./uploadModal/SymbolUploadFields";
import {
  formatUploadError,
  getFileDedupKey,
  isBlockedLogFile,
  mergeUniqueFiles,
  normalizeCrashTime,
  resolveInitialProjectKey,
} from "./uploadModal/utils";

export interface UploadSymbolTargetHint {
  module_name?: string;
  debug_file?: string;
  debug_id?: string;
}

export interface UploadModalProps {
  isOpen: boolean;
  onClose: () => void;
  mode: "crash" | "symbol";
  projects: ProjectRead[];
  defaultProjectKey?: string;
  defaultSymbolScope?: SymbolScope;
  symbolTargetHint?: UploadSymbolTargetHint | null;
  onSuccess?: (
    result:
      | { mode: "crash"; crashId: string }
      | {
          mode: "symbol";
          symbolId: string;
          scope?: SymbolScope;
          resymbolicateCrashIds?: string[];
          resymbolicateActiveCrashIds?: string[];
          resymbolicateSkippedActiveCount?: number;
        },
  ) => void;
}

export function UploadModal({
  isOpen,
  onClose,
  mode,
  projects,
  defaultProjectKey,
  defaultSymbolScope = "project",
  symbolTargetHint = null,
  onSuccess,
}: UploadModalProps) {
  const { t } = useAppI18n();
  const [activeMode, setActiveMode] = useState<"crash" | "symbol">(mode);
  const [symbolScope, setSymbolScope] =
    useState<SymbolScope>(defaultSymbolScope);
  const [autoResymbolicate, setAutoResymbolicate] = useState(true);
  const [selectedProjectKey, setSelectedProjectKey] = useState<string>(() =>
    resolveInitialProjectKey(projects, defaultProjectKey),
  );
  const [apiKey, setApiKey] = useState<string>("");
  const [rememberApiKey, setRememberApiKey] = useState(true);
  const [showApiKey, setShowApiKey] = useState(false);
  const [isApiKeyPrefilled, setIsApiKeyPrefilled] = useState(false);
  const [appVersion, setAppVersion] = useState("");

  // Crash fields
  const [dumpFile, setDumpFile] = useState<File | null>(null);
  const [logFiles, setLogFiles] = useState<File[]>([]);
  const [buildId, setBuildId] = useState("");
  const [osVersion, setOsVersion] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [crashTime, setCrashTime] = useState("");

  // Symbol fields
  const [pdbFile, setPdbFile] = useState<File | null>(null);
  const [peFile, setPeFile] = useState<File | null>(null);

  // Upload state
  const [isUploading, setIsUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const abortControllerRef = useRef<AbortController | null>(null);
  const wasOpenRef = useRef(false);

  // Reset form when modal transitions from closed -> open; abort active XHR when closed
  useEffect(() => {
    if (isOpen && !wasOpenRef.current) {
      const initialKey = resolveInitialProjectKey(projects, defaultProjectKey);
      const storedKey = getStoredProjectApiKey(initialKey);
      setActiveMode(mode);
      setSymbolScope(defaultSymbolScope);
      setAutoResymbolicate(true);
      setSelectedProjectKey(initialKey);
      setApiKey(storedKey ?? "");
      setIsApiKeyPrefilled(Boolean(storedKey));
      setRememberApiKey(true);
      setShowApiKey(false);
      setAppVersion("");
      setDumpFile(null);
      setLogFiles([]);
      setBuildId("");
      setOsVersion("");
      setSessionId("");
      setCrashTime("");
      setPdbFile(null);
      setPeFile(null);
      setIsUploading(false);
      setProgress(0);
      setErrorMessage(null);
    } else if (!isOpen && wasOpenRef.current) {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
        abortControllerRef.current = null;
      }
    }
    wasOpenRef.current = isOpen;
  }, [isOpen, mode, projects, defaultProjectKey, defaultSymbolScope]);

  // If projects arrive asynchronously while open and none is selected yet
  useEffect(() => {
    if (isOpen && !selectedProjectKey && projects.length > 0) {
      const resolved = resolveInitialProjectKey(projects, defaultProjectKey);
      setSelectedProjectKey(resolved);
    }
  }, [isOpen, selectedProjectKey, projects, defaultProjectKey]);

  // Re-load stored API key whenever selectedProjectKey changes while open
  useEffect(() => {
    if (!isOpen) {
      return;
    }
    if (!selectedProjectKey) {
      setApiKey("");
      setIsApiKeyPrefilled(false);
      return;
    }
    const stored = getStoredProjectApiKey(selectedProjectKey);
    setApiKey(stored ?? "");
    setIsApiKeyPrefilled(Boolean(stored));
  }, [isOpen, selectedProjectKey]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
        abortControllerRef.current = null;
      }
    };
  }, []);

  const handleClose = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setIsUploading(false);
    setProgress(0);
    setErrorMessage(null);
    onClose();
  }, [onClose]);

  // Escape key & body scroll lock while open
  useEffect(() => {
    if (!isOpen) {
      return;
    }
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        handleClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      document.body.style.overflow = prevOverflow;
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [isOpen, handleClose]);

  const appendLogFiles = (incoming: File[]) => {
    const valid: File[] = [];
    for (const f of incoming) {
      if (isBlockedLogFile(f.name)) {
        setErrorMessage(t("upload.errBinaryInLogs", { name: f.name }));
        return;
      }
      valid.push(f);
    }
    setErrorMessage(null);
    setLogFiles((prev) => mergeUniqueFiles(prev, valid));
  };

  const handleDumpFilesSelected = (files: FileList | null) => {
    if (!files || files.length === 0) {
      return;
    }
    const list = Array.from(files);
    const dmpCandidates = list.filter((f) =>
      f.name.toLowerCase().endsWith(".dmp"),
    );
    const nonDmp = list.filter((f) => !f.name.toLowerCase().endsWith(".dmp"));

    if (dmpCandidates.length > 1) {
      setErrorMessage(t("upload.errMultiDmp"));
      return;
    }
    if (dmpCandidates.length === 0) {
      setErrorMessage(t("upload.errNeedDmpExt"));
      return;
    }
    setDumpFile(dmpCandidates[0]);
    setErrorMessage(null);
    if (nonDmp.length > 0) {
      appendLogFiles(nonDmp);
    }
  };

  const isPublicSymbolMode =
    activeMode === "symbol" && symbolScope === "public";
  const isDirectSymSelected = Boolean(
    pdbFile && pdbFile.name.toLowerCase().endsWith(".sym"),
  );

  const handlePdbFileSelected = (files: FileList | null) => {
    if (!files || files.length === 0) {
      return;
    }
    const file = files[0];
    const lower = file.name.toLowerCase();
    if (isPublicSymbolMode) {
      if (!lower.endsWith(".pdb") && !lower.endsWith(".sym")) {
        setErrorMessage(t("upload.errNeedPublicSymExt"));
        return;
      }
      if (lower.endsWith(".sym")) {
        setPeFile(null);
      }
    } else if (!lower.endsWith(".pdb")) {
      setErrorMessage(t("upload.errNeedPdbExt"));
      return;
    }
    setPdbFile(file);
    setErrorMessage(null);
  };

  const handlePeFileSelected = (files: FileList | null) => {
    if (!files || files.length === 0) {
      return;
    }
    if (isDirectSymSelected) {
      setErrorMessage(t("upload.errSymWithPeUnsupported"));
      return;
    }
    const file = files[0];
    if (!/\.(exe|dll)$/i.test(file.name)) {
      setErrorMessage(t("upload.errNeedPeExt"));
      return;
    }
    setPeFile(file);
    setErrorMessage(null);
  };

  const handleSelectSymbolScope = (scope: SymbolScope) => {
    setSymbolScope(scope);
    if (scope === "project" && pdbFile?.name.toLowerCase().endsWith(".sym")) {
      setPdbFile(null);
    }
    setErrorMessage(null);
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (isUploading) {
      return;
    }

    if (!isPublicSymbolMode) {
      if (!selectedProjectKey) {
        setErrorMessage(t("upload.errSelectProject"));
        return;
      }
      if (!apiKey.trim()) {
        setErrorMessage(t("upload.errNeedApiKey"));
        return;
      }
      if (!appVersion.trim()) {
        setErrorMessage(t("upload.errNeedAppVersion"));
        return;
      }
    }

    if (activeMode === "crash" && !dumpFile) {
      setErrorMessage(t("upload.errAttachDump"));
      return;
    }
    if (activeMode === "symbol" && !pdbFile) {
      setErrorMessage(
        isPublicSymbolMode
          ? t("upload.errAttachPublicSymbol")
          : t("upload.errAttachPdb"),
      );
      return;
    }
    if (isPublicSymbolMode && isDirectSymSelected && peFile) {
      setErrorMessage(t("upload.errSymWithPeUnsupported"));
      return;
    }

    const normalizedCrashTime =
      activeMode === "crash" ? normalizeCrashTime(crashTime) : undefined;

    const controller = new AbortController();
    abortControllerRef.current = controller;
    setIsUploading(true);
    setProgress(0);
    setErrorMessage(null);

    try {
      if (activeMode === "crash") {
        const trimmedKey = apiKey.trim();
        const res = await uploadCrash({
          projectKey: selectedProjectKey,
          appVersion: appVersion.trim(),
          dumpFile: dumpFile!,
          logFiles: logFiles.length > 0 ? logFiles : undefined,
          buildId: buildId.trim() || undefined,
          osVersion: osVersion.trim() || undefined,
          sessionId: sessionId.trim() || undefined,
          crashTime: normalizedCrashTime,
          apiKey: trimmedKey,
          onProgress: (pct) => setProgress(pct),
          signal: controller.signal,
        });

        if (rememberApiKey) {
          setStoredProjectApiKey(selectedProjectKey, trimmedKey);
        } else {
          removeStoredProjectApiKey(selectedProjectKey);
        }

        onSuccess?.({ mode: "crash", crashId: res.crash_id });
        onClose();
      } else if (isPublicSymbolMode) {
        const res = await uploadPublicSymbol({
          pdbFile: pdbFile!,
          peFile: isDirectSymSelected ? undefined : (peFile ?? undefined),
          autoResymbolicate,
          onProgress: (pct) => setProgress(pct),
          signal: controller.signal,
        });

        onSuccess?.({
          mode: "symbol",
          symbolId: res.id,
          scope: "public",
          resymbolicateCrashIds: res.resymbolicate_crash_ids,
          resymbolicateActiveCrashIds: res.resymbolicate_active_crash_ids,
          resymbolicateSkippedActiveCount:
            res.resymbolicate_skipped_active_count,
        });
        onClose();
      } else {
        const trimmedKey = apiKey.trim();
        const res = await uploadSymbol({
          projectKey: selectedProjectKey,
          appVersion: appVersion.trim(),
          pdbFile: pdbFile!,
          peFile: peFile ?? undefined,
          apiKey: trimmedKey,
          onProgress: (pct) => setProgress(pct),
          signal: controller.signal,
        });

        if (rememberApiKey) {
          setStoredProjectApiKey(selectedProjectKey, trimmedKey);
        } else {
          removeStoredProjectApiKey(selectedProjectKey);
        }

        onSuccess?.({ mode: "symbol", symbolId: res.id, scope: "project" });
        onClose();
      }
    } catch (err) {
      if (controller.signal.aborted) {
        return;
      }
      setErrorMessage(formatUploadError(err));
    } finally {
      if (abortControllerRef.current === controller) {
        abortControllerRef.current = null;
      }
      setIsUploading(false);
    }
  };

  if (!isOpen) {
    return null;
  }

  const selectedProject = projects.find((p) => p.key === selectedProjectKey);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="upload-modal-title"
    >
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-slate-900/60 backdrop-blur-[1px]"
        onClick={handleClose}
        aria-hidden="true"
      />

      {/* Modal Panel */}
      <div className="relative z-10 flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-slate-200 bg-white shadow-xl">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-slate-200 bg-slate-50 px-5 py-3.5">
          <div className="flex items-center gap-2.5">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-indigo-600 text-white">
              <Upload className="h-4 w-4" />
            </div>
            <div>
              <h2
                id="upload-modal-title"
                className="text-sm font-semibold text-slate-900"
              >
                {activeMode === "crash"
                  ? t("upload.crashHeaderTitle")
                  : t("upload.symbolHeaderTitle")}
              </h2>
              <p className="text-xs text-slate-500">
                {activeMode === "crash"
                  ? t("upload.crashHeaderSubtitle")
                  : t("upload.symbolHeaderSubtitle")}
              </p>
            </div>
          </div>

          <button
            type="button"
            onClick={handleClose}
            aria-label={t("upload.closeModalAria")}
            className="rounded-md p-1.5 text-slate-400 hover:bg-slate-200/70 hover:text-slate-700"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Mode Switcher Tabs */}
        <div className="flex border-b border-slate-200 bg-white px-5 pt-2">
          <button
            type="button"
            disabled={isUploading}
            onClick={() => {
              setActiveMode("crash");
              setErrorMessage(null);
            }}
            className={`mr-4 border-b-2 pb-2.5 text-xs font-semibold transition-colors disabled:opacity-50 ${
              activeMode === "crash"
                ? "border-indigo-600 text-indigo-600"
                : "border-transparent text-slate-500 hover:text-slate-800"
            }`}
          >
            {t("upload.tabCrash")}
          </button>
          <button
            type="button"
            disabled={isUploading}
            onClick={() => {
              setActiveMode("symbol");
              setErrorMessage(null);
            }}
            className={`border-b-2 pb-2.5 text-xs font-semibold transition-colors disabled:opacity-50 ${
              activeMode === "symbol"
                ? "border-indigo-600 text-indigo-600"
                : "border-transparent text-slate-500 hover:text-slate-800"
            }`}
          >
            {t("upload.tabSymbol")}
          </button>
        </div>

        {/* Form Body */}
        <form
          onSubmit={(e) => {
            void handleSubmit(e);
          }}
          className="flex min-h-0 flex-1 flex-col"
        >
          <div className="ck-scrollbar flex-1 space-y-4 overflow-y-auto px-5 py-4">
            {errorMessage && (
              <div
                role="alert"
                className="flex items-start gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3.5 py-2.5 text-xs text-rose-800"
              >
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-rose-600" />
                <span className="break-words">{errorMessage}</span>
              </div>
            )}

            {activeMode === "symbol" && (
              <SymbolScopeSelector
                symbolScope={symbolScope}
                isPublicSymbolMode={isPublicSymbolMode}
                symbolTargetHint={symbolTargetHint}
                isUploading={isUploading}
                onSelectScope={handleSelectSymbolScope}
              />
            )}

            {!isPublicSymbolMode && (
              <ProjectAuthFields
                projects={projects}
                selectedProjectKey={selectedProjectKey}
                appVersion={appVersion}
                apiKey={apiKey}
                rememberApiKey={rememberApiKey}
                showApiKey={showApiKey}
                isApiKeyPrefilled={isApiKeyPrefilled}
                isUploading={isUploading}
                onSelectProjectKey={setSelectedProjectKey}
                onAppVersionChange={setAppVersion}
                onApiKeyChange={(val) => {
                  setApiKey(val);
                  setIsApiKeyPrefilled(false);
                }}
                onToggleShowApiKey={() => setShowApiKey((prev) => !prev)}
                onRememberApiKeyChange={setRememberApiKey}
              />
            )}

            {activeMode === "crash" ? (
              <CrashUploadFields
                selectedProject={selectedProject}
                dumpFile={dumpFile}
                logFiles={logFiles}
                sessionId={sessionId}
                buildId={buildId}
                osVersion={osVersion}
                crashTime={crashTime}
                isUploading={isUploading}
                onDumpFilesSelected={handleDumpFilesSelected}
                onClearDumpFile={() => setDumpFile(null)}
                onAppendLogFiles={appendLogFiles}
                onRemoveLogFile={(dedupKey) =>
                  setLogFiles((prev) =>
                    prev.filter((f) => getFileDedupKey(f) !== dedupKey),
                  )
                }
                onSessionIdChange={setSessionId}
                onBuildIdChange={setBuildId}
                onOsVersionChange={setOsVersion}
                onCrashTimeChange={setCrashTime}
              />
            ) : (
              <SymbolUploadFields
                isPublicSymbolMode={isPublicSymbolMode}
                isDirectSymSelected={isDirectSymSelected}
                pdbFile={pdbFile}
                peFile={peFile}
                autoResymbolicate={autoResymbolicate}
                isUploading={isUploading}
                onPdbFileSelected={handlePdbFileSelected}
                onClearPdbFile={() => setPdbFile(null)}
                onPeFileSelected={handlePeFileSelected}
                onClearPeFile={() => setPeFile(null)}
                onAutoResymbolicateChange={setAutoResymbolicate}
              />
            )}

            {/* Upload Progress Bar */}
            {isUploading && (
              <div className="space-y-1.5 rounded-lg border border-slate-200 bg-slate-50 p-3">
                <div className="flex items-center justify-between text-xs">
                  <span className="font-medium text-slate-700">
                    {progress < 100
                      ? t("upload.uploadingProgress", { progress })
                      : activeMode === "symbol"
                        ? t("upload.convertingSymbol")
                        : t("upload.queuingCrash")}
                  </span>
                  <span className="font-mono font-semibold text-indigo-600">
                    {progress}%
                  </span>
                </div>
                <div
                  role="progressbar"
                  aria-valuenow={progress}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  className="h-2 w-full overflow-hidden rounded-full bg-slate-200"
                >
                  <div
                    className="h-full bg-indigo-600 transition-all duration-150"
                    style={{ width: `${progress}%` }}
                  />
                </div>
              </div>
            )}
          </div>

          {/* Footer */}
          <div className="flex items-center justify-end gap-2.5 border-t border-slate-200 bg-slate-50 px-5 py-3">
            <button
              type="button"
              onClick={handleClose}
              className="rounded-md border border-slate-300 bg-white px-3.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-100"
            >
              {isUploading ? t("upload.abortUpload") : t("common.cancel")}
            </button>
            <button
              type="submit"
              disabled={isUploading}
              className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-4 py-1.5 text-xs font-medium text-white shadow-sm hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {isUploading ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  <span>{t("upload.uploadingBtn")}</span>
                </>
              ) : (
                <>
                  <Upload className="h-3.5 w-3.5" />
                  <span>
                    {activeMode === "crash"
                      ? t("upload.uploadCrashBtn")
                      : isPublicSymbolMode
                        ? t("upload.uploadPublicSymbolsBtn")
                        : t("upload.uploadSymbolsBtn")}
                  </span>
                </>
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default UploadModal;
