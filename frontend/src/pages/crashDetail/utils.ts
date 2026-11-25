import type {
  AIAnalysisResult,
  CrashDetailRead,
  CrashStatus,
  IssueStatus,
} from "../../api/types";

export const PIPELINE_STEPS: readonly CrashStatus[] = [
  "PENDING",
  "PARSING",
  "TRIAGING",
  "ANALYZED",
];

export const ISSUE_STATUSES: readonly IssueStatus[] = [
  "NEW",
  "KNOWN",
  "FIXED",
  "WONTFIX",
  "REGRESSION",
];

export function isCrashInFlight(status: CrashStatus | undefined): boolean {
  return status === "PENDING" || status === "PARSING" || status === "TRIAGING";
}

export function getLatestAIResult(
  crash: CrashDetailRead | undefined,
): AIAnalysisResult | null {
  if (!crash || !Array.isArray(crash.ai_runs) || crash.ai_runs.length === 0) {
    return null;
  }
  const sortedRuns = [...crash.ai_runs].sort((a, b) => {
    const ta = Date.parse(a.created_at);
    const tb = Date.parse(b.created_at);
    if (Number.isNaN(ta) || Number.isNaN(tb)) {
      return 0;
    }
    return tb - ta;
  });
  for (const run of sortedRuns) {
    if (run.result_json) {
      return run.result_json;
    }
  }
  return null;
}
