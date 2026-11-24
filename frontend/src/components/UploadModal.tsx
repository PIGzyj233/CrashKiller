import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
  type FormEvent,
} from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Eye,
  EyeOff,
  FileCode2,
  FileText,
  HardDriveUpload,
  KeyRound,
  Loader2,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import {
  ApiError,
  getStoredProjectApiKey,
  removeStoredProjectApiKey,
  setStoredProjectApiKey,
  uploadCrash,
  uploadSymbol,
} from "../api/client";
import type { ProjectRead } from "../api/types";
import { i18n, useAppI18n } from "../i18n";
import { getSessionIdLabel } from "../utils/sessionLabel";

export interface UploadModalProps {
  isOpen: boolean;
  onClose: () => void;
  mode: "crash" | "symbol";
  projects: ProjectRead[];
  defaultProjectKey?: string;
  onSuccess?: (
    result:
      | { mode: "crash"; crashId: string }
      | { mode: "symbol"; symbolId: string },
  ) => void;
}

function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) {
    return "-";
  }
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function resolveInitialProjectKey(
  projects: ProjectRead[],
  defaultProjectKey?: string,
): string {
  if (defaultProjectKey) {
    if (
      projects.length === 0 ||
      projects.some((p) => p.key === defaultProjectKey)
    ) {
      return defaultProjectKey;
    }
  }
  return projects[0]?.key ?? "";
}

function formatUploadError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401 || err.status === 403) {
      return i18n.t("upload.errorAuth", { message: err.message });
    }
    if (err.status === 413) {
      return i18n.t("upload.errorTooLarge", { message: err.message });
    }
    if (err.status === 507) {
      return i18n.t("upload.errorDiskFull", { message: err.message });
    }
    return err.message;
  }
  if (err instanceof Error) {
    return err.message;
  }
  return i18n.t("upload.errorUnexpected");
}

