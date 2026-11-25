import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  Cpu,
  FileCode,
  Filter,
  Info,
  Layers,
  Loader2,
  Search,
  ShieldAlert,
} from "lucide-react";
import type {
  CrashStatus,
  LoadedModule,
  MissingSymbolInfo,
  ParsedStacks,
  StackFrame,
  StackThread,
} from "../api/types";
import { useAppI18n } from "../i18n";

export interface StackTraceViewerProps {
  parsedStacks: ParsedStacks | null | undefined;
  missingSymbols?: MissingSymbolInfo[] | null;
  crashReason?: string | null;
  crashAddress?: string | null;
  status?: CrashStatus;
}

type CompatThread = StackThread & { thread_id?: number | null };
type CompatFrame = StackFrame & { module_offset?: string | number | null };
type CompatModule = Omit<LoadedModule, "filename"> & {
  filename?: string | null;
  module_name?: string | null;
};
type ModuleFilter = "all" | "loaded" | "missing" | "unreferenced" | "corrupt";

function getThreadId(thread: CompatThread): number | undefined {
  return thread.id ?? thread.thread_id ?? undefined;
}

function resolveCrashingThreadIndex(parsedStacks: ParsedStacks): number {
  const flagged = parsedStacks.threads.find((t) => t.is_crashing === true);
  if (flagged) {
    return flagged.thread_index;
  }
  if (
    typeof parsedStacks.crashing_thread_index === "number" &&
    parsedStacks.threads.some(
      (t) => t.thread_index === parsedStacks.crashing_thread_index,
    )
  ) {
    return parsedStacks.crashing_thread_index;
  }
  return parsedStacks.threads[0]?.thread_index ?? 0;
}

function dedupeMissingSymbols(
  propMissing?: MissingSymbolInfo[] | null,
  stacksMissing?: MissingSymbolInfo[] | null,
): MissingSymbolInfo[] {
  const merged = [...(propMissing ?? []), ...(stacksMissing ?? [])];
  const seen = new Set<string>();
  const result: MissingSymbolInfo[] = [];
  for (const item of merged) {
    const key = `${item.debug_file}::${item.debug_id}`;
    if (!seen.has(key)) {
      seen.add(key);
      result.push(item);
    }
  }
  return result;
}

function extractThreadRegisters(
  thread: CompatThread | undefined,
): Record<string, string> | null {
  if (!thread) {
    return null;
  }
  if (thread.registers && Object.keys(thread.registers).length > 0) {
    return thread.registers;
  }
  for (const frame of thread.frames) {
    if (frame.registers && Object.keys(frame.registers).length > 0) {
      return frame.registers;
    }
  }
  return null;
}

function getModuleDisplayName(mod: CompatModule, fallbackLabel: string): string {
  if (mod.module_name && mod.module_name.trim()) {
    return mod.module_name;
  }
  const filename = mod.filename ?? "";
  if (!filename) {
    return mod.debug_file || mod.code_file || fallbackLabel;
  }
  const parts = filename.split(/[\\/]/);
  return parts[parts.length - 1] || filename;
}

function formatOffset(offset: string | number | null | undefined): string {
  if (offset === null || offset === undefined || offset === "") {
    return "";
  }
  if (typeof offset === "number") {
    return `+0x${offset.toString(16)}`;
  }
  const trimmed = offset.trim();
  return trimmed.startsWith("+") ? trimmed : `+${trimmed}`;
}

function getTrustBadgeMeta(trust: string | null | undefined): {
  label: string;
  className: string;
  descKind: "context" | "cfi" | "fp" | "scan" | "other";
  rawTrust: string;
} | null {
  if (!trust) {
    return null;
  }
  const normalized = trust.toLowerCase();
  if (normalized === "cfi" || normalized === "context") {
    return {
      label: normalized,
      className: "border-emerald-200 bg-emerald-50 text-emerald-700",
      descKind: normalized === "context" ? "context" : "cfi",
      rawTrust: trust,
    };
  }
  if (normalized === "fp" || normalized === "frame_pointer") {
    return {
      label: normalized === "frame_pointer" ? "fp" : normalized,
      className: "border-amber-200 bg-amber-50 text-amber-800",
      descKind: "fp",
      rawTrust: trust,
    };
  }
  if (normalized === "scan") {
    return {
      label: "scan",
      className: "border-rose-200 bg-rose-50 text-rose-700",
      descKind: "scan",
      rawTrust: trust,
    };
  }
  return {
    label: trust,
    className: "border-slate-200 bg-slate-100 text-slate-600",
    descKind: "other",
    rawTrust: trust,
  };
}

function getModuleStatus(mod: CompatModule): Exclude<ModuleFilter, "all"> {
  if (mod.corrupt_symbols) {
    return "corrupt";
  }
  if (mod.loaded_symbols === true) {
    return "loaded";
  }
  if (mod.missing_symbols === true || mod.missing_symbols === undefined) {
    return "missing";
  }
  return "unreferenced";
}

