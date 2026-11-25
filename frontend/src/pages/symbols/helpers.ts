import type {
  MissingSymbolQueueItem,
  PublicSymbolSyncItemResult,
  ResymbolicateStatusResponse,
} from "../../api/types";

export const PAGE_SIZE = 25;

export type SymbolsTab = "inventory" | "public" | "missing";

export interface LiveSyncProgressState {
  total: number;
  completed: number;
  alreadyCached: number;
  imported: number;
  upgraded: number;
  cachedHit: number;
  notFound: number;
  skipped: number;
  failed: number;
  circuitOpen: boolean;
  activeModules: string[];
  results: PublicSymbolSyncItemResult[];
}

export interface CancelledSyncInfo {
  completed: number;
  total: number;
  imported: number;
  upgraded: number;
  projectId?: string;
  results: PublicSymbolSyncItemResult[];
}

export interface TrackedResymState {
  mode: "queued" | "active_busy";
  projectId?: string;
  crashIds: string[];
  debugId?: string;
  status: ResymbolicateStatusResponse | null;
  skippedActiveCount?: number;
}

export interface ResymFeedbackState {
  queued_count: number;
  crash_ids: string[];
  debug_id?: string;
  skipped_active_count?: number;
}

export function isTrackedResymFinished(state: TrackedResymState): boolean {
  if (!state.status) {
    return false;
  }
  return state.mode === "active_busy"
    ? state.status.pending + state.status.parsing + state.status.triaging === 0
    : state.status.done;
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) {
    return "—";
  }
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

export function missingRowKey(item: MissingSymbolQueueItem): string {
  return `${item.debug_file.toLowerCase()}::${item.debug_id.toUpperCase()}`;
}
