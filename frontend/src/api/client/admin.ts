import type {
  DashboardResponse,
  GCReport,
  HealthResponse,
  LLMProviderPreset,
  SystemSettingsRead,
  SystemSettingsUpdate,
} from "../types";
import { API_BASE, buildQueryString, requestJson } from "./http";

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
  refresh?: boolean;
}

export function getDashboard(
  params?: GetDashboardParams,
): Promise<DashboardResponse> {
  const qs = buildQueryString(params);
  return requestJson<DashboardResponse>(`${API_BASE}/dashboard${qs}`);
}