export function UploadModal({
  isOpen,
  onClose,
  mode,
  projects,
  defaultProjectKey,
  onSuccess,
}: UploadModalProps) {
  const { t } = useAppI18n();
  const [activeMode, setActiveMode] = useState<"crash" | "symbol">(mode);
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

  // Drag states
  const [isDumpDragOver, setIsDumpDragOver] = useState(false);
  const [isLogDragOver, setIsLogDragOver] = useState(false);
  const [isPdbDragOver, setIsPdbDragOver] = useState(false);
  const [isPeDragOver, setIsPeDragOver] = useState(false);

  const abortControllerRef = useRef<AbortController | null>(null);
  const wasOpenRef = useRef(false);
  const dumpInputRef = useRef<HTMLInputElement | null>(null);
  const logInputRef = useRef<HTMLInputElement | null>(null);
  const pdbInputRef = useRef<HTMLInputElement | null>(null);
  const peInputRef = useRef<HTMLInputElement | null>(null);

  // Reset form when modal transitions from closed -> open; abort active XHR when closed
  useEffect(() => {
    if (isOpen && !wasOpenRef.current) {
      const initialKey = resolveInitialProjectKey(projects, defaultProjectKey);
      const storedKey = getStoredProjectApiKey(initialKey);
      setActiveMode(mode);
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
  }, [isOpen, mode, projects, defaultProjectKey]);

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
    const blockedExt = /\.(dmp|pdb|exe|dll)$/i;
    const valid: File[] = [];
    for (const f of incoming) {
      if (blockedExt.test(f.name)) {
        setErrorMessage(t("upload.errBinaryInLogs", { name: f.name }));
        return;
      }
      valid.push(f);
    }
    setErrorMessage(null);
    setLogFiles((prev) => {
      const seen = new Set(
        prev.map((f) => `${f.name}:${f.size}:${f.lastModified}`),
      );
      const merged = [...prev];
      for (const f of valid) {
        const key = `${f.name}:${f.size}:${f.lastModified}`;
        if (!seen.has(key)) {
          seen.add(key);
          merged.push(f);
        }
      }
      return merged;
    });
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

  const handlePdbFileSelected = (files: FileList | null) => {
    if (!files || files.length === 0) {
      return;
    }
    const file = files[0];
    if (!file.name.toLowerCase().endsWith(".pdb")) {
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
    const file = files[0];
    if (!/\.(exe|dll)$/i.test(file.name)) {
      setErrorMessage(t("upload.errNeedPeExt"));
      return;
    }
    setPeFile(file);
    setErrorMessage(null);
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (isUploading) {
      return;
    }

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

    if (activeMode === "crash" && !dumpFile) {
      setErrorMessage(t("upload.errAttachDump"));
      return;
    }
    if (activeMode === "symbol" && !pdbFile) {
      setErrorMessage(t("upload.errAttachPdb"));
      return;
    }

    let normalizedCrashTime: string | undefined = undefined;
    if (activeMode === "crash" && crashTime.trim()) {
      const parsed = new Date(crashTime.trim());
      if (!Number.isNaN(parsed.getTime())) {
        normalizedCrashTime = parsed.toISOString();
      } else {
        normalizedCrashTime = crashTime.trim();
      }
    }

    const controller = new AbortController();
    abortControllerRef.current = controller;
    setIsUploading(true);
    setProgress(0);
    setErrorMessage(null);

    try {
      const trimmedKey = apiKey.trim();
      if (activeMode === "crash") {
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
      } else {
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

        onSuccess?.({ mode: "symbol", symbolId: res.id });
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

            {/* Project + App Version */}
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <label
                  htmlFor="upload-project-select"
                  className="mb-1 block text-xs font-medium text-slate-700"
                >
                  {t("upload.projectLabel")}{" "}
                  <span className="text-rose-500">*</span>
                </label>
                <select
                  id="upload-project-select"
                  value={selectedProjectKey}
                  disabled={isUploading || projects.length === 0}
                  onChange={(e) => setSelectedProjectKey(e.target.value)}
                  className="h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 text-xs text-slate-800 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:bg-slate-100"
                >
                  {projects.length === 0 ? (
                    <option value="">{t("upload.noProjects")}</option>
                  ) : (
                    projects.map((p) => (
                      <option key={p.id} value={p.key}>
                        {p.name} ({p.key})
                      </option>
                    ))
                  )}
                </select>
              </div>

              <div>
                <label
                  htmlFor="upload-app-version"
                  className="mb-1 block text-xs font-medium text-slate-700"
                >
                  {t("upload.appVersionLabel")}{" "}
                  <span className="text-rose-500">*</span>
                </label>
                <input
                  id="upload-app-version"
                  type="text"
                  value={appVersion}
                  disabled={isUploading}
                  onChange={(e) => setAppVersion(e.target.value)}
                  placeholder={t("upload.appVersionPlaceholder")}
                  className="h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 font-mono text-xs text-slate-800 placeholder:font-sans placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:bg-slate-100"
                />
              </div>
            </div>

            {/* Project API Key */}
            <div>
              <div className="mb-1 flex items-center justify-between">
                <label
                  htmlFor="upload-api-key"
                  className="flex items-center gap-1.5 text-xs font-medium text-slate-700"
                >
                  <KeyRound className="h-3.5 w-3.5 text-slate-400" />
                  <span>
                    {t("upload.apiKeyLabel")}{" "}
                    <span className="text-rose-500">*</span>
                  </span>
                </label>
                {isApiKeyPrefilled && (
                  <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700">
                    {t("upload.apiKeyPrefilled")}
                  </span>
                )}
              </div>

              <div className="relative">
                <input
                  id="upload-api-key"
                  type={showApiKey ? "text" : "password"}
                  value={apiKey}
                  disabled={isUploading}
                  onChange={(e) => {
                    setApiKey(e.target.value);
                    setIsApiKeyPrefilled(false);
                  }}
                  placeholder={t("upload.apiKeyPlaceholder")}
                  className="h-9 w-full rounded-md border border-slate-300 bg-white pl-2.5 pr-9 font-mono text-xs text-slate-800 placeholder:font-sans placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:bg-slate-100"
                />
                <button
                  type="button"
                  onClick={() => setShowApiKey((prev) => !prev)}
                  aria-label={
                    showApiKey
                      ? t("upload.hideApiKey")
                      : t("upload.showApiKey")
                  }
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600"
                >
                  {showApiKey ? (
                    <EyeOff className="h-4 w-4" />
                  ) : (
                    <Eye className="h-4 w-4" />
                  )}
                </button>
              </div>

              <label className="mt-1.5 inline-flex cursor-pointer items-center gap-1.5 text-xs text-slate-600">
                <input
                  type="checkbox"
                  checked={rememberApiKey}
                  disabled={isUploading}
                  onChange={(e) => setRememberApiKey(e.target.checked)}
                  className="h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
                />
                <span>{t("upload.rememberApiKey")}</span>
              </label>
            </div>

            {activeMode === "crash" ? (
              <>
                {/* .dmp Dropzone */}
                <div>
                  <label className="mb-1 block text-xs font-medium text-slate-700">
                    {t("upload.dumpFileLabel")}{" "}
                    <span className="text-rose-500">*</span>
                  </label>
                  <input
                    ref={dumpInputRef}
                    type="file"
                    accept=".dmp"
                    className="hidden"
                    onChange={(e: ChangeEvent<HTMLInputElement>) => {
                      handleDumpFilesSelected(e.target.files);
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
                        handleDumpFilesSelected(e.dataTransfer.files);
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
                            setDumpFile(null);
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
                        appendLogFiles(Array.from(e.target.files));
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
                        appendLogFiles(Array.from(e.dataTransfer.files));
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
                        const key = `${file.name}:${file.size}:${file.lastModified}`;
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
                                onClick={() =>
                                  setLogFiles((prev) =>
                                    prev.filter(
                                      (f) =>
                                        `${f.name}:${f.size}:${f.lastModified}` !==
                                        key,
                                    ),
                                  )
                                }
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
                        label: getSessionIdLabel(
                          projects.find((p) => p.key === selectedProjectKey),
                          t("common.sessionId"),
                        ),
                      })}
                    </label>
                    <input
                      id="upload-session-id"
                      type="text"
                      maxLength={128}
                      value={sessionId}
                      disabled={isUploading}
                      onChange={(e) => setSessionId(e.target.value)}
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
                      onChange={(e) => setBuildId(e.target.value)}
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
                      onChange={(e) => setOsVersion(e.target.value)}
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
                      onChange={(e) => setCrashTime(e.target.value)}
                      className="h-8 w-full rounded-md border border-slate-300 bg-white px-2 text-xs text-slate-800 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
                    />
                  </div>
                </div>
              </>
            ) : (
              <>
                {/* .pdb Dropzone */}
                <div>
                  <label className="mb-1 block text-xs font-medium text-slate-700">
                    {t("upload.pdbFileLabel")}{" "}
                    <span className="text-rose-500">*</span>
                  </label>
                  <input
                    ref={pdbInputRef}
                    type="file"
                    accept=".pdb"
                    className="hidden"
                    onChange={(e: ChangeEvent<HTMLInputElement>) => {
                      handlePdbFileSelected(e.target.files);
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
                        handlePdbFileSelected(e.dataTransfer.files);
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
                            setPdbFile(null);
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
                          {t("upload.dropPdbTextBefore")}
                          <span className="font-mono">.pdb</span>
                          {t("upload.dropPdbTextAfter")}
                        </p>
                      </>
                    )}
                  </div>
                </div>

                {/* Optional PE Companion (.exe / .dll) */}
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
                      handlePeFileSelected(e.target.files);
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
                        handlePeFileSelected(e.dataTransfer.files);
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
                            setPeFile(null);
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

                {/* Mandatory PE Unwind Warning or Confirmation */}
                {!peFile ? (
                  <div
                    role="alert"
                    data-testid="pe-unwind-warning"
                    className="flex items-start gap-2.5 rounded-lg border border-amber-200 bg-amber-50 px-3.5 py-2.5 text-xs text-amber-900"
                  >
                    <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
                    <div>
                      <p className="font-semibold">
                        {t("upload.peWarningTitle")}
                      </p>
                      <p className="mt-0.5 text-amber-800">
                        {t("upload.peWarningDesc")}
                      </p>
                    </div>
                  </div>
                ) : (
                  <div className="flex items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3.5 py-2 text-xs text-emerald-800">
                    <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600" />
                    <span>{t("upload.peAttachedOk")}</span>
                  </div>
                )}
              </>
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
