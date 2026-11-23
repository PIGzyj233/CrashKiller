import type {
  AIVerdict,
  ArtifactKind,
  ClusterListResponse,
  CrashDetailRead,
  CrashListResponse,
  CrashPinResponse,
  CrashReprocessRequest,
  CrashReprocessResponse,
  CrashSSEEvent,
  CrashStatus,
  CrashUploadResponse,
  CrashVerdictRequest,
  DashboardResponse,
  GCReport,
  HealthResponse,
  IssueCreateRequest,
  IssueDetailRead,
  IssueListResponse,
  IssueMergeRequest,
  IssueRead,
  IssueSplitRequest,
  IssueSplitResponse,
  IssueStatus,
  IssueUpdateRequest,
  LLMProviderPreset,
  LogAroundTimeResponse,
  LogLinesResponse,
  LogSearchResponse,
  LogSummaryResponse,
  MissingSymbolQueueItem,
  ProjectApiKeyResponse,
  ProjectCreate,
  ProjectCreateResponse,
  ProjectRead,
  ProjectRotateKeyResponse,
  ProjectUpdate,
  ResymbolicateRequest,
  ResymbolicateResponse,
  SymbolListResponse,
  SymbolUploadResponse,
  SystemSettingsRead,
  SystemSettingsUpdate,
} from "./types";

const API_BASE = "/api/v1";
const PROJECT_API_KEY_PREFIX = "crashkiller.project_api_key.";

// ============================================================================
// Error Handling
// ============================================================================

export interface FastAPIValidationErrorItem {
  loc?: Array<string | number>;
  msg?: string;
  type?: string;
}

