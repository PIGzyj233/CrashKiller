import type { StorageReport } from "./settings";
import type { MissingSymbolQueueItem } from "./symbols";

// ============================================================================
// Dashboard (/api/v1/dashboard)
// ============================================================================

export interface DashboardKPIs {
  total_crashes: number;
  analyzed_crashes: number;
  pending_crashes: number;
  open_issues: number;
  missing_symbols_count: number;
  failed_crashes?: number;
  total_issues?: number;
  total_symbols?: number;
}

export interface CrashTrendPoint {
  date: string;
  count: number;
  analyzed?: number;
  failed?: number;
}

export interface VerdictDistributionItem {
  verdict: string;
  count: number;
}

export interface IssueStatusDistributionItem {
  status: string;
  count: number;
}

export interface DashboardResponse {
  kpis: DashboardKPIs;
  crash_trend: CrashTrendPoint[];
  verdict_distribution: VerdictDistributionItem[];
  issue_status_distribution: IssueStatusDistributionItem[];
  top_missing_symbols: MissingSymbolQueueItem[];
  storage: StorageReport;
}
