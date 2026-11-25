export { ApiError, buildQueryString } from "./client/http";
export type { FastAPIValidationErrorItem } from "./client/http";

export {
  getStoredProjectApiKey,
  setStoredProjectApiKey,
  removeStoredProjectApiKey,
  listProjects,
  getProject,
  createProject,
  updateProject,
  rotateProjectKey,
  getProjectApiKey,
  syncProjectApiKey,
} from "./client/projects";

export {
  listSymbols,
  listMissingSymbols,
  listMissingSymbolsWithTotal,
  resymbolicate,
  getResymbolicateStatus,
  syncPublicSymbols,
  syncPublicSymbolsStream,
  deleteSymbol,
  uploadSymbol,
  uploadPublicSymbol,
} from "./client/symbols";
export type {
  ListSymbolsParams,
  ListMissingSymbolsParams,
  UploadSymbolParams,
  UploadPublicSymbolParams,
  SyncPublicSymbolsStreamOptions,
} from "./client/symbols";

export {
  listCrashes,
  getCrash,
  uploadCrash,
  createCrashEventsSource,
  reprocessCrash,
  submitCrashVerdict,
  pinCrash,
  getCrashArtifactUrl,
} from "./client/crashes";
export type {
  ListCrashesParams,
  UploadCrashParams,
  CrashSSEHandlers,
} from "./client/crashes";

export {
  getLogSummary,
  getLogLines,
  getLogSessionLines,
  getLogAroundTime,
  searchLog,
} from "./client/logs";
export type {
  LogAroundTimeParams,
  LogSessionLinesParams,
  SearchLogParams,
} from "./client/logs";

export {
  listIssues,
  listClusters,
  createIssue,
  getIssue,
  updateIssue,
  mergeIssues,
  splitIssueCluster,
  transitionIssue,
  batchTransitionIssues,
} from "./client/issues";
export type { ListIssuesParams, ListClustersParams } from "./client/issues";

export {
  getHealth,
  getSymSrvBaseUrl,
  getSystemSettings,
  listLLMProviders,
  updateSystemSettings,
  triggerGC,
  getDashboard,
} from "./client/admin";
export type { GetDashboardParams } from "./client/admin";
