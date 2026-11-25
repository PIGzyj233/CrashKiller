import { Link } from "react-router-dom";
import {
  ArrowRightLeft,
  ChevronLeft,
  ChevronRight,
  GitMerge,
  Pin,
  Search,
} from "lucide-react";
import type { IssueRead, IssueStatus, ProjectRead } from "../../api/types";
import {
  ISSUE_STATUSES,
  IssueTransitionSplitButton,
  STATUS_BADGE_CLASSES,
  type TransitionActionConfig,
} from "../../components/IssueStatusWorkflow";
import { useAppI18n } from "../../i18n";
import { PAGE_SIZE } from "./utils";

interface IssuesTabPanelProps {
  searchInput: string;
  onSearchInputChange: (value: string) => void;
  statusFilter: "" | IssueStatus;
  onStatusFilterChange: (value: "" | IssueStatus) => void;
  componentInput: string;
  onComponentInputChange: (value: string) => void;
  pinnedOnly: boolean;
  onTogglePinnedOnly: () => void;
  onResetFilters: () => void;
  selectedProjectId: string;
  projectById: Map<string, ProjectRead>;
  issuesList: IssueRead[];
  issuesTotal: number;
  issuesPage: number;
  onPageChange: (nextPage: number) => void;
  isPending: boolean;
  isError: boolean;
  error: unknown;
  onRetry: () => void;
  selectedIssuesMap: Map<number, IssueRead>;
  selectedIssueIds: number[];
  hasCrossProjectSelection: boolean;
  allOnPageSelected: boolean;
  onToggleSelectAllOnPage: () => void;
  onToggleSelectIssue: (issue: IssueRead) => void;
  onClearSelection: () => void;
  onOpenBatchTransitionModal: () => void;
  onOpenMergeModal: () => void;
  isTransitionPending: boolean;
  transitioningIssueId: number | null;
  onRowTransitionAction: (
    issue: IssueRead,
    action: TransitionActionConfig,
  ) => void;
  isPinPending: boolean;
  pinningIssueId: number | null;
  onTogglePin: (issue: IssueRead) => void;
}

