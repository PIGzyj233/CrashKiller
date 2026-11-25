import type {
  ProjectApiKeyResponse,
  ProjectCreate,
  ProjectCreateResponse,
  ProjectRead,
  ProjectRotateKeyResponse,
  ProjectUpdate,
} from "../types";
import { API_BASE, requestJson } from "./http";

const PROJECT_API_KEY_PREFIX = "crashkiller.project_api_key.";

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
