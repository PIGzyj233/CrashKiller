import type {
  MissingSymbolQueueItem,
  PublicSymbolSyncTarget,
} from "../api/types";

const INVALID_FILENAME_RE = /[<>:"/\\|?*\x00-\x1f]/;
const PDB_DEBUG_ID_RE = /^[0-9a-fA-F]{33,40}$/;
const CODE_ID_RE = /^[0-9a-fA-F]{8,24}$/;
const ALL_ZERO_RE = /^0+$/;
const DASHED_GUID_PATTERN =
  "[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}";
const MANUAL_GUID_RE = new RegExp(
  `^(?:(${DASHED_GUID_PATTERN})|\\{(${DASHED_GUID_PATTERN})\\}|([0-9a-fA-F]{32}))([0-9a-fA-F]{0,8})$`,
);

/**
 * Mirrors backend `_is_valid_pdb_identity` strict Breakpad PDB Debug ID check:
 * 33-40 hex characters (32-hex GUID + 1-8 hex age) with a non-zero GUID.
 */
export function isValidPdbDebugId(
  rawDebugId: string | null | undefined,
): boolean {
  if (!rawDebugId) {
    return false;
  }
  const trimmed = rawDebugId.trim();
  if (!PDB_DEBUG_ID_RE.test(trimmed)) {
    return false;
  }
  return !ALL_ZERO_RE.test(trimmed) && !ALL_ZERO_RE.test(trimmed.slice(0, 32));
}

/**
 * Mirrors backend `parse_manual_ms_target` Debug ID / GUID validation:
 * accepts a 32-hex, dashed, or braced GUID optionally followed by 0-8 hex age
 * characters, and rejects all-zero GUIDs (even when an age suffix is non-zero).
 */
export function isValidMsSyncDebugId(
  rawDebugId: string | null | undefined,
): boolean {
  if (!rawDebugId) {
    return false;
  }
  const trimmed = rawDebugId.trim();
  const match = MANUAL_GUID_RE.exec(trimmed);
  if (!match) {
    return false;
  }
  const rawGuid = match[1] ?? match[2] ?? match[3] ?? "";
  const guidHex = rawGuid.replace(/-/g, "");
  return guidHex.length === 32 && !ALL_ZERO_RE.test(guidHex);
}

/**
 * Mirrors backend `_is_valid_pdb_identity` / `_is_safe_segment` filename checks.
 */
export function isSafePdbDebugFile(
  rawDebugFile: string | null | undefined,
): boolean {
  if (!rawDebugFile) {
    return false;
  }
  const trimmed = rawDebugFile.trim();
  if (!trimmed || trimmed.length > 255) {
    return false;
  }
  if (!trimmed.toLowerCase().endsWith(".pdb")) {
    return false;
  }
  const stem = trimmed.slice(0, -4);
  if (!stem) {
    return false;
  }
  if (
    trimmed === "." ||
    trimmed === ".." ||
    trimmed.includes("..") ||
    INVALID_FILENAME_RE.test(trimmed)
  ) {
    return false;
  }
  return true;
}

export function canMsSyncMissingItem(item: {
  debug_file: string;
  debug_id: string;
}): boolean {
  return (
    isSafePdbDebugFile(item.debug_file) && isValidMsSyncDebugId(item.debug_id)
  );
}

/**
 * Mirrors backend `mode="missing_windows"` eligibility (`_is_valid_pdb_identity`)
 * so the "Sync Microsoft Symbols (N)" badge matches what auto-sync processes.
 */
export function canAutoSyncMissingWindowsItem(item: {
  debug_file: string;
  debug_id: string;
  is_windows_system?: boolean;
  is_available_locally?: boolean;
}): boolean {
  return (
    Boolean(item.is_windows_system) &&
    !item.is_available_locally &&
    isSafePdbDebugFile(item.debug_file) &&
    isValidPdbDebugId(item.debug_id)
  );
}

export function sanitizeOptionalCodeFile(
  rawCodeFile: string | null | undefined,
): string | undefined {
  if (!rawCodeFile) {
    return undefined;
  }
  const cleaned = rawCodeFile.trim().replace(/\\/g, "/").replace(/\/+$/, "");
  if (!cleaned || cleaned.includes("..")) {
    return undefined;
  }
  const base = cleaned.slice(cleaned.lastIndexOf("/") + 1);
  if (
    !base ||
    base.length > 255 ||
    base === "." ||
    base === ".." ||
    INVALID_FILENAME_RE.test(base)
  ) {
    return undefined;
  }
  return base;
}

export function sanitizeOptionalCodeId(
  rawCodeId: string | null | undefined,
): string | undefined {
  if (!rawCodeId) {
    return undefined;
  }
  const trimmed = rawCodeId.trim();
  if (!CODE_ID_RE.test(trimmed) || ALL_ZERO_RE.test(trimmed)) {
    return undefined;
  }
  return trimmed;
}

export function buildMsSyncTarget(
  item: Pick<
    MissingSymbolQueueItem,
    "module_name" | "debug_file" | "debug_id" | "code_file" | "code_id"
  >,
): PublicSymbolSyncTarget {
  return {
    module_name: item.module_name,
    debug_file: item.debug_file.trim(),
    debug_id: item.debug_id.trim(),
    code_file: sanitizeOptionalCodeFile(item.code_file),
    code_id: sanitizeOptionalCodeId(item.code_id),
  };
}
