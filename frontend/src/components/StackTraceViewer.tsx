import { useEffect, useMemo, useRef, useState } from "react";
import { Cpu, FileCode, Layers, Loader2, ShieldAlert } from "lucide-react";
import type {
  CrashStatus,
  MissingSymbolInfo,
  ParsedStacks,
} from "../api/types";
import { useAppI18n } from "../i18n";
import { LoadedModulesTab } from "./stackTraceViewer/LoadedModulesTab";
import { MissingSymbolsBanner } from "./stackTraceViewer/MissingSymbolsBanner";
import { ThreadStackTab } from "./stackTraceViewer/ThreadStackTab";
import type {
  CompatFrame,
  CompatModule,
  CompatThread,
  ModuleFilter,
} from "./stackTraceViewer/types";
import {
  analyzeCrashingMissingSymbols,
  computeModuleCounts,
  dedupeMissingSymbols,
  extractThreadRegisters,
  filterModules,
  formatThreadStackForClipboard,
  resolveCrashingThreadIndex,
} from "./stackTraceViewer/utils";

export interface StackTraceViewerProps {
  parsedStacks: ParsedStacks | null | undefined;
  missingSymbols?: MissingSymbolInfo[] | null;
  crashReason?: string | null;
  crashAddress?: string | null;
  status?: CrashStatus;
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
        (th) => th.thread_index === prev,
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

  const crashingMissingInfo = useMemo(
    () => analyzeCrashingMissingSymbols(crashingThread, consolidatedMissing),
    [crashingThread, consolidatedMissing],
  );

  const activeThread = useMemo(
    () =>
      threads.find((th) => th.thread_index === selectedThreadIndex) ??
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

  const modules = useMemo(
    () => (parsedStacks?.modules ?? []) as CompatModule[],
    [parsedStacks],
  );

  const moduleCounts = useMemo(() => computeModuleCounts(modules), [modules]);

  const filteredModules = useMemo(
    () => filterModules(modules, moduleFilter, moduleQuery),
    [modules, moduleFilter, moduleQuery],
  );

  const effectiveReason =
    crashReason ?? parsedStacks?.crash_reason ?? t("stack.unknownException");
  const effectiveAddress = crashAddress ?? parsedStacks?.crash_address ?? null;

  const handleCopyStack = async () => {
    if (!activeThread) {
      return;
    }
    const text = formatThreadStackForClipboard(
      activeThread,
      visibleFrames,
      crashingThreadIdx,
    );
    try {
      await navigator.clipboard.writeText(text);
      setCopiedStack(true);
      window.setTimeout(() => setCopiedStack(false), 1800);
    } catch {
      // Clipboard may be unavailable in insecure contexts
    }
  };

  if (!parsedStacks || threads.length === 0) {
    return (
      <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
        <MissingSymbolsBanner
          consolidatedMissing={consolidatedMissing}
          hasParsedThreads={false}
          crashingMissingInfo={crashingMissingInfo}
          expanded={missingBannerExpanded}
          onToggleExpanded={() => setMissingBannerExpanded((prev) => !prev)}
        />
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
      <MissingSymbolsBanner
        consolidatedMissing={consolidatedMissing}
        hasParsedThreads={true}
        crashingMissingInfo={crashingMissingInfo}
        expanded={missingBannerExpanded}
        onToggleExpanded={() => setMissingBannerExpanded((prev) => !prev)}
      />

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
        <ThreadStackTab
          threads={threads}
          activeThread={activeThread}
          crashingThreadIdx={crashingThreadIdx}
          activeFrames={activeFrames}
          visibleFrames={visibleFrames}
          noiseCount={noiseCount}
          hideNoiseFrames={hideNoiseFrames}
          copiedStack={copiedStack}
          threadRegisters={threadRegisters}
          registersExpanded={registersExpanded}
          registerFilter={registerFilter}
          onSelectThread={setSelectedThreadIndex}
          onToggleHideNoise={() => setHideNoiseFrames((prev) => !prev)}
          onShowAllFrames={() => setHideNoiseFrames(false)}
          onCopyStack={() => {
            void handleCopyStack();
          }}
          onToggleRegistersExpanded={() =>
            setRegistersExpanded((prev) => !prev)
          }
          onRegisterFilterChange={setRegisterFilter}
        />
      ) : (
        <LoadedModulesTab
          modules={modules}
          filteredModules={filteredModules}
          moduleCounts={moduleCounts}
          moduleQuery={moduleQuery}
          moduleFilter={moduleFilter}
          onModuleQueryChange={setModuleQuery}
          onModuleFilterChange={setModuleFilter}
        />
      )}
    </div>
  );
}

export default StackTraceViewer;
