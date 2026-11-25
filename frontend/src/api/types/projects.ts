import type { AITriageMode } from "./common";

// ============================================================================
// Projects (/api/v1/projects)
// ============================================================================

export interface ProjectSettings {
  ai_triage_mode?: AITriageMode | null;
  noise_modules?: string[];
  noise_functions?: string[];
  custom_crash_handlers?: string[];
  log_retention_days?: number;
  dump_retention_days?: number;
  session_id_label?: string | null;
  [key: string]: unknown;
}

export interface ProjectRead {
  id: string;
  key: string;
  name: string;
  settings_json: ProjectSettings;
  created_at: string;
}

export interface ProjectCreate {
  key: string;
  name: string;
  settings_json?: ProjectSettings;
}

export interface ProjectCreateResponse extends ProjectRead {
  api_key: string;
}

export interface ProjectUpdate {
  name?: string;
  settings_json?: ProjectSettings;
}

export interface ProjectRotateKeyResponse {
  id: string;
  key: string;
  api_key: string;
}

export interface ProjectApiKeyResponse {
  id: string;
  key: string;
  api_key: string | null;
}

export interface ProjectApiKeySyncRequest {
  api_key: string;
}
