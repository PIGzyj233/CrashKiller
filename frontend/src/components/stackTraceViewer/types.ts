import type {
  LoadedModule,
  MissingSymbolInfo,
  StackFrame,
  StackThread,
} from "../../api/types";

export type CompatThread = StackThread & { thread_id?: number | null };

export type CompatFrame = StackFrame & {
  module_offset?: string | number | null;
};

export type CompatModule = Omit<LoadedModule, "filename"> & {
  filename?: string | null;
  module_name?: string | null;
};

export type ModuleFilter =
  | "all"
  | "loaded"
  | "missing"
  | "unreferenced"
  | "corrupt";

export interface TrustBadgeMeta {
  label: string;
  className: string;
  descKind: "context" | "cfi" | "fp" | "scan" | "other";
  rawTrust: string;
}

export interface CrashingMissingInfo {
  affectedFrameCount: number;
  isItemOnCrashingStack: (item: MissingSymbolInfo) => boolean;
  sortedMissing: MissingSymbolInfo[];
}

export interface ModuleCounts {
  loaded: number;
  missing: number;
  unreferenced: number;
  corrupt: number;
}
