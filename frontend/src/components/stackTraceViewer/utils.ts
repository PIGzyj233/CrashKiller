import type { MissingSymbolInfo, ParsedStacks } from "../../api/types";
import type {
  CompatFrame,
  CompatModule,
  CompatThread,
  CrashingMissingInfo,
  ModuleCounts,
  ModuleFilter,
  TrustBadgeMeta,
} from "./types";

export function getThreadId(thread: CompatThread): number | undefined {
  return thread.id ?? thread.thread_id ?? undefined;
}

export function resolveCrashingThreadIndex(parsedStacks: ParsedStacks): number {
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

export function dedupeMissingSymbols(
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

export function extractThreadRegisters(
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

export function getModuleDisplayName(
  mod: CompatModule,
  fallbackLabel: string,
): string {
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

export function formatOffset(offset: string | number | null | undefined): string {
  if (offset === null || offset === undefined || offset === "") {
    return "";
  }
  if (typeof offset === "number") {
    return `+0x${offset.toString(16)}`;
  }
  const trimmed = offset.trim();
  return trimmed.startsWith("+") ? trimmed : `+${trimmed}`;
}

export function getTrustBadgeMeta(
  trust: string | null | undefined,
): TrustBadgeMeta | null {
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

export function getModuleStatus(
  mod: CompatModule,
): Exclude<ModuleFilter, "all"> {
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

export function analyzeCrashingMissingSymbols(
  crashingThread: CompatThread | undefined,
  consolidatedMissing: MissingSymbolInfo[],
): CrashingMissingInfo {
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
}

export function computeModuleCounts(modules: CompatModule[]): ModuleCounts {
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
}

export function filterModules(
  modules: CompatModule[],
  moduleFilter: ModuleFilter,
  moduleQuery: string,
): CompatModule[] {
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
}

export function formatThreadStackForClipboard(
  activeThread: CompatThread,
  visibleFrames: CompatFrame[],
  crashingThreadIdx: number,
): string {
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
  return lines.join("\n");
}
