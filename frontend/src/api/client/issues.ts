import type {
  ClusterListResponse,
  IssueBatchTransitionRequest,
  IssueBatchTransitionResponse,
  IssueCreateRequest,
  IssueDetailRead,
  IssueListResponse,
  IssueMergeRequest,
  IssueRead,
  IssueSplitRequest,
  IssueSplitResponse,
  IssueStatus,
  IssueTransitionRequest,
  IssueTransitionResponse,
  IssueUpdateRequest,
} from "../types";
import { API_BASE, buildQueryString, requestJson } from "./http";

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

export function transitionIssue(
  issueId: number,
  payload: IssueTransitionRequest,
): Promise<IssueTransitionResponse> {
  return requestJson<IssueTransitionResponse>(
    `${API_BASE}/issues/${encodeURIComponent(String(issueId))}/transition`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function batchTransitionIssues(
  payload: IssueBatchTransitionRequest,
): Promise<IssueBatchTransitionResponse> {
  return requestJson<IssueBatchTransitionResponse>(
    `${API_BASE}/issues/batch-transition`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}