export function StackTraceViewer({
  parsedStacks,
  missingSymbols,
  crashReason,
  crashAddress,
  status,
}: StackTraceViewerProps) {
  const { t, formatCrashStatus } = useAppI18n();
  const [activeTab, setActiveTab] = useState<"stack" | "modules">("stack");
  const [selectedThreadIndex, setSelectedThreadIndex] = useState<number>(() =>
    parsedStacks ? resolveCrashingThreadIndex(parsedStacks) : 0,
  );
  const [hideNoiseFrames, setHideNoiseFrames] = useState(false);
  const [registersExpanded, setRegistersExpanded] = useState(true);
  const [registerFilter, setRegisterFilter] = useState("");
  const [moduleQuery, setModuleQuery] = useState("");
  const [moduleFilter, setModuleFilter] = useState<ModuleFilter>("all");
  const [missingBannerExpanded, setMissingBannerExpanded] = useState(false);
  const [copiedStack, setCopiedStack] = useState(false);

  const consolidatedMissing = useMemo(
    () => dedupeMissingSymbols(missingSymbols, parsedStacks?.missing_symbols),
    [missingSymbols, parsedStacks?.missing_symbols],
  );

  const crashingThreadIdx = useMemo(
    () => (parsedStacks ? resolveCrashingThreadIndex(parsedStacks) : 0),
    [parsedStacks],
  );
  const prevCrashingThreadIdxRef = useRef<number | null>(
    parsedStacks && parsedStacks.threads.length > 0 ? crashingThreadIdx : null,
  );

  useEffect(() => {
    if (!parsedStacks || parsedStacks.threads.length === 0) {
      prevCrashingThreadIdxRef.current = null;
      return;
    }
    const crashingChanged =
      prevCrashingThreadIdxRef.current !== crashingThreadIdx;
    prevCrashingThreadIdxRef.current = crashingThreadIdx;

    setSelectedThreadIndex((prev) => {
      if (crashingChanged) {
        return crashingThreadIdx;
      }
      const exists = parsedStacks.threads.some(
        (t) => t.thread_index === prev,
      );
      return exists ? prev : crashingThreadIdx;
    });
  }, [parsedStacks, crashingThreadIdx]);

  const threads = useMemo(
    () => (parsedStacks?.threads ?? []) as CompatThread[],
    [parsedStacks],
  );

  const crashingThread = useMemo(
    () =>
      threads.find((th) => th.thread_index === crashingThreadIdx) ?? threads[0],
    [threads, crashingThreadIdx],
  );

  const crashingMissingInfo = useMemo(() => {
    const frames = (crashingThread?.frames ?? []) as CompatFrame[];
    const affectedModules = new Set<string>();
    let affectedFrameCount = 0;

    for (const frame of frames) {
      const frameMissing =
        frame.missing_symbols === true ||
        (!frame.function && Boolean(frame.module));
      if (frameMissing) {
        affectedFrameCount += 1;
        if (frame.module) {
          const base = frame.module.split(/[/\\]/).pop()?.toLowerCase();
          if (base) {
            affectedModules.add(base);
          }
        }
      }
    }

    const isItemOnCrashingStack = (item: MissingSymbolInfo): boolean => {
      if (affectedModules.size === 0) {
        return false;
      }
      const candidates = [item.module_name, item.code_file, item.debug_file]
        .filter((v): v is string => Boolean(v))
        .map((v) => v.split(/[/\\]/).pop()?.toLowerCase() ?? "");
      return candidates.some((c) => affectedModules.has(c));
    };

    const sortedMissing =
      affectedModules.size > 0
        ? [...consolidatedMissing].sort((a, b) => {
            const aHit = isItemOnCrashingStack(a) ? 0 : 1;
            const bHit = isItemOnCrashingStack(b) ? 0 : 1;
            return aHit - bHit;
          })
        : consolidatedMissing;

    return {
      affectedFrameCount,
      isItemOnCrashingStack,
      sortedMissing,
    };
  }, [crashingThread, consolidatedMissing]);

  const activeThread = useMemo(
    () =>
      threads.find((t) => t.thread_index === selectedThreadIndex) ??
      threads[0],
    [threads, selectedThreadIndex],
  );

  const activeFrames = useMemo(
    () => (activeThread?.frames ?? []) as CompatFrame[],
    [activeThread],
  );

  const noiseCount = useMemo(
    () => activeFrames.filter((f) => f.is_noise === true).length,
    [activeFrames],
  );

  const visibleFrames = useMemo(
    () =>
      hideNoiseFrames
        ? activeFrames.filter((f) => f.is_noise !== true)
        : activeFrames,
    [activeFrames, hideNoiseFrames],
  );

  const threadRegisters = useMemo(
    () => extractThreadRegisters(activeThread),
    [activeThread],
  );

  const registerEntries = useMemo(() => {
    if (!threadRegisters) {
      return [];
    }
    const entries = Object.entries(threadRegisters);
    const q = registerFilter.trim().toLowerCase();
    if (!q) {
      return entries;
    }
    return entries.filter(
      ([name, value]) =>
        name.toLowerCase().includes(q) ||
        String(value ?? "").toLowerCase().includes(q),
    );
  }, [threadRegisters, registerFilter]);

  const modules = useMemo(
    () => (parsedStacks?.modules ?? []) as CompatModule[],
    [parsedStacks],
  );

  const moduleCounts = useMemo(() => {
    let loaded = 0;
    let missing = 0;
    let unreferenced = 0;
    let corrupt = 0;
    for (const mod of modules) {
      const st = getModuleStatus(mod);
      if (st === "loaded") loaded += 1;
      else if (st === "missing") missing += 1;
      else if (st === "unreferenced") unreferenced += 1;
      else corrupt += 1;
    }
    return { loaded, missing, unreferenced, corrupt };
  }, [modules]);

  const filteredModules = useMemo(() => {
    const q = moduleQuery.trim().toLowerCase();
    return modules.filter((mod) => {
      if (moduleFilter !== "all" && getModuleStatus(mod) !== moduleFilter) {
        return false;
      }
      if (!q) {
        return true;
      }
      return (
        (mod.filename ?? "").toLowerCase().includes(q) ||
        (mod.module_name ?? "").toLowerCase().includes(q) ||
        (mod.debug_file ?? "").toLowerCase().includes(q) ||
        (mod.debug_id ?? "").toLowerCase().includes(q) ||
        (mod.code_file ?? "").toLowerCase().includes(q)
      );
    });
  }, [modules, moduleFilter, moduleQuery]);

  const effectiveReason =
    crashReason ?? parsedStacks?.crash_reason ?? t("stack.unknownException");
  const effectiveAddress = crashAddress ?? parsedStacks?.crash_address ?? null;

  const handleCopyStack = async () => {
    if (!activeThread) {
      return;
    }
    const lines: string[] = [];
    const tid = getThreadId(activeThread);
    lines.push(
      `Thread ${activeThread.thread_index}${
        activeThread.thread_name ? ` (${activeThread.thread_name})` : ""
      }${tid !== undefined ? ` [tid:${tid}]` : ""}${
        activeThread.thread_index === crashingThreadIdx ||
        activeThread.is_crashing
          ? " [CRASHED]"
          : ""
      }`,
    );
    visibleFrames.forEach((frame, idx) => {
      const frameNo = frame.frame_index ?? frame.frame ?? idx;
      const mod = frame.module || "<unknown>";
      const fn = frame.function || frame.ip || "(unresolved)";
      const off = formatOffset(frame.function_offset);
      const loc = frame.file
        ? ` (${frame.file}${frame.line != null ? `:${frame.line}` : ""})`
        : "";
      const trust = frame.trust ? ` [${frame.trust}]` : "";
      lines.push(`#${frameNo} ${mod}!${fn}${off}${loc}${trust}`);
      if (Array.isArray(frame.inlines)) {
        frame.inlines.forEach((inl) => {
          const inlFn = inl.function || "(inlined)";
          const inlOff = formatOffset(inl.function_offset);
          const inlLoc = inl.file
            ? ` (${inl.file}${inl.line != null ? `:${inl.line}` : ""})`
            : "";
          lines.push(`    [inlined] ${inlFn}${inlOff}${inlLoc}`);
        });
      }
    });

    try {
      await navigator.clipboard.writeText(lines.join("\n"));
      setCopiedStack(true);
      window.setTimeout(() => setCopiedStack(false), 1800);
    } catch {
      // Clipboard may be unavailable in insecure contexts
    }
  };

  const renderMissingSymbolsBanner = () => {
    if (consolidatedMissing.length === 0) {
      return null;
    }

    const hasParsedThreads = threads.length > 0;
    const affectedFrames = crashingMissingInfo.affectedFrameCount;
    const affectsCrashingStack = !hasParsedThreads || affectedFrames > 0;

    const bannerTitle = !hasParsedThreads
      ? consolidatedMissing.length === 1
        ? t("stack.missingBannerTitleOne")
        : t("stack.missingBannerTitleOther", {
            count: consolidatedMissing.length,
          })
      : affectedFrames > 0
        ? consolidatedMissing.length === 1
          ? t("stack.missingBannerAffectedOne", { affected: affectedFrames })
          : t("stack.missingBannerAffectedOther", {
              count: consolidatedMissing.length,
              affected: affectedFrames,
            })
        : consolidatedMissing.length === 1
          ? t("stack.missingBannerUnaffectedOne")
          : t("stack.missingBannerUnaffectedOther", {
              count: consolidatedMissing.length,
            });

    return (
      <div
        className={
          affectsCrashingStack
            ? "border-b border-amber-200 bg-amber-50 px-4 py-2.5 text-amber-950"
            : "border-b border-slate-200 bg-slate-50/75 px-4 py-2 text-slate-600"
        }
        role={affectsCrashingStack ? "alert" : undefined}
      >
        <div className="flex flex-wrap items-center justify-between gap-2">
          <button
            type="button"
            onClick={() => setMissingBannerExpanded((prev) => !prev)}
            aria-expanded={missingBannerExpanded}
            className={`flex items-center gap-2 text-left text-xs ${
              affectsCrashingStack
                ? "font-semibold text-amber-900 hover:text-amber-950"
                : "font-medium text-slate-600 hover:text-slate-900"
            }`}
          >
            {missingBannerExpanded ? (
              <ChevronDown className="h-3.5 w-3.5 shrink-0" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5 shrink-0" />
            )}
            {affectsCrashingStack ? (
              <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-amber-600" />
            ) : (
              <Info className="h-3.5 w-3.5 shrink-0 text-slate-400" />
            )}
            <span>{bannerTitle}</span>
            <span
              className={`ml-1 rounded px-1.5 py-0.5 text-[11px] font-normal ${
                affectsCrashingStack
                  ? "bg-amber-100/80 text-amber-800"
                  : "bg-slate-200/70 text-slate-600"
              }`}
            >
              {missingBannerExpanded
                ? t("stack.missingBannerCollapse")
                : t("stack.missingBannerExpand")}
            </span>
          </button>
          <Link
            to="/symbols"
            className={`inline-flex items-center gap-1 rounded-md border px-2.5 py-1 text-xs font-medium shadow-sm ${
              affectsCrashingStack
                ? "border-amber-300 bg-white text-amber-900 hover:bg-amber-100"
                : "border-slate-200 bg-white text-slate-600 hover:bg-slate-100 hover:text-slate-900"
            }`}
          >
            <FileCode className="h-3.5 w-3.5" />
            {t("stack.uploadSymbols")}
          </Link>
        </div>

        {missingBannerExpanded && (
          <div className="ck-scrollbar mt-2.5 grid max-h-72 gap-1.5 overflow-y-auto pr-1 sm:grid-cols-2">
            {crashingMissingInfo.sortedMissing.map((item) => {
              const onCrashingStack =
                crashingMissingInfo.isItemOnCrashingStack(item);
              return (
                <div
                  key={`${item.debug_file}::${item.debug_id}`}
                  className={`flex flex-col rounded-md border px-2.5 py-1.5 text-xs ${
                    onCrashingStack
                      ? "border-amber-300 bg-amber-50/70"
                      : affectsCrashingStack
                        ? "border-amber-200/80 bg-white/80"
                        : "border-slate-200 bg-white"
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex min-w-0 items-center gap-1.5">
                      <span className="truncate font-semibold text-slate-900">
                        {item.module_name || item.debug_file}
                      </span>
                      {onCrashingStack && (
                        <span className="shrink-0 rounded border border-amber-300 bg-amber-100 px-1 py-0.5 text-[10px] font-semibold text-amber-900">
                          {t("stack.missingBannerCrashingTag")}
                        </span>
                      )}
                    </div>
                    <span
                      className={`shrink-0 font-mono text-[11px] ${
                        affectsCrashingStack
                          ? "text-amber-800"
                          : "text-slate-500"
                      }`}
                    >
                      {item.debug_file}
                    </span>
                  </div>
                  <div className="mt-0.5 truncate font-mono text-[11px] text-slate-600">
                    {t("stack.debugIdLabel", { id: item.debug_id })}
                  </div>
                  {(item.code_file || item.code_id) && (
                    <div className="mt-0.5 truncate font-mono text-[11px] text-slate-500">
                      {t("stack.codeLabel", { file: item.code_file ?? "-" })}
                      {item.code_id ? ` (${item.code_id})` : ""}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    );
  };

  if (!parsedStacks || threads.length === 0) {
    return (
      <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
        {renderMissingSymbolsBanner()}
        {status === "PENDING" || status === "PARSING" ? (
          <div className="flex flex-col items-center justify-center gap-2.5 px-6 py-12 text-center">
            <Loader2 className="h-6 w-6 animate-spin text-indigo-600" />
            <p className="text-sm font-medium text-slate-800">
              {t("stack.parsingTitle")}
            </p>
            <p className="text-xs text-slate-500">
              {t("stack.statusLabel")}
              <span className="font-mono">{formatCrashStatus(status)}</span>
            </p>
          </div>
        ) : status === "FAILED" ? (
          <div className="m-4 flex items-start gap-3 rounded-lg border border-rose-200 bg-rose-50 p-4 text-rose-900">
            <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0 text-rose-600" />
            <div>
              <h3 className="text-sm font-semibold">
                {t("stack.parseFailedTitle")}
              </h3>
              <p className="mt-1 text-xs text-rose-700">
                {t("stack.parseFailedDescBefore")}
                <span className="font-mono">minidump-stackwalk</span>
                {t("stack.parseFailedDescAfter")}
              </p>
            </div>
          </div>
        ) : (
          <div className="m-4 rounded-lg border border-dashed border-slate-300 bg-slate-50/70 px-6 py-10 text-center">
            <Layers className="mx-auto h-6 w-6 text-slate-400" />
            <p className="mt-2 text-sm font-medium text-slate-700">
              {t("stack.noStackTitle")}
            </p>
            <p className="mt-1 text-xs text-slate-500">
              {crashReason
                ? t("stack.noStackException", { reason: crashReason })
                : t("stack.noStackHint")}
            </p>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
      {/* Crash Summary Header Bar */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-slate-50 px-4 py-3">
        <div className="flex flex-wrap items-center gap-2.5">
          <span className="inline-flex items-center gap-1.5 rounded-md border border-rose-200 bg-rose-50 px-2.5 py-1 font-mono text-xs font-semibold text-rose-700">
            <ShieldAlert className="h-3.5 w-3.5" />
            {effectiveReason}
          </span>
          {effectiveAddress && (
            <span className="rounded-md border border-slate-200 bg-white px-2 py-1 font-mono text-xs text-slate-700">
              @ {effectiveAddress}
            </span>
          )}
          {parsedStacks.cpu_arch && (
            <span className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2 py-1 font-mono text-xs text-slate-600">
              <Cpu className="h-3.5 w-3.5 text-slate-400" />
              {parsedStacks.cpu_arch}
            </span>
          )}
          {parsedStacks.os_version && (
            <span className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-600">
              {parsedStacks.os_version}
            </span>
          )}
        </div>
        <div className="text-xs text-slate-500">
          {t("stack.summaryCounts", {
            threads: threads.length,
            modules: modules.length,
          })}
        </div>
      </div>

      {/* Missing Symbols Alert Banner */}
      {renderMissingSymbolsBanner()}

      {/* Tab Bar */}
      <div className="flex items-center justify-between border-b border-slate-200 bg-white px-4">
        <div className="flex items-center gap-1" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "stack"}
            onClick={() => setActiveTab("stack")}
            className={`inline-flex items-center gap-2 border-b-2 px-3 py-2.5 text-sm font-medium transition-colors ${
              activeTab === "stack"
                ? "border-indigo-600 text-indigo-600"
                : "border-transparent text-slate-600 hover:text-slate-900"
            }`}
          >
            <Layers className="h-4 w-4" />
            <span>{t("stack.tabStackRegisters")}</span>
            <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-semibold text-slate-700">
              {threads.length}
            </span>
          </button>

          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "modules"}
            onClick={() => setActiveTab("modules")}
            className={`inline-flex items-center gap-2 border-b-2 px-3 py-2.5 text-sm font-medium transition-colors ${
              activeTab === "modules"
                ? "border-indigo-600 text-indigo-600"
                : "border-transparent text-slate-600 hover:text-slate-900"
            }`}
          >
            <FileCode className="h-4 w-4" />
            <span>{t("stack.tabLoadedModules")}</span>
            <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-semibold text-slate-700">
              {modules.length}
            </span>
            {moduleCounts.missing > 0 && (
              <span className="rounded-full border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[11px] font-medium text-amber-800">
                {t("stack.missingShort", { count: moduleCounts.missing })}
              </span>
            )}
          </button>
        </div>
      </div>

      {activeTab === "stack" ? (
        <div>
          {/* Thread Switcher & Frame Toolbar */}
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-slate-50/70 px-4 py-2.5">
            <div className="flex flex-wrap items-center gap-2">
              <label
                htmlFor="stack-thread-select"
                className="text-xs font-medium text-slate-600"
              >
                {t("stack.threadLabel")}
              </label>
              <select
                id="stack-thread-select"
                aria-label={t("stack.selectThreadAria")}
                value={activeThread?.thread_index ?? 0}
                onChange={(e) => setSelectedThreadIndex(Number(e.target.value))}
                className="h-8 max-w-md rounded-md border border-slate-300 bg-white px-2.5 text-xs font-medium text-slate-800 shadow-sm focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
              >
                {threads.map((thread) => {
                  const isCrashed =
                    thread.is_crashing === true ||
                    thread.thread_index === crashingThreadIdx;
                  const tid = getThreadId(thread);
                  return (
                    <option key={thread.thread_index} value={thread.thread_index}>
                      {isCrashed ? t("stack.crashedPrefix") : ""}#
                      {thread.thread_index}{" "}
                      {thread.thread_name || t("stack.defaultThreadName")}
                      {tid !== undefined ? ` (tid:${tid})` : ""} ·{" "}
                      {t("stack.framesCount", { count: thread.frames.length })}
                    </option>
                  );
                })}
              </select>

              {activeThread &&
                activeThread.thread_index !== crashingThreadIdx && (
                  <button
                    type="button"
                    onClick={() => setSelectedThreadIndex(crashingThreadIdx)}
                    className="inline-flex items-center gap-1 rounded-md border border-rose-200 bg-rose-50 px-2 py-1 text-xs font-medium text-rose-700 hover:bg-rose-100"
                  >
                    {t("stack.jumpToCrashed", { index: crashingThreadIdx })}
                  </button>
                )}
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={() => setHideNoiseFrames((prev) => !prev)}
                disabled={noiseCount === 0}
                className={`inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                  hideNoiseFrames
                    ? "border-indigo-300 bg-indigo-50 text-indigo-700"
                    : "border-slate-300 bg-white text-slate-700 hover:bg-slate-50"
                }`}
              >
                <Filter className="h-3.5 w-3.5" />
                <span>
                  {hideNoiseFrames
                    ? t("stack.showNoiseFrames", { count: noiseCount })
                    : t("stack.hideNoiseFrames", { count: noiseCount })}
                </span>
              </button>

              <button
                type="button"
                onClick={() => {
                  void handleCopyStack();
                }}
                className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
              >
                {copiedStack ? (
                  <>
                    <Check className="h-3.5 w-3.5 text-emerald-600" />
                    <span className="text-emerald-700">{t("stack.copied")}</span>
                  </>
                ) : (
                  <>
                    <Copy className="h-3.5 w-3.5" />
                    <span>{t("stack.copyStack")}</span>
                  </>
                )}
              </button>
            </div>
          </div>

          {/* Quick Thread Pills (up to 12 threads for rapid switching) */}
          {threads.length > 1 && (
            <div className="ck-scrollbar flex items-center gap-1.5 overflow-x-auto border-b border-slate-200 bg-white px-4 py-2">
              {threads.map((thread) => {
                const isCrashed =
                  thread.is_crashing === true ||
                  thread.thread_index === crashingThreadIdx;
                const isSelected =
                  activeThread?.thread_index === thread.thread_index;
                return (
                  <button
                    key={thread.thread_index}
                    type="button"
                    onClick={() => setSelectedThreadIndex(thread.thread_index)}
                    className={`inline-flex shrink-0 items-center gap-1.5 rounded-md border px-2 py-1 text-xs font-medium transition-colors ${
                      isSelected
                        ? isCrashed
                          ? "border-rose-300 bg-rose-50 text-rose-800 ring-1 ring-rose-300"
                          : "border-indigo-300 bg-indigo-50 text-indigo-800 ring-1 ring-indigo-300"
                        : isCrashed
                          ? "border-rose-200 bg-rose-50/50 text-rose-700 hover:bg-rose-50"
                          : "border-slate-200 bg-slate-50 text-slate-600 hover:bg-slate-100"
                    }`}
                  >
                    <span className="font-mono">#{thread.thread_index}</span>
                    {isCrashed && (
                      <span className="rounded bg-rose-600 px-1 py-0.5 text-[10px] font-bold uppercase text-white">
                        {t("stack.crashedBadge")}
                      </span>
                    )}
                    {thread.thread_name && (
                      <span className="max-w-[140px] truncate">
                        {thread.thread_name}
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          )}

          {/* Call Stack Frames */}
          <div className="ck-scrollbar max-h-[540px] divide-y divide-slate-100 overflow-y-auto">
            {visibleFrames.length === 0 ? (
              <div className="px-6 py-10 text-center">
                <p className="text-sm text-slate-600">
                  {activeFrames.length > 0 && hideNoiseFrames
                    ? t("stack.allFramesNoise", { count: activeFrames.length })
                    : t("stack.noFramesRecorded")}
                </p>
                {activeFrames.length > 0 && hideNoiseFrames && (
                  <button
                    type="button"
                    onClick={() => setHideNoiseFrames(false)}
                    className="mt-2 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
                  >
                    {t("stack.showAllFrames")}
                  </button>
                )}
              </div>
            ) : (
              visibleFrames.map((frame, idx) => {
                const isNoise = frame.is_noise === true;
                const frameNo = frame.frame_index ?? frame.frame ?? idx;
                const trustMeta = getTrustBadgeMeta(frame.trust);
                const offsetText = formatOffset(frame.function_offset);
                const moduleOffsetText = formatOffset(frame.module_offset);
                const hasInlines =
                  Array.isArray(frame.inlines) && frame.inlines.length > 0;

                return (
                  <div key={`${activeThread?.thread_index ?? 0}-${frameNo}-${idx}`}>
                    <div
                      className={`flex items-start gap-3 border-l-2 px-4 py-2.5 transition-colors hover:bg-slate-50/90 ${
                        isNoise
                          ? "border-l-slate-200 bg-slate-50/60 opacity-70"
                          : "border-l-indigo-500 bg-white"
                      }`}
                    >
                      <span className="w-9 shrink-0 pt-0.5 font-mono text-xs text-slate-400">
                        #{frameNo}
                      </span>

                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                          {frame.module && (
                            <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs font-medium text-slate-700">
                              {frame.module}
                            </span>
                          )}

                          <span
                            className={`break-all font-mono text-xs ${
                              isNoise
                                ? "font-normal text-slate-500"
                                : "font-semibold text-slate-900"
                            }`}
                          >
                            {frame.function || t("stack.unresolvedSymbol")}
                            {offsetText && (
                              <span className="font-normal text-slate-500">
                                {" "}
                                {offsetText}
                              </span>
                            )}
                          </span>

                          {frame.is_inlined === true && (
                            <span className="rounded border border-indigo-200 bg-indigo-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-indigo-700">
                              {t("stack.inlinedBadge")}
                            </span>
                          )}

                          {isNoise && (
                            <span className="rounded border border-slate-200 bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-slate-500">
                              {t("stack.noiseBadge")}
                            </span>
                          )}

                          {frame.missing_symbols === true && (
                            <span className="rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-700">
                              {t("stack.missingSymBadge")}
                            </span>
                          )}

                          {trustMeta && (
                            <span
                              title={
                                trustMeta.descKind === "context"
                                  ? t("stack.trustContext")
                                  : trustMeta.descKind === "cfi"
                                    ? t("stack.trustCfi")
                                    : trustMeta.descKind === "fp"
                                      ? t("stack.trustFp")
                                      : trustMeta.descKind === "scan"
                                        ? t("stack.trustScan")
                                        : t("stack.trustOther", {
                                            trust: trustMeta.rawTrust,
                                          })
                              }
                              className={`rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase ${trustMeta.className}`}
                            >
                              {trustMeta.label}
                            </span>
                          )}
                        </div>

                        {(frame.file || frame.ip || frame.module_offset) && (
                          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 font-mono text-[11px] text-slate-500">
                            {frame.file && (
                              <span className="break-all text-slate-600">
                                {frame.file}
                                {frame.line !== null && frame.line !== undefined
                                  ? `:${frame.line}`
                                  : ""}
                              </span>
                            )}
                            {frame.ip && (
                              <span className="text-slate-400">
                                ip:{frame.ip}
                              </span>
                            )}
                            {!frame.function && moduleOffsetText && (
                              <span className="text-slate-400">
                                mod{moduleOffsetText}
                              </span>
                            )}
                          </div>
                        )}
                      </div>
                    </div>

                    {/* Nested Inline Frames */}
                    {hasInlines && (
                      <div className="divide-y divide-indigo-100/70 border-l-2 border-l-indigo-200 bg-indigo-50/30">
                        {frame.inlines!.map((inl, inlIdx) => (
                          <div
                            key={`${frameNo}-inl-${inlIdx}`}
                            className="flex items-start gap-2.5 py-1.5 pl-14 pr-4"
                          >
                            <span className="mt-0.5 shrink-0 rounded border border-indigo-200 bg-indigo-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-indigo-700">
                              {t("stack.inlinedBadge")}
                            </span>
                            <div className="min-w-0 flex-1">
                              <div className="break-all font-mono text-xs text-slate-800">
                                {inl.function || t("stack.inlinedFrame")}
                                {formatOffset(inl.function_offset) && (
                                  <span className="text-slate-500">
                                    {" "}
                                    {formatOffset(inl.function_offset)}
                                  </span>
                                )}
                              </div>
                              {inl.file && (
                                <div className="mt-0.5 break-all font-mono text-[11px] text-slate-500">
                                  {inl.file}
                                  {inl.line !== null && inl.line !== undefined
                                    ? `:${inl.line}`
                                    : ""}
                                </div>
                              )}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>

          {/* Registers Panel */}
          {threadRegisters && Object.keys(threadRegisters).length > 0 && (
            <div className="border-t border-slate-200 bg-slate-50/50">
              <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5">
                <button
                  type="button"
                  onClick={() => setRegistersExpanded((prev) => !prev)}
                  className="inline-flex items-center gap-1.5 text-xs font-semibold text-slate-700 hover:text-slate-900"
                >
                  {registersExpanded ? (
                    <ChevronDown className="h-4 w-4" />
                  ) : (
                    <ChevronRight className="h-4 w-4" />
                  )}
                  <Cpu className="h-3.5 w-3.5 text-slate-500" />
                  <span>
                    {t("stack.cpuRegisters", {
                      count: Object.keys(threadRegisters).length,
                    })}
                  </span>
                </button>

                {registersExpanded && (
                  <div className="relative">
                    <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-400" />
                    <input
                      type="text"
                      value={registerFilter}
                      onChange={(e) => setRegisterFilter(e.target.value)}
                      placeholder={t("stack.filterRegistersPlaceholder")}
                      aria-label={t("stack.filterRegistersAria")}
                      className="h-7 w-44 rounded-md border border-slate-300 bg-white pl-7 pr-2 text-xs text-slate-800 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
                    />
                  </div>
                )}
              </div>

              {registersExpanded && (
                <div className="ck-scrollbar grid max-h-64 grid-cols-2 gap-2 overflow-y-auto px-4 pb-3 sm:grid-cols-3 lg:grid-cols-4">
                  {registerEntries.length === 0 ? (
                    <p className="col-span-full py-2 text-xs text-slate-500">
                      {t("stack.noRegistersMatch", { query: registerFilter })}
                    </p>
                  ) : (
                    registerEntries.map(([regName, regValue]) => (
                      <div
                        key={regName}
                        className="flex items-center justify-between gap-2 rounded-md border border-slate-200 bg-white px-2.5 py-1.5"
                      >
                        <span className="font-mono text-[11px] font-semibold uppercase text-slate-500">
                          {regName}
                        </span>
                        <span className="select-all truncate font-mono text-xs text-slate-900">
                          {String(regValue ?? "")}
                        </span>
                      </div>
                    ))
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      ) : (
        /* Loaded Modules Tab */
        <div>
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-slate-50/70 px-4 py-2.5">
            <div className="relative min-w-[220px] flex-1 sm:max-w-xs">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-400" />
              <input
                type="text"
                value={moduleQuery}
                onChange={(e) => setModuleQuery(e.target.value)}
                placeholder={t("stack.searchModulesPlaceholder")}
                aria-label={t("stack.searchModulesAria")}
                className="h-8 w-full rounded-md border border-slate-300 bg-white pl-8 pr-3 text-xs text-slate-800 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
              />
            </div>

            <div className="flex flex-wrap items-center gap-1.5">
              {(
                [
                  { id: "all", label: t("stack.filterAll", { count: modules.length }) },
                  { id: "loaded", label: t("stack.filterLoaded", { count: moduleCounts.loaded }) },
                  { id: "missing", label: t("stack.filterMissing", { count: moduleCounts.missing }) },
                  ...(moduleCounts.unreferenced > 0
                    ? [
                        {
                          id: "unreferenced" as const,
                          label: t("stack.filterUnreferenced", {
                            count: moduleCounts.unreferenced,
                          }),
                        },
                      ]
                    : []),
                  { id: "corrupt", label: t("stack.filterCorrupt", { count: moduleCounts.corrupt }) },
                ] as const
              ).map((pill) => (
                <button
                  key={pill.id}
                  type="button"
                  onClick={() => setModuleFilter(pill.id)}
                  className={`rounded-md border px-2.5 py-1 text-xs font-medium transition-colors ${
                    moduleFilter === pill.id
                      ? "border-indigo-300 bg-indigo-50 text-indigo-700"
                      : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
                  }`}
                >
                  {pill.label}
                </button>
              ))}
            </div>
          </div>

          <div className="ck-scrollbar max-h-[520px] overflow-auto">
            {filteredModules.length === 0 ? (
              <div className="px-6 py-10 text-center text-sm text-slate-500">
                {modules.length === 0
                  ? t("stack.noModulesMetadata")
                  : t("stack.noModulesMatch")}
              </div>
            ) : (
              <table className="min-w-full divide-y divide-slate-200 text-left text-xs">
                <thead className="sticky top-0 z-10 bg-slate-50 text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                  <tr>
                    <th className="px-4 py-2.5">{t("stack.colModule")}</th>
                    <th className="px-4 py-2.5">{t("stack.colVersion")}</th>
                    <th className="px-4 py-2.5">{t("stack.colAddressRange")}</th>
                    <th className="px-4 py-2.5">{t("stack.colDebugFileId")}</th>
                    <th className="px-4 py-2.5">{t("stack.colSymbols")}</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 bg-white">
                  {filteredModules.map((mod, idx) => {
                    const modStatus = getModuleStatus(mod);
                    return (
                      <tr
                        key={`${mod.filename ?? mod.module_name ?? mod.debug_id ?? "mod"}-${mod.base_addr ?? idx}-${idx}`}
                        className="hover:bg-slate-50"
                      >
                        <td className="max-w-[220px] px-4 py-2.5">
                          <div
                            className="truncate font-mono font-medium text-slate-900"
                            title={mod.filename ?? undefined}
                          >
                            {getModuleDisplayName(mod, t("stack.unknownModule"))}
                          </div>
                          <div
                            className="truncate font-mono text-[11px] text-slate-400"
                            title={mod.filename ?? undefined}
                          >
                            {mod.filename || "-"}
                          </div>
                        </td>
                        <td className="whitespace-nowrap px-4 py-2.5 font-mono text-slate-600">
                          {mod.version || "-"}
                        </td>
                        <td className="whitespace-nowrap px-4 py-2.5 font-mono text-[11px] text-slate-600">
                          {mod.base_addr || "-"}
                          {mod.end_addr ? ` - ${mod.end_addr}` : ""}
                        </td>
                        <td className="max-w-[240px] px-4 py-2.5">
                          <div className="truncate font-mono text-slate-700">
                            {mod.debug_file || mod.code_file || "-"}
                          </div>
                          {(mod.debug_id || mod.code_id) && (
                            <div className="truncate font-mono text-[11px] text-slate-400">
                              {mod.debug_id || mod.code_id}
                            </div>
                          )}
                        </td>
                        <td className="whitespace-nowrap px-4 py-2.5">
                          {modStatus === "loaded" ? (
                            <span className="inline-flex rounded border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700">
                              {t("stack.statusLoaded")}
                            </span>
                          ) : modStatus === "corrupt" ? (
                            <span className="inline-flex rounded border border-rose-200 bg-rose-50 px-2 py-0.5 text-[11px] font-medium text-rose-700">
                              {t("stack.statusCorrupt")}
                            </span>
                          ) : modStatus === "unreferenced" ? (
                            <span className="inline-flex rounded border border-slate-200 bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600">
                              {t("stack.statusUnreferenced")}
                            </span>
                          ) : (
                            <span className="inline-flex rounded border border-amber-200 bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-800">
                              {t("stack.statusMissing")}
                            </span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default StackTraceViewer;
