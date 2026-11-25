import { Check, Copy, Filter } from "lucide-react";
import { useAppI18n } from "../../i18n";
import { RegistersPanel } from "./RegistersPanel";
import type { CompatFrame, CompatThread } from "./types";
import { formatOffset, getThreadId, getTrustBadgeMeta } from "./utils";

export interface ThreadStackTabProps {
  threads: CompatThread[];
  activeThread: CompatThread | undefined;
  crashingThreadIdx: number;
  activeFrames: CompatFrame[];
  visibleFrames: CompatFrame[];
  noiseCount: number;
  hideNoiseFrames: boolean;
  copiedStack: boolean;
  threadRegisters: Record<string, string> | null;
  registersExpanded: boolean;
  registerFilter: string;
  onSelectThread: (threadIndex: number) => void;
  onToggleHideNoise: () => void;
  onShowAllFrames: () => void;
  onCopyStack: () => void;
  onToggleRegistersExpanded: () => void;
  onRegisterFilterChange: (value: string) => void;
}

export function ThreadStackTab({
  threads,
  activeThread,
  crashingThreadIdx,
  activeFrames,
  visibleFrames,
  noiseCount,
  hideNoiseFrames,
  copiedStack,
  threadRegisters,
  registersExpanded,
  registerFilter,
  onSelectThread,
  onToggleHideNoise,
  onShowAllFrames,
  onCopyStack,
  onToggleRegistersExpanded,
  onRegisterFilterChange,
}: ThreadStackTabProps) {
  const { t } = useAppI18n();

  return (
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
            onChange={(e) => onSelectThread(Number(e.target.value))}
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

          {activeThread && activeThread.thread_index !== crashingThreadIdx && (
            <button
              type="button"
              onClick={() => onSelectThread(crashingThreadIdx)}
              className="inline-flex items-center gap-1 rounded-md border border-rose-200 bg-rose-50 px-2 py-1 text-xs font-medium text-rose-700 hover:bg-rose-100"
            >
              {t("stack.jumpToCrashed", { index: crashingThreadIdx })}
            </button>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={onToggleHideNoise}
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
            onClick={onCopyStack}
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

      {/* Quick Thread Pills */}
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
                onClick={() => onSelectThread(thread.thread_index)}
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
                onClick={onShowAllFrames}
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
                          <span className="text-slate-400">ip:{frame.ip}</span>
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
      <RegistersPanel
        threadRegisters={threadRegisters}
        expanded={registersExpanded}
        onToggleExpanded={onToggleRegistersExpanded}
        filter={registerFilter}
        onFilterChange={onRegisterFilterChange}
      />
    </div>
  );
}