export class ApiError extends Error {
  public readonly status: number;
  public readonly detail: unknown;

  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function formatValidationItem(item: unknown): string {
  if (typeof item === "object" && item !== null && "msg" in item) {
    const typed = item as FastAPIValidationErrorItem;
    const loc =
      Array.isArray(typed.loc) && typed.loc.length > 0
        ? `${typed.loc.join(".")}: `
        : "";
    return `${loc}${typed.msg ?? "Validation error"}`;
  }
  return JSON.stringify(item);
}

function parseErrorPayload(
  status: number,
  statusText: string,
  rawText: string,
): { message: string; detail: unknown } {
  let parsed: unknown = undefined;
  if (rawText.trim()) {
    try {
      parsed = JSON.parse(rawText) as unknown;
    } catch {
      return {
        message: rawText.trim().slice(0, 300),
        detail: rawText,
      };
    }
  }

  if (typeof parsed === "object" && parsed !== null && "detail" in parsed) {
    const detail = (parsed as { detail: unknown }).detail;
    if (typeof detail === "string") {
      return { message: detail, detail };
    }
    if (Array.isArray(detail)) {
      return {
        message: detail.map(formatValidationItem).join("; "),
        detail,
      };
    }
    return {
      message: JSON.stringify(detail),
      detail,
    };
  }

  if (rawText.trim()) {
    return {
      message: rawText.trim().slice(0, 300),
      detail: parsed ?? rawText,
    };
  }

  return {
    message: `HTTP ${status} ${statusText}`.trim(),
    detail: undefined,
  };
}

// ============================================================================
// Query String & JSON Request Helpers
// ============================================================================

export function buildQueryString(params?: object): string {
  if (!params) {
    return "";
  }
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") {
      continue;
    }
    search.set(key, String(value));
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (!headers.has("Accept")) {
    headers.set("Accept", "application/json");
  }
  if (typeof init?.body === "string" && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(path, {
    ...init,
    headers,
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

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

// ============================================================================
// localStorage Project API Key Helpers
// ============================================================================

export function getStoredProjectApiKey(projectKey: string): string | null {
  if (!projectKey || typeof window === "undefined") {
    return null;
  }
  try {
    return window.localStorage.getItem(`${PROJECT_API_KEY_PREFIX}${projectKey}`);
  } catch {
    return null;
  }
}

export function setStoredProjectApiKey(
  projectKey: string,
  apiKey: string,
): void {
  if (!projectKey || typeof window === "undefined") {
    return;
  }
  const trimmed = apiKey.trim();
  try {
    if (!trimmed) {
      window.localStorage.removeItem(`${PROJECT_API_KEY_PREFIX}${projectKey}`);
    } else {
      window.localStorage.setItem(
        `${PROJECT_API_KEY_PREFIX}${projectKey}`,
        trimmed,
      );
    }
  } catch {
    // Ignore storage quota or privacy mode restrictions
  }
}

export function removeStoredProjectApiKey(projectKey: string): void {
  if (!projectKey || typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.removeItem(`${PROJECT_API_KEY_PREFIX}${projectKey}`);
  } catch {
    // Ignore storage restrictions
  }
}

// ============================================================================
// Health & SymSrv
// ============================================================================

export function getHealth(): Promise<HealthResponse> {
  return requestJson<HealthResponse>("/health");
}

export function getSymSrvBaseUrl(): string {
  if (typeof window === "undefined") {
    return "/symsrv";
  }
  return `${window.location.origin}/symsrv`;
}

// ============================================================================
// Projects (/api/v1/projects)
// ============================================================================

export function listProjects(): Promise<ProjectRead[]> {
  return requestJson<ProjectRead[]>(`${API_BASE}/projects`);
}

export function getProject(projectId: string): Promise<ProjectRead> {
  return requestJson<ProjectRead>(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}`,
  );
}

export async function createProject(
  payload: ProjectCreate,
): Promise<ProjectCreateResponse> {
  const created = await requestJson<ProjectCreateResponse>(
    `${API_BASE}/projects`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
  if (created.key && created.api_key) {
    setStoredProjectApiKey(created.key, created.api_key);
  }
  return created;
}

export function updateProject(
  projectId: string,
  payload: ProjectUpdate,
): Promise<ProjectRead> {
  return requestJson<ProjectRead>(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
  );
}

export async function rotateProjectKey(
  projectId: string,
): Promise<ProjectRotateKeyResponse> {
  const rotated = await requestJson<ProjectRotateKeyResponse>(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/rotate-key`,
    {
      method: "POST",
    },
  );
  if (rotated.key && rotated.api_key) {
    setStoredProjectApiKey(rotated.key, rotated.api_key);
  }
  return rotated;
}

export async function getProjectApiKey(
  projectId: string,
): Promise<ProjectApiKeyResponse> {
  const res = await requestJson<ProjectApiKeyResponse>(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/api-key`,
  );
  if (res.key && res.api_key) {
    setStoredProjectApiKey(res.key, res.api_key);
  }
  return res;
}

export async function syncProjectApiKey(
  projectId: string,
  apiKey: string,
): Promise<ProjectApiKeyResponse> {
  const res = await requestJson<ProjectApiKeyResponse>(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/api-key/sync`,
    {
      method: "POST",
      body: JSON.stringify({ api_key: apiKey }),
    },
  );
  if (res.key && res.api_key) {
    setStoredProjectApiKey(res.key, res.api_key);
  }
  return res;
}

// ============================================================================
// Symbols (/api/v1/symbols)
// ============================================================================

export interface ListSymbolsParams {
  project_id?: string;
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

// ============================================================================
// Logs (/api/v1/logs)
// ============================================================================

export interface LogAroundTimeParams {
  timestamp: string;
  window_sec?: number;
  max_lines?: number;
}

export interface SearchLogParams {
  pattern: string;
  is_regex?: boolean;
  max_matches?: number;
  case_sensitive?: boolean;
}

export function getLogSummary(logId: string): Promise<LogSummaryResponse> {
  return requestJson<LogSummaryResponse>(
    `${API_BASE}/logs/${encodeURIComponent(logId)}/summary`,
  );
}

export function getLogLines(
  logId: string,
  start = 1,
  end = 200,
): Promise<LogLinesResponse> {
  const qs = buildQueryString({ start, end });
  return requestJson<LogLinesResponse>(
    `${API_BASE}/logs/${encodeURIComponent(logId)}/lines${qs}`,
  );
}

export function getLogAroundTime(
  logId: string,
  params: LogAroundTimeParams,
): Promise<LogAroundTimeResponse> {
  const qs = buildQueryString({
    timestamp: params.timestamp,
    window_sec: params.window_sec,
    max_lines: params.max_lines,
  });
  return requestJson<LogAroundTimeResponse>(
    `${API_BASE}/logs/${encodeURIComponent(logId)}/around-time${qs}`,
  );
}

export function searchLog(
  logId: string,
  params: SearchLogParams,
): Promise<LogSearchResponse> {
  const qs = buildQueryString({
    pattern: params.pattern,
    is_regex: params.is_regex,
    max_matches: params.max_matches,
    case_sensitive: params.case_sensitive,
  });
  return requestJson<LogSearchResponse>(
    `${API_BASE}/logs/${encodeURIComponent(logId)}/search${qs}`,
  );
}

// ============================================================================
// Issues & Clusters (/api/v1/issues)
// ============================================================================

export interface ListIssuesParams {
  project_id?: string;
  status?: IssueStatus;
  component?: string;
  is_pinned?: boolean;
  q?: string;
  limit?: number;
  offset?: number;
}

export interface ListClustersParams {
  project_id?: string;
  issue_id?: number;
  unassigned_only?: boolean;
  limit?: number;
  offset?: number;
}

export function listIssues(
  params?: ListIssuesParams,
): Promise<IssueListResponse> {
  const qs = buildQueryString(params);
  return requestJson<IssueListResponse>(`${API_BASE}/issues${qs}`);
}

export function listClusters(
  params?: ListClustersParams,
): Promise<ClusterListResponse> {
  const qs = buildQueryString(params);
  return requestJson<ClusterListResponse>(`${API_BASE}/issues/clusters${qs}`);
}

export function createIssue(
  payload: IssueCreateRequest,
): Promise<IssueRead> {
  return requestJson<IssueRead>(`${API_BASE}/issues`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getIssue(issueId: number): Promise<IssueDetailRead> {
  return requestJson<IssueDetailRead>(
    `${API_BASE}/issues/${encodeURIComponent(String(issueId))}`,
  );
}

export function updateIssue(
  issueId: number,
  payload: IssueUpdateRequest,
): Promise<IssueRead> {
  return requestJson<IssueRead>(
    `${API_BASE}/issues/${encodeURIComponent(String(issueId))}`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
  );
}

export function mergeIssues(
  payload: IssueMergeRequest,
): Promise<IssueDetailRead> {
  return requestJson<IssueDetailRead>(`${API_BASE}/issues/merge`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function splitIssueCluster(
  issueId: number,
  payload: IssueSplitRequest,
): Promise<IssueSplitResponse> {
  return requestJson<IssueSplitResponse>(
    `${API_BASE}/issues/${encodeURIComponent(String(issueId))}/split`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

// ============================================================================
// Settings, Storage & GC (/api/v1/settings)
// ============================================================================

export function getSystemSettings(): Promise<SystemSettingsRead> {
  return requestJson<SystemSettingsRead>(`${API_BASE}/settings`);
}

export function listLLMProviders(): Promise<LLMProviderPreset[]> {
  return requestJson<LLMProviderPreset[]>(`${API_BASE}/settings/llm-providers`);
}

export function updateSystemSettings(
  payload: SystemSettingsUpdate,
): Promise<SystemSettingsRead> {
  return requestJson<SystemSettingsRead>(`${API_BASE}/settings`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function triggerGC(forceEmergency = false): Promise<GCReport> {
  return requestJson<GCReport>(`${API_BASE}/settings/gc`, {
    method: "POST",
    body: JSON.stringify({ force_emergency: forceEmergency }),
  });
}

// ============================================================================
// Dashboard (/api/v1/dashboard)
// ============================================================================

export interface GetDashboardParams {
  project_id?: string;
  days?: number;
}

export function getDashboard(
  params?: GetDashboardParams,
): Promise<DashboardResponse> {
  const qs = buildQueryString(params);
  return requestJson<DashboardResponse>(`${API_BASE}/dashboard${qs}`);
}

// ============================================================================
// Internal XHR Multipart Helper
// ============================================================================

interface MultipartXhrOptions {
  url: string;
  formData: FormData;
  apiKey?: string;
  onProgress?: (percent: number) => void;
  signal?: AbortSignal;
}

function sendMultipartXhr<T>(options: MultipartXhrOptions): Promise<T> {
  const { url, formData, apiKey, onProgress, signal } = options;

  return new Promise<T>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new ApiError(0, "Upload aborted"));
      return;
    }

    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.setRequestHeader("Accept", "application/json");
    if (apiKey) {
      xhr.setRequestHeader("X-API-Key", apiKey);
    }

    const handleAbortSignal = () => {
      xhr.abort();
    };

    if (signal) {
      signal.addEventListener("abort", handleAbortSignal, { once: true });
    }

    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable && ev.total > 0 && onProgress) {
        const percent = Math.min(
          100,
          Math.round((ev.loaded / ev.total) * 100),
        );
        onProgress(percent);
      }
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as T);
        } catch {
          reject(
            new ApiError(
              xhr.status,
              "Invalid JSON response from server",
              xhr.responseText,
            ),
          );
        }
        return;
      }

      const { message, detail } = parseErrorPayload(
        xhr.status,
        xhr.statusText,
        xhr.responseText,
      );
      reject(new ApiError(xhr.status, message, detail));
    };

    xhr.onerror = () => {
      reject(new ApiError(0, "Network error during upload"));
    };

    xhr.onabort = () => {
      reject(new ApiError(0, "Upload aborted"));
    };

    xhr.onloadend = () => {
      if (signal) {
        signal.removeEventListener("abort", handleAbortSignal);
      }
    };

    xhr.send(formData);
  });
}
