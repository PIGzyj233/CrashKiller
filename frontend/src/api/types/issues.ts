import type { IssueStatus, IssueStatusEventSource } from "./common";
import type { CrashSummary } from "./crashes";

// ============================================================================
// Issues & Clusters (/api/v1/issues)
// ============================================================================

export type SignatureFrameItem =
  | string
  | {
      module?: string | null;
      function?: string | null;
      [key: string]: unknown;
    };

export interface CrashClusterRead {
  id: string;
  project_id: string;
  issue_id: number | null;
  signature_hash: string;
  signature_frames_json: SignatureFrameItem[];
  signature_text?: string | null;
  exception_type: string | null;
  crash_count: number;
}

export interface ClusterListResponse {
  items: CrashClusterRead[];
  total: number;
}

export interface IssueRead {
  id: number;
  project_id: string;
  title: string;
  status: IssueStatus;
  component: string | null;
  root_cause: string | null;
  log_patterns_json: string[];
  fixed_in_version: string | null;
  is_ambiguous_stack: boolean;
  is_pinned: boolean;
  crash_count: number;
  first_seen_at: string;
  last_seen_at: string;
}

export interface IssueListResponse {
  items: IssueRead[];
  total: number;
}

export interface IssueCreateRequest {
  project_id: string;
  title: string;
  status?: IssueStatus;
  component?: string | null;
  root_cause?: string | null;
  log_patterns_json?: string[];
  fixed_in_version?: string | null;
  is_ambiguous_stack?: boolean;
  is_pinned?: boolean;
}

export interface IssueUpdateRequest {
  title?: string;
  status?: IssueStatus;
  component?: string | null;
  root_cause?: string | null;
  log_patterns_json?: string[];
  fixed_in_version?: string | null;
  is_ambiguous_stack?: boolean;
  is_pinned?: boolean;
}

export interface IssueMergeRequest {
  target_issue_id: number;
  source_issue_ids: number[];
}

export interface IssueSplitRequest {
  cluster_id: string;
  target_issue_id?: number;
  new_issue_title?: string;
}

export interface IssueSplitResponse {
  source_issue: IssueRead;
  target_issue: IssueRead;
  cluster: CrashClusterRead;
}

export interface IssueStatusEventRead {
  id: number;
  issue_id: number;
  from_status: IssueStatus | null;
  to_status: IssueStatus;
  source: IssueStatusEventSource;
  fixed_in_version: string | null;
  note: string | null;
  crash_id: string | null;
  created_at: string;
}

export interface IssueTransitionRequest {
  to_status: IssueStatus;
  fixed_in_version?: string | null;
  clear_fixed_in_version?: boolean;
  note?: string | null;
}

export interface IssueTransitionResponse {
  issue: IssueRead;
  event: IssueStatusEventRead;
}

export interface IssueBatchTransitionRequest {
  issue_ids: number[];
  to_status: IssueStatus;
  fixed_in_version?: string | null;
  clear_fixed_in_version?: boolean;
  note?: string | null;
}

export interface IssueBatchTransitionResponse {
  updated_count: number;
  skipped_count: number;
  items: IssueRead[];
}

export interface IssueDetailRead extends IssueRead {
  clusters: CrashClusterRead[];
  recent_crashes: CrashSummary[];
  status_events: IssueStatusEventRead[];
}
