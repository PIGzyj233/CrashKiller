import type {
  MissingSymbolListResult,
  MissingSymbolQueueItem,
  PublicSymbolSyncRequest,
  PublicSymbolSyncResponse,
  PublicSymbolSyncStreamEvent,
  ResymbolicateRequest,
  ResymbolicateResponse,
  ResymbolicateStatusResponse,
  SymbolListResponse,
  SymbolScope,
  SymbolUploadResponse,
} from "../types";
import {
  API_BASE,
  ApiError,
  buildQueryString,
  parseErrorPayload,
  requestJson,
  sendMultipartXhr,
} from "./http";

// ============================================================================
// Symbols (/api/v1/symbols)
// ============================================================================

export interface ListSymbolsParams {
  project_id?: string;
  scope?: SymbolScope | "all";
  source?: string;
  app_version?: string;
  q?: string;
  has_pe_unwind?: boolean;
  limit?: number;
  offset?: number;
}

export interface ListMissingSymbolsParams {
  project_id?: string;
  limit?: number;
  offset?: number;
}

export interface UploadSymbolParams {
  projectKey?: string;
  projectId?: string;
  appVersion: string;
  pdbFile: File;
  peFile?: File | null;
  apiKey?: string;
  onProgress?: (percent: number) => void;
  signal?: AbortSignal;
}

export interface UploadPublicSymbolParams {
  pdbFile: File;
  peFile?: File | null;
  autoResymbolicate?: boolean;
  onProgress?: (percent: number) => void;
  signal?: AbortSignal;
}

export function listSymbols(
  params?: ListSymbolsParams,
): Promise<SymbolListResponse> {
  const qs = buildQueryString(params);
  return requestJson<SymbolListResponse>(`${API_BASE}/symbols${qs}`);
}

export function listMissingSymbols(
  params?: ListMissingSymbolsParams,
): Promise<MissingSymbolQueueItem[]> {
  const qs = buildQueryString(params);
  return requestJson<MissingSymbolQueueItem[]>(
    `${API_BASE}/symbols/missing${qs}`,
  );
}

export async function listMissingSymbolsWithTotal(
  params?: ListMissingSymbolsParams,
): Promise<MissingSymbolListResult> {
  const qs = buildQueryString(params);
  const response = await fetch(`${API_BASE}/symbols/missing${qs}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    const rawText = await response.text();
    const { message, detail } = parseErrorPayload(
      response.status,
      response.statusText,
      rawText,
    );
    throw new ApiError(response.status, message, detail);
  }
  const items = (await response.json()) as MissingSymbolQueueItem[];
  const rawTotal = response.headers.get("X-Total-Count");
  const parsedTotal =
    rawTotal !== null ? Number.parseInt(rawTotal, 10) : Number.NaN;
  return {
    items,
    total:
      Number.isFinite(parsedTotal) && parsedTotal >= 0
        ? parsedTotal
        : items.length,
  };
}

export function resymbolicate(
  payload: ResymbolicateRequest,
): Promise<ResymbolicateResponse> {
  return requestJson<ResymbolicateResponse>(
    `${API_BASE}/symbols/resymbolicate`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function getResymbolicateStatus(
  crashIds: string[],
): Promise<ResymbolicateStatusResponse> {
  const params = new URLSearchParams();
  for (const id of crashIds) {
    params.append("crash_ids", id);
  }
  return requestJson<ResymbolicateStatusResponse>(
    `${API_BASE}/symbols/resymbolicate/status?${params.toString()}`,
  );
}

export function syncPublicSymbols(
  payload: PublicSymbolSyncRequest,
): Promise<PublicSymbolSyncResponse> {
  return requestJson<PublicSymbolSyncResponse>(
    `${API_BASE}/symbols/public/sync`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export interface SyncPublicSymbolsStreamOptions {
  onEvent?: (event: PublicSymbolSyncStreamEvent) => void;
  signal?: AbortSignal;
}

export async function syncPublicSymbolsStream(
  payload: PublicSymbolSyncRequest,
  options?: SyncPublicSymbolsStreamOptions,
): Promise<PublicSymbolSyncResponse> {
  const response = await fetch(`${API_BASE}/symbols/public/sync/stream`, {
    method: "POST",
    headers: {
      Accept: "text/event-stream",
      "Content-Type": "application/json",
    },
    credentials: "same-origin",
    body: JSON.stringify(payload),
    signal: options?.signal,
  });

  if (!response.ok) {
    const rawText = await response.text();
    const { message, detail } = parseErrorPayload(
      response.status,
      response.statusText,
      rawText,
    );
    throw new ApiError(response.status, message, detail);
  }

  if (!response.body) {
    throw new ApiError(500, "Streaming response body is unavailable");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalSummary: PublicSymbolSyncResponse | null = null;

  const processBlock = (block: string) => {
    const lines = block.split(/\r?\n/);
    const dataLines: string[] = [];
    for (const line of lines) {
      if (!line || line.startsWith(":")) {
        continue;
      }
      if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trimStart());
      }
    }
    if (dataLines.length === 0) {
      return;
    }
    const rawJson = dataLines.join("\n");
    const parsed = JSON.parse(rawJson) as PublicSymbolSyncStreamEvent;
    options?.onEvent?.(parsed);
    if (parsed.type === "complete") {
      finalSummary = parsed.summary;
    } else if (parsed.type === "error") {
      throw new ApiError(500, parsed.detail || "Public symbol sync failed");
    }
  };

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) {
        buffer += decoder.decode();
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split(/\r?\n\r?\n/);
      buffer = parts.pop() ?? "";
      for (const part of parts) {
        if (part.trim()) {
          processBlock(part);
        }
      }
    }
    if (buffer.trim()) {
      processBlock(buffer);
    }
  } finally {
    reader.releaseLock();
  }

  if (!finalSummary) {
    throw new ApiError(500, "Symbol sync stream ended unexpectedly");
  }
  return finalSummary;
}

export function deleteSymbol(symbolId: string): Promise<void> {
  return requestJson<void>(
    `${API_BASE}/symbols/${encodeURIComponent(symbolId)}`,
    {
      method: "DELETE",
    },
  );
}

export function uploadSymbol(
  params: UploadSymbolParams,
): Promise<SymbolUploadResponse> {
  const formData = new FormData();
  if (params.projectKey) {
    formData.append("project_key", params.projectKey);
  } else if (params.projectId) {
    formData.append("project_id", params.projectId);
  }
  formData.append("app_version", params.appVersion);
  formData.append("pdb_file", params.pdbFile);
  if (params.peFile) {
    formData.append("pe_file", params.peFile);
  }

  const trimmedKey = params.apiKey?.trim();
  if (trimmedKey) {
    formData.append("api_key", trimmedKey);
  }

  return sendMultipartXhr<SymbolUploadResponse>({
    url: `${API_BASE}/symbols/upload`,
    formData,
    apiKey: trimmedKey,
    onProgress: params.onProgress,
    signal: params.signal,
  });
}

export function uploadPublicSymbol(
  params: UploadPublicSymbolParams,
): Promise<SymbolUploadResponse> {
  const formData = new FormData();
  formData.append("pdb_file", params.pdbFile);
  if (params.peFile) {
    formData.append("pe_file", params.peFile);
  }
  formData.append(
    "auto_resymbolicate",
    params.autoResymbolicate ? "true" : "false",
  );

  return sendMultipartXhr<SymbolUploadResponse>({
    url: `${API_BASE}/symbols/public/upload`,
    formData,
    onProgress: params.onProgress,
    signal: params.signal,
  });
}
