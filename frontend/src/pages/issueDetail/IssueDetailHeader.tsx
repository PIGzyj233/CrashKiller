import { Link } from "react-router-dom";
import { ArrowLeft, Layers, Pin } from "lucide-react";
import type { IssueDetailRead } from "../../api/types";
import {
  IssueTransitionSplitButton,
  STATUS_BADGE_CLASSES,
  type TransitionActionConfig,
} from "../../components/IssueStatusWorkflow";
import { useAppI18n } from "../../i18n";

interface IssueDetailHeaderProps {
  numericIssueId: number;
  issue: IssueDetailRead | undefined;
  projectLabel: string | null;
  isTransitionPending: boolean;
  isPinPending: boolean;
  onTransitionAction: (action: TransitionActionConfig) => void;
  onTogglePin: (nextPinned: boolean) => void;
}

export function IssueDetailHeader({
  numericIssueId,
  issue,
  projectLabel,
  isTransitionPending,
  isPinPending,
  onTransitionAction,
  onTogglePin,
}: IssueDetailHeaderProps) {
  const { t, formatDateTime, formatNumber, formatIssueStatus } = useAppI18n();

  return (
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div className="space-y-1.5">
        <div className="flex items-center gap-2 text-xs text-slate-500">
          <Link
            to="/issues"
            className="inline-flex items-center gap-1 font-medium text-indigo-600 hover:underline"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            {t("nav.issues")}
          </Link>
          <span>/</span>
          <span className="font-mono text-slate-700">#{numericIssueId}</span>
        </div>

        <div className="flex flex-wrap items-center gap-2.5">
          <Layers className="h-5 w-5 text-indigo-600" />
          <h1 className="text-xl font-semibold text-slate-900">
            {issue
              ? issue.title
              : t("issueDetail.fallbackTitle", { id: numericIssueId })}
          </h1>
          {issue && (
            <span
              className={`inline-flex whitespace-nowrap rounded-full border px-2.5 py-0.5 text-xs font-medium ${
                STATUS_BADGE_CLASSES[issue.status]
              }`}
            >
              {formatIssueStatus(issue.status)}
            </span>
          )}
          {issue?.fixed_in_version && (
            <span className="inline-flex whitespace-nowrap rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-0.5 font-mono text-xs text-emerald-700">
              {t("issues.fixedVersionBadge", {
                version: issue.fixed_in_version,
              })}
            </span>
          )}
        </div>

        {issue && (
          <div className="flex flex-wrap items-center gap-3 text-xs text-slate-500">
            <span>
              {t("issueDetail.projectMeta", { project: projectLabel ?? "" })}
            </span>
            <span>|</span>
            <span className="font-mono font-medium text-slate-700">
              {t("issueDetail.crashesCount", {
                value: formatNumber(issue.crash_count),
              })}
            </span>
            <span>|</span>
            <span>
              {t("issueDetail.firstSeenMeta", {
                time: formatDateTime(issue.first_seen_at),
              })}
            </span>
            <span>|</span>
            <span>
              {t("issueDetail.lastSeenMeta", {
                time: formatDateTime(issue.last_seen_at),
              })}
            </span>
          </div>
        )}
      </div>

      {issue && (
        <div className="flex flex-wrap items-center gap-2">
          <IssueTransitionSplitButton
            currentStatus={issue.status}
            issueId={issue.id}
            size="md"
            isPending={isTransitionPending}
            onSelectAction={onTransitionAction}
          />
          <button
            type="button"
            disabled={isPinPending}
            onClick={() => onTogglePin(!issue.is_pinned)}
            className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-md border px-3 py-1.5 text-xs font-medium shadow-sm transition-colors ${
              issue.is_pinned
                ? "border-amber-300 bg-amber-50 text-amber-800 hover:bg-amber-100"
                : "border-slate-300 bg-white text-slate-700 hover:bg-slate-50"
            }`}
          >
            <Pin className="h-3.5 w-3.5" />
            {issue.is_pinned
              ? t("issueDetail.pinnedProtected")
              : t("issueDetail.pinIssue")}
          </button>
          <Link
            to={`/crashes?issue_id=${issue.id}`}
            className="whitespace-nowrap rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 shadow-sm hover:bg-slate-50"
          >
            {t("issueDetail.viewAllCrashes")}
          </Link>
        </div>
      )}
    </div>
  );
}