export function IssuesTabPanel({
  searchInput,
  onSearchInputChange,
  statusFilter,
  onStatusFilterChange,
  componentInput,
  onComponentInputChange,
  pinnedOnly,
  onTogglePinnedOnly,
  onResetFilters,
  selectedProjectId,
  projectById,
  issuesList,
  issuesTotal,
  issuesPage,
  onPageChange,
  isPending,
  isError,
  error,
  onRetry,
  selectedIssuesMap,
  selectedIssueIds,
  hasCrossProjectSelection,
  allOnPageSelected,
  onToggleSelectAllOnPage,
  onToggleSelectIssue,
  onClearSelection,
  onOpenBatchTransitionModal,
  onOpenMergeModal,
  isTransitionPending,
  transitioningIssueId,
  onRowTransitionAction,
  isPinPending,
  pinningIssueId,
  onTogglePin,
}: IssuesTabPanelProps) {
  const { t, formatDateTime, formatNumber, formatIssueStatus } = useAppI18n();

  const issuesFrom = issuesTotal === 0 ? 0 : issuesPage * PAGE_SIZE + 1;
  const issuesTo = Math.min(issuesTotal, (issuesPage + 1) * PAGE_SIZE);

  return (
    <div className="space-y-4">
      {/* Filter Bar */}
      <div className="flex flex-wrap items-center gap-3 rounded-xl border border-slate-200 bg-white p-3.5 shadow-sm">
        <div className="relative min-w-[220px] flex-1">
          <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
          <input
            type="text"
            value={searchInput}
            onChange={(e) => onSearchInputChange(e.target.value)}
            placeholder={t("issues.searchPlaceholder")}
            className="h-9 w-full rounded-md border border-slate-300 pl-9 pr-3 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
          />
        </div>

        <select
          aria-label={t("issues.filterStatusAria")}
          value={statusFilter}
          onChange={(e) =>
            onStatusFilterChange(e.target.value as "" | IssueStatus)
          }
          className="h-9 rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-800 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
        >
          <option value="">{t("issues.allStatuses")}</option>
          {ISSUE_STATUSES.map((st) => (
            <option key={st} value={st}>
              {formatIssueStatus(st)}
            </option>
          ))}
        </select>

        <input
          type="text"
          value={componentInput}
          onChange={(e) => onComponentInputChange(e.target.value)}
          placeholder={t("issues.componentPlaceholder")}
          aria-label={t("issues.filterComponentAria")}
          className="h-9 w-40 rounded-md border border-slate-300 px-3 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
        />

        <button
          type="button"
          onClick={onTogglePinnedOnly}
          className={`inline-flex h-9 items-center gap-1.5 rounded-md border px-3 text-sm font-medium transition-colors ${
            pinnedOnly
              ? "border-amber-300 bg-amber-50 text-amber-800"
              : "border-slate-300 bg-white text-slate-700 hover:bg-slate-50"
          }`}
        >
          <Pin className="h-3.5 w-3.5" />
          {t("issues.pinnedOnly")}
        </button>

        {(searchInput || statusFilter || componentInput || pinnedOnly) && (
          <button
            type="button"
            onClick={onResetFilters}
            className="h-9 rounded-md px-2.5 text-xs font-medium text-slate-600 hover:bg-slate-100 hover:text-slate-900"
          >
            {t("common.reset")}
          </button>
        )}
      </div>

      {/* Multi-select Batch Action Bar */}
      {selectedIssueIds.length >= 1 && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-indigo-200 bg-indigo-50 px-4 py-2.5 text-sm">
          <div className="flex flex-wrap items-center gap-2 text-indigo-900">
            <ArrowRightLeft className="h-4 w-4 text-indigo-600" />
            <span className="font-medium">
              {t("issues.selectedCount", {
                count: selectedIssueIds.length,
              })}
            </span>
            {selectedIssueIds.length >= 2 && hasCrossProjectSelection && (
              <span className="rounded bg-rose-100 px-2 py-0.5 text-xs font-medium text-rose-700">
                {t("issues.crossProjectWarning")}
              </span>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={onClearSelection}
              className="rounded-md border border-indigo-200 bg-white px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
            >
              {t("common.clear")}
            </button>
            <button
              type="button"
              onClick={onOpenBatchTransitionModal}
              className="inline-flex items-center gap-1.5 rounded-md border border-indigo-300 bg-white px-3 py-1 text-xs font-medium text-indigo-700 hover:bg-indigo-100/60"
            >
              <ArrowRightLeft className="h-3.5 w-3.5" />
              {t("issues.batchTransitionButton")}
            </button>
            {selectedIssueIds.length >= 2 && (
              <button
                type="button"
                disabled={hasCrossProjectSelection}
                onClick={onOpenMergeModal}
                className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1 text-xs font-medium text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <GitMerge className="h-3.5 w-3.5" />
                {t("issues.mergeButton", {
                  count: selectedIssueIds.length,
                })}
              </button>
            )}
          </div>
        </div>
      )}

      {/* Issues Error */}
      {isError && (
        <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          <span>
            {error instanceof Error
              ? error.message
              : t("issues.loadIssuesFailed")}
          </span>
          <button
            type="button"
            onClick={onRetry}
            className="rounded border border-rose-300 bg-white px-2.5 py-1 text-xs font-medium text-rose-700"
          >
            {t("common.retry")}
          </button>
        </div>
      )}

      {/* Issues Table */}
      <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-200 text-left text-sm">
            <thead className="bg-slate-50 text-xs font-medium uppercase tracking-wider text-slate-500">
              <tr>
                <th className="w-10 px-4 py-3">
                  <input
                    type="checkbox"
                    aria-label={t("issues.selectAllAria")}
                    checked={allOnPageSelected}
                    onChange={onToggleSelectAllOnPage}
                    className="h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
                  />
                </th>
                <th className="px-4 py-3">{t("issues.colIssue")}</th>
                <th className="whitespace-nowrap px-4 py-3">
                  {t("issues.colStatus")}
                </th>
                <th className="whitespace-nowrap px-4 py-3">
                  {t("issues.colComponent")}
                </th>
                <th className="whitespace-nowrap px-4 py-3">
                  {t("issues.colCrashes")}
                </th>
                <th className="whitespace-nowrap px-4 py-3">
                  {t("issues.colLastSeen")}
                </th>
                <th className="whitespace-nowrap px-4 py-3 text-right">
                  {t("issues.colActions")}
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {isPending ? (
                <tr>
                  <td
                    colSpan={7}
                    className="px-4 py-10 text-center text-sm text-slate-500"
                  >
                    {t("issues.loadingIssues")}
                  </td>
                </tr>
              ) : issuesList.length === 0 ? (
                <tr>
                  <td
                    colSpan={7}
                    className="px-4 py-12 text-center text-sm text-slate-500"
                  >
                    {t("issues.emptyIssues")}
                  </td>
                </tr>
              ) : (
                issuesList.map((issue: IssueRead) => {
                  const project = projectById.get(issue.project_id);
                  const isSelected = selectedIssuesMap.has(issue.id);
                  return (
                    <tr
                      key={issue.id}
                      className={`hover:bg-slate-50 ${
                        isSelected ? "bg-indigo-50/40" : ""
                      }`}
                    >
                      <td className="px-4 py-3">
                        <input
                          type="checkbox"
                          aria-label={t("issues.selectIssueAria", {
                            id: issue.id,
                          })}
                          checked={isSelected}
                          onChange={() => onToggleSelectIssue(issue)}
                          className="h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
                        />
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex flex-wrap items-center gap-2">
                          <Link
                            to={`/issues/${issue.id}`}
                            className="font-medium text-slate-900 hover:text-indigo-600 hover:underline"
                          >
                            <span className="mr-1.5 font-mono text-xs text-slate-500">
                              #{issue.id}
                            </span>
                            {issue.title}
                          </Link>
                          {!selectedProjectId && project && (
                            <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-600">
                              {project.key}
                            </span>
                          )}
                          {issue.is_ambiguous_stack && (
                            <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-700">
                              {t("issues.ambiguousStackBadge")}
                            </span>
                          )}
                          {issue.fixed_in_version && (
                            <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 font-mono text-[11px] text-emerald-700">
                              {t("issues.fixedVersionBadge", {
                                version: issue.fixed_in_version,
                              })}
                            </span>
                          )}
                        </div>
                        {issue.root_cause && (
                          <p className="mt-0.5 line-clamp-1 text-xs text-slate-500">
                            {issue.root_cause}
                          </p>
                        )}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3">
                        <span
                          className={`inline-flex whitespace-nowrap rounded-full border px-2.5 py-0.5 text-xs font-medium ${
                            STATUS_BADGE_CLASSES[issue.status]
                          }`}
                        >
                          {formatIssueStatus(issue.status)}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-xs text-slate-600">
                        {issue.component || "—"}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 font-mono text-xs font-semibold text-slate-800">
                        {formatNumber(issue.crash_count)}
                      </td>
                      <td
                        className="whitespace-nowrap px-4 py-3 text-xs text-slate-600"
                        title={t("issues.firstSeenTitle", {
                          time: formatDateTime(issue.first_seen_at),
                        })}
                      >
                        {formatDateTime(issue.last_seen_at)}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-right">
                        <div className="inline-flex items-center justify-end gap-1.5">
                          <IssueTransitionSplitButton
                            currentStatus={issue.status}
                            issueId={issue.id}
                            size="sm"
                            isPending={
                              isTransitionPending &&
                              transitioningIssueId === issue.id
                            }
                            onSelectAction={(action) =>
                              onRowTransitionAction(issue, action)
                            }
                          />
                          <button
                            type="button"
                            disabled={
                              isPinPending && pinningIssueId === issue.id
                            }
                            onClick={() => onTogglePin(issue)}
                            title={
                              issue.is_pinned
                                ? t("issues.unpinTitle")
                                : t("issues.pinTitle")
                            }
                            aria-label={
                              issue.is_pinned
                                ? t("issues.unpinAria", { id: issue.id })
                                : t("issues.pinAria", { id: issue.id })
                            }
                            className={`inline-flex items-center gap-1 whitespace-nowrap rounded-md border px-2 py-1 text-xs font-medium transition-colors ${
                              issue.is_pinned
                                ? "border-amber-300 bg-amber-50 text-amber-800 hover:bg-amber-100"
                                : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
                            }`}
                          >
                            <Pin className="h-3.5 w-3.5" />
                            {issue.is_pinned
                              ? t("issues.pinned")
                              : t("issues.pin")}
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>

        {/* Pagination Footer */}
        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 bg-slate-50 px-4 py-3 text-xs text-slate-600">
          <span>
            {t("common.showingRange", {
              from: issuesFrom,
              to: issuesTo,
              total: formatNumber(issuesTotal),
            })}
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={issuesPage === 0}
              onClick={() => onPageChange(Math.max(0, issuesPage - 1))}
              className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1 font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
              {t("common.previous")}
            </button>
            <button
              type="button"
              disabled={(issuesPage + 1) * PAGE_SIZE >= issuesTotal}
              onClick={() => onPageChange(issuesPage + 1)}
              className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1 font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {t("common.next")}
              <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
