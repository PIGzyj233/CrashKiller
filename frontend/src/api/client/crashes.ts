import type {
  AIVerdict,
  ArtifactKind,
  CrashDetailRead,
  CrashListResponse,
  CrashPinResponse,
  CrashReprocessRequest,
  CrashReprocessResponse,
  CrashSSEEvent,
  CrashStatus,
  CrashUploadResponse,
  CrashVerdictRequest,
} from "../types";
import {
  API_BASE,
  buildQueryString,
  requestJson,
  sendMultipartXhr,
} from "./http";

// ============================================================================
// Crashes & SSE (/api/v1/crashes)
// ============================================================================

export interface ListCrashesParams {
  project_id?: string;
  status?: CrashStatus;
  ai_verdict?: AIVerdict;
  issue_id?: number;
  cluster_id?: string;
  app_version?: string;
  session_id?: string;
  is_pinned?: boolean;
  is_human_verified?: boolean;
  q?: string;
  limit?: number;
  offset?: number;
}

export interface UploadCrashParams {
  projectKey?: string;
  projectId?: string;
  appVersion: string;
  dumpFile: File;
  logFiles?: File[];
  buildId?: string;
  osVersion?: string;
  sessionId?: string;
  crashTime?: string;
  apiKey?: string;
  onProgress?: (percent: number) => void;
  signal?: AbortSignal;
}

export interface CrashSSEHandlers {
  onStatus: (event: CrashSSEEvent) => void;
  onError?: (event: Event) => void;
  onOpen?: () => void;
}

export function listCrashes(
  params?: ListCrashesParams,
): Promise<CrashListResponse> {
  const qs = buildQueryString(params);
  return requestJson<CrashListResponse>(`${API_BASE}/crashes${qs}`);
}

export function getCrash(crashId: string): Promise<CrashDetailRead> {
  return requestJson<CrashDetailRead>(
    `${API_BASE}/crashes/${encodeURIComponent(crashId)}`,
  );
}

export function uploadCrash(
  params: UploadCrashParams,
): Promise<CrashUploadResponse> {
  const formData = new FormData();
  if (params.projectKey) {
    formData.append("project_key", params.projectKey);
  } else if (params.projectId) {
    formData.append("project_id", params.projectId);
  }
  formData.append("app_version", params.appVersion);
  formData.append("dump_file", params.dumpFile);

  for (const logFile of params.logFiles ?? []) {
    formData.append("log_files", logFile);
  }

  if (params.buildId) {
    formData.append("build_id", params.buildId);
  }
  if (params.osVersion) {
    formData.append("os_version", params.osVersion);
  }
  if (params.sessionId) {
    formData.append("session_id", params.sessionId);
  }
  if (params.crashTime) {
    formData.append("crash_time", params.crashTime);
  }

  const trimmedKey = params.apiKey?.trim();
  if (trimmedKey) {
    formData.append("api_key", trimmedKey);
  }

  return sendMultipartXhr<CrashUploadResponse>({
    url: `${API_BASE}/crashes/upload`,
    formData,
    apiKey: trimmedKey,
    onProgress: params.onProgress,
    signal: params.signal,
  });
}

export function createCrashEventsSource(
  crashId: string,
  handlers: CrashSSEHandlers,
): () => void {
  const source = new EventSource(
    `${API_BASE}/crashes/${encodeURIComponent(crashId)}/events`,
  );

  const onStatusEvent = (evt: MessageEvent) => {
    try {
      const payload = JSON.parse(String(evt.data)) as CrashSSEEvent;
      handlers.onStatus(payload);
    } catch {
      // Ignore malformed SSE frames/keepalives
    }
  };

  source.addEventListener("status", onStatusEvent as EventListener);

  if (handlers.onOpen) {
    source.onopen = () => {
      handlers.onOpen?.();
    };
  }

  if (handlers.onError) {
    source.onerror = (evt) => {
      handlers.onError?.(evt);
    };
  }

  return () => {
    source.removeEventListener("status", onStatusEvent as EventListener);
    source.close();
  };
}

export function reprocessCrash(
  crashId: string,
  payload: CrashReprocessRequest = { mode: "full" },
): Promise<CrashReprocessResponse> {
  return requestJson<CrashReprocessResponse>(
    `${API_BASE}/crashes/${encodeURIComponent(crashId)}/reprocess`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function submitCrashVerdict(
  crashId: string,
  payload: CrashVerdictRequest,
): Promise<CrashDetailRead> {
  return requestJson<CrashDetailRead>(
    `${API_BASE}/crashes/${encodeURIComponent(crashId)}/verdict`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function pinCrash(
  crashId: string,
  isPinned: boolean,
): Promise<CrashPinResponse> {
  return requestJson<CrashPinResponse>(
    `${API_BASE}/crashes/${encodeURIComponent(crashId)}/pin`,
    {
      method: "PATCH",
      body: JSON.stringify({ is_pinned: isPinned }),
    },
  );
}

export function getCrashArtifactUrl(
  crashId: string,
  kind: ArtifactKind,
): string {
  return `${API_BASE}/crashes/${encodeURIComponent(crashId)}/artifacts/${encodeURIComponent(kind)}`;
}
