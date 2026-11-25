import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  ArrowRight,
  Ban,
  Check,
  CheckCircle2,
  ChevronDown,
  History,
  Loader2,
  RotateCcw,
  X,
} from "lucide-react";
import type {
  IssueRead,
  IssueStatus,
  IssueStatusEventRead,
  IssueStatusEventSource,
} from "../api/types";
import { useAppI18n } from "../i18n";

export const ISSUE_STATUSES: readonly IssueStatus[] = [
  "NEW",
  "KNOWN",
  "FIXED",
  "WONTFIX",
  "REGRESSION",
];

export const STATUS_BADGE_CLASSES: Record<IssueStatus, string> = {
  NEW: "border-sky-200 bg-sky-50 text-sky-700",
  KNOWN: "border-indigo-200 bg-indigo-50 text-indigo-700",
  FIXED: "border-emerald-200 bg-emerald-50 text-emerald-700",
  WONTFIX: "border-slate-200 bg-slate-100 text-slate-700",
  REGRESSION: "border-rose-200 bg-rose-50 text-rose-700",
};

const STATUS_DOT_CLASSES: Record<IssueStatus, string> = {
  NEW: "bg-sky-500",
  KNOWN: "bg-indigo-500",
  FIXED: "bg-emerald-500",
  WONTFIX: "bg-slate-400",
  REGRESSION: "bg-rose-500",
};

export type TransitionActionKey =
  | "confirm"
  | "markFixed"
  | "markRegression"
  | "markWontfix"
  | "reopen"
  | "markKnown"
  | "resetNew";

export interface TransitionActionConfig {
  toStatus: IssueStatus;
  actionKey: TransitionActionKey;
  requiresModal: boolean;
  tone: "indigo" | "emerald" | "rose" | "slate";
}

export function getPrimaryTransition(
  current: IssueStatus,
): TransitionActionConfig {
  switch (current) {
    case "NEW":
      return {
        toStatus: "KNOWN",
        actionKey: "confirm",
        requiresModal: false,
        tone: "indigo",
      };
    case "KNOWN":
      return {
        toStatus: "FIXED",
        actionKey: "markFixed",
        requiresModal: true,
        tone: "emerald",
      };
    case "REGRESSION":
      return {
        toStatus: "FIXED",
        actionKey: "markFixed",
        requiresModal: true,
        tone: "emerald",
      };
    case "FIXED":
      return {
        toStatus: "REGRESSION",
        actionKey: "markRegression",
        requiresModal: true,
        tone: "rose",
      };
    case "WONTFIX":
      return {
        toStatus: "KNOWN",
        actionKey: "reopen",
        requiresModal: false,
        tone: "indigo",
      };
  }
}

export function getSecondaryTransitions(
  current: IssueStatus,
): TransitionActionConfig[] {
  switch (current) {
    case "NEW":
      return [
        {
          toStatus: "FIXED",
          actionKey: "markFixed",
          requiresModal: true,
          tone: "emerald",
        },
        {
          toStatus: "WONTFIX",
          actionKey: "markWontfix",
          requiresModal: true,
          tone: "slate",
        },
      ];
    case "KNOWN":
      return [
        {
          toStatus: "WONTFIX",
          actionKey: "markWontfix",
          requiresModal: true,
          tone: "slate",
        },
        {
          toStatus: "NEW",
          actionKey: "resetNew",
          requiresModal: true,
          tone: "slate",
        },
      ];
    case "REGRESSION":
      return [
        {
          toStatus: "KNOWN",
          actionKey: "markKnown",
          requiresModal: true,
          tone: "indigo",
        },
        {
          toStatus: "WONTFIX",
          actionKey: "markWontfix",
          requiresModal: true,
          tone: "slate",
        },
      ];
    case "FIXED":
      return [
        {
          toStatus: "KNOWN",
          actionKey: "reopen",
          requiresModal: true,
          tone: "indigo",
        },
        {
          toStatus: "WONTFIX",
          actionKey: "markWontfix",
          requiresModal: true,
          tone: "slate",
        },
      ];
    case "WONTFIX":
      return [
        {
          toStatus: "FIXED",
          actionKey: "markFixed",
          requiresModal: true,
          tone: "emerald",
        },
      ];
  }
}

function renderActionIcon(
  actionKey: TransitionActionKey,
  className = "h-3.5 w-3.5",
) {
  switch (actionKey) {
    case "confirm":
    case "markKnown":
      return <Check className={className} />;
    case "markFixed":
      return <CheckCircle2 className={className} />;
    case "markRegression":
      return <AlertTriangle className={className} />;
    case "markWontfix":
      return <Ban className={className} />;
    case "reopen":
    case "resetNew":
      return <RotateCcw className={className} />;
  }
}

const SPLIT_TONE_CLASSES: Record<
  TransitionActionConfig["tone"],
  { main: string; caret: string; divider: string }
> = {
  indigo: {
    main: "border-indigo-200 bg-indigo-50 text-indigo-700 hover:bg-indigo-100",
    caret: "border-indigo-200 bg-indigo-50 text-indigo-700 hover:bg-indigo-100",
    divider: "border-l-indigo-200",
  },
  emerald: {
    main: "border-emerald-200 bg-emerald-50 text-emerald-700 hover:bg-emerald-100",
    caret:
      "border-emerald-200 bg-emerald-50 text-emerald-700 hover:bg-emerald-100",
    divider: "border-l-emerald-200",
  },
  rose: {
    main: "border-rose-200 bg-rose-50 text-rose-700 hover:bg-rose-100",
    caret: "border-rose-200 bg-rose-50 text-rose-700 hover:bg-rose-100",
    divider: "border-l-rose-200",
  },
  slate: {
    main: "border-slate-300 bg-white text-slate-700 hover:bg-slate-50",
    caret: "border-slate-300 bg-white text-slate-700 hover:bg-slate-50",
    divider: "border-l-slate-200",
  },
};

export interface IssueTransitionSplitButtonProps {
  currentStatus: IssueStatus;
  issueId?: number;
  disabled?: boolean;
  isPending?: boolean;
  size?: "sm" | "md";
  onSelectAction: (action: TransitionActionConfig) => void;
}

export function IssueTransitionSplitButton({
  currentStatus,
  issueId,
  disabled = false,
  isPending = false,
  size = "sm",
  onSelectAction,
}: IssueTransitionSplitButtonProps) {
  const { t, formatIssueStatus } = useAppI18n();
  const [menuOpen, setMenuOpen] = useState(false);
  const [menuPosition, setMenuPosition] = useState<{
    top: number;
    left: number;
  } | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  const primary = useMemo(
    () => getPrimaryTransition(currentStatus),
    [currentStatus],
  );
  const secondary = useMemo(
    () => getSecondaryTransitions(currentStatus),
    [currentStatus],
  );

  const toneClasses = SPLIT_TONE_CLASSES[primary.tone];
  const isDisabled = disabled || isPending;

  const updatePosition = () => {
    const el = containerRef.current;
    if (!el) {
      return;
    }
    const rect = el.getBoundingClientRect();
    const menuWidth = 224;
    const estimatedHeight = secondary.length * 40 + 44;
    const openUpward =
      window.innerHeight - rect.bottom < estimatedHeight &&
      rect.top > estimatedHeight;
    const top = openUpward
      ? Math.max(8, rect.top - estimatedHeight - 4)
      : rect.bottom + 4;
    const left = Math.max(
      8,
      Math.min(rect.right - menuWidth, window.innerWidth - menuWidth - 8),
    );
    setMenuPosition({ top, left });
  };

  useEffect(() => {
    if (!menuOpen) {
      return;
    }
    updatePosition();

    const handleClickOutside = (event: MouseEvent) => {
      const target = event.target as Node;
      if (
        containerRef.current?.contains(target) ||
        menuRef.current?.contains(target)
      ) {
        return;
      }
      setMenuOpen(false);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setMenuOpen(false);
      }
    };
    const handleReposition = () => setMenuOpen(false);

    document.addEventListener("mousedown", handleClickOutside);
    document.addEventListener("keydown", handleKeyDown);
    window.addEventListener("scroll", handleReposition, true);
    window.addEventListener("resize", handleReposition);
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
      document.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("scroll", handleReposition, true);
      window.removeEventListener("resize", handleReposition);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [menuOpen, secondary.length]);

  const padMain = size === "md" ? "px-3 py-1.5 text-xs" : "px-2.5 py-1 text-xs";
  const padCaret = size === "md" ? "px-2 py-1.5" : "px-1.5 py-1";

  return (
    <div ref={containerRef} className="inline-flex shrink-0 items-stretch">
      <button
        type="button"
        disabled={isDisabled}
        onClick={() => {
          setMenuOpen(false);
          onSelectAction(primary);
        }}
        title={t(`issueWorkflow.actions.${primary.actionKey}`)}
        className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-l-md border font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${padMain} ${toneClasses.main}`}
      >
        {isPending ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
        ) : (
          renderActionIcon(primary.actionKey)
        )}
        <span>{t(`issueWorkflow.actions.${primary.actionKey}`)}</span>
      </button>
      <button
        type="button"
        disabled={isDisabled}
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        aria-label={
          issueId !== undefined
            ? t("issueWorkflow.moreActionsAria", { id: issueId })
            : t("issueWorkflow.moreActionsGenericAria")
        }
        onClick={() => setMenuOpen((prev) => !prev)}
        className={`inline-flex items-center justify-center rounded-r-md border border-l-0 transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${padCaret} ${toneClasses.caret} ${toneClasses.divider}`}
      >
        <ChevronDown className="h-3.5 w-3.5" />
      </button>

      {menuOpen &&
        menuPosition &&
        createPortal(
          <div
            ref={menuRef}
            role="menu"
            style={{ top: menuPosition.top, left: menuPosition.left }}
            className="fixed z-50 w-56 rounded-lg border border-slate-200 bg-white py-1 shadow-lg"
          >
            <div className="border-b border-slate-100 px-3 py-1.5 text-[11px] font-medium text-slate-400">
              {t("issueWorkflow.switchStatusHeading")}
            </div>
            {secondary.map((option) => (
              <button
                key={option.toStatus}
                type="button"
                role="menuitem"
                onClick={() => {
                  setMenuOpen(false);
                  onSelectAction(option);
                }}
                className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-xs text-slate-700 hover:bg-slate-50"
              >
                <span className="inline-flex items-center gap-2 font-medium">
                  <span
                    className={`h-2 w-2 rounded-full ${STATUS_DOT_CLASSES[option.toStatus]}`}
                  />
                  {t(`issueWorkflow.actions.${option.actionKey}`)}
                </span>
                <span
                  className={`inline-flex whitespace-nowrap rounded-full border px-2 py-0.5 text-[10px] font-medium ${STATUS_BADGE_CLASSES[option.toStatus]}`}
                >
                  {formatIssueStatus(option.toStatus)}
                </span>
              </button>
            ))}
          </div>,
          document.body,
        )}
    </div>
  );
}

export interface IssueTransitionModalSubmitPayload {
  to_status: IssueStatus;
  fixed_in_version?: string | null;
  clear_fixed_in_version?: boolean;
  note?: string | null;
}

export interface IssueTransitionModalProps {
  isOpen: boolean;
  mode: "single" | "batch";
  issue?: IssueRead | null;
  selectedIssues?: IssueRead[];
  initialTargetStatus: IssueStatus;
  isPending: boolean;
  errorMessage?: string | null;
  onClose: () => void;
  onSubmit: (payload: IssueTransitionModalSubmitPayload) => void;
}

export function IssueTransitionModal({
  isOpen,
  mode,
  issue,
  selectedIssues = [],
  initialTargetStatus,
  isPending,
  errorMessage,
  onClose,
  onSubmit,
}: IssueTransitionModalProps) {
  const { t, formatIssueStatus } = useAppI18n();
  const [targetStatus, setTargetStatus] =
    useState<IssueStatus>(initialTargetStatus);
  const [fixedInVersion, setFixedInVersion] = useState("");
  const [clearFixedVersion, setClearFixedVersion] = useState(false);
  const [note, setNote] = useState("");

  useEffect(() => {
    if (!isOpen) {
      return;
    }
    setTargetStatus(initialTargetStatus);
    setFixedInVersion(mode === "single" ? (issue?.fixed_in_version ?? "") : "");
    setClearFixedVersion(false);
    setNote("");
  }, [isOpen, initialTargetStatus, mode, issue?.id, issue?.fixed_in_version]);

  useEffect(() => {
    if (!isOpen) {
      return;
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !isPending) {
        onClose();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [isOpen, isPending, onClose]);

  const selectableStatuses = useMemo(() => {
    if (mode === "single" && issue) {
      return ISSUE_STATUSES.filter((st) => st !== issue.status);
    }
    return ISSUE_STATUSES;
  }, [mode, issue]);

  const batchSummary = useMemo(() => {
    if (mode !== "batch") {
      return { willUpdate: 0, willSkip: 0 };
    }
    let willUpdate = 0;
    let willSkip = 0;
    for (const item of selectedIssues) {
      if (item.status === targetStatus) {
        willSkip += 1;
      } else {
        willUpdate += 1;
      }
    }
    return { willUpdate, willSkip };
  }, [mode, selectedIssues, targetStatus]);

  if (!isOpen) {
    return null;
  }

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    const cleanedVersion = fixedInVersion.trim();
    const cleanedNote = note.trim();
    const payload: IssueTransitionModalSubmitPayload = {
      to_status: targetStatus,
      note: cleanedNote || null,
    };
    if (targetStatus === "FIXED") {
      if (cleanedVersion) {
        payload.fixed_in_version = cleanedVersion;
      }
    } else if (clearFixedVersion) {
      payload.clear_fixed_in_version = true;
    }
    onSubmit(payload);
  };

  const canSubmit =
    !isPending &&
    (mode === "single"
      ? Boolean(issue && targetStatus !== issue.status)
      : batchSummary.willUpdate > 0);

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/45 p-4">
      <div className="w-full max-w-lg rounded-xl border border-slate-200 bg-white shadow-xl">
        <div className="flex items-center justify-between border-b border-slate-200 px-5 py-4">
          <h3 className="text-base font-semibold text-slate-900">
            {mode === "batch"
              ? t("issueWorkflow.modalBatchTitle", {
                  count: selectedIssues.length,
                })
              : t("issueWorkflow.modalSingleTitle", {
                  id: issue?.id ?? "",
                })}
          </h3>
          <button
            type="button"
            disabled={isPending}
            onClick={onClose}
            aria-label={t("common.close")}
            className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700 disabled:opacity-50"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4 px-5 py-4">
          {mode === "single" && issue && (
            <div className="rounded-lg border border-slate-200 bg-slate-50 px-3.5 py-2.5">
              <div className="line-clamp-2 text-xs font-medium text-slate-800">
                <span className="mr-1.5 font-mono text-slate-500">
                  #{issue.id}
                </span>
                {issue.title}
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
                <span
                  className={`inline-flex whitespace-nowrap rounded-full border px-2.5 py-0.5 text-xs font-medium ${STATUS_BADGE_CLASSES[issue.status]}`}
                >
                  {formatIssueStatus(issue.status)}
                </span>
                <ArrowRight className="h-3.5 w-3.5 text-slate-400" />
                <span
                  className={`inline-flex whitespace-nowrap rounded-full border px-2.5 py-0.5 text-xs font-medium ${STATUS_BADGE_CLASSES[targetStatus]}`}
                >
                  {formatIssueStatus(targetStatus)}
                </span>
              </div>
            </div>
          )}

          {mode === "batch" && (
            <div className="rounded-lg border border-indigo-100 bg-indigo-50/70 px-3.5 py-2.5 text-xs text-indigo-900">
              {t("issueWorkflow.modalBatchSummary", {
                total: selectedIssues.length,
                willUpdate: batchSummary.willUpdate,
                willSkip: batchSummary.willSkip,
              })}
            </div>
          )}

          {errorMessage && (
            <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
              {errorMessage}
            </div>
          )}

          <div>
            <label className="block text-xs font-medium text-slate-700">
              {t("issueWorkflow.targetStatusLabel")}
            </label>
            <div className="mt-1.5 flex flex-wrap gap-2">
              {selectableStatuses.map((st) => {
                const active = targetStatus === st;
                return (
                  <button
                    key={st}
                    type="button"
                    onClick={() => setTargetStatus(st)}
                    className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-3 py-1 text-xs font-medium transition-all ${
                      active
                        ? `${STATUS_BADGE_CLASSES[st]} ring-2 ring-indigo-500/30`
                        : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
                    }`}
                  >
                    <span
                      className={`h-2 w-2 rounded-full ${STATUS_DOT_CLASSES[st]}`}
                    />
                    {formatIssueStatus(st)}
                  </button>
                );
              })}
            </div>
          </div>

          {targetStatus === "FIXED" && (
            <div className="rounded-lg border border-emerald-200 bg-emerald-50/50 p-3">
              <label className="block text-xs font-medium text-emerald-900">
                {t("issueWorkflow.fixedInVersionLabel")}
              </label>
              <input
                type="text"
                value={fixedInVersion}
                onChange={(e) => setFixedInVersion(e.target.value)}
                placeholder={t("issueWorkflow.fixedInVersionPlaceholder")}
                className="mt-1.5 h-9 w-full rounded-md border border-emerald-300 bg-white px-3 font-mono text-sm text-slate-900 placeholder:text-slate-400 focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500"
              />
              <p className="mt-1.5 text-[11px] leading-relaxed text-emerald-800">
                {t("issueWorkflow.fixedInVersionHint")}
              </p>
            </div>
          )}

          {targetStatus !== "FIXED" &&
            mode === "single" &&
            issue?.fixed_in_version && (
              <label className="flex items-center gap-2 text-xs text-slate-600">
                <input
                  type="checkbox"
                  checked={clearFixedVersion}
                  onChange={(e) => setClearFixedVersion(e.target.checked)}
                  className="h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
                />
                <span>
                  {t("issueWorkflow.clearFixedVersionCheckbox", {
                    version: issue.fixed_in_version,
                  })}
                </span>
              </label>
            )}

          <div>
            <label className="block text-xs font-medium text-slate-700">
              {t("issueWorkflow.noteLabel")}
            </label>
            <textarea
              rows={3}
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder={t("issueWorkflow.notePlaceholder")}
              className="mt-1 w-full rounded-md border border-slate-300 p-2.5 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />
          </div>

          <div className="flex items-center justify-end gap-2 border-t border-slate-100 pt-3">
            <button
              type="button"
              disabled={isPending}
              onClick={onClose}
              className="rounded-md border border-slate-300 bg-white px-3.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            >
              {t("common.cancel")}
            </button>
            <button
              type="submit"
              disabled={!canSubmit}
              className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-4 py-1.5 text-xs font-medium text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              {isPending
                ? t("issueWorkflow.submitting")
                : t("issueWorkflow.confirmTransition")}
            </button>
          </div>
        </form>
      </div>
    </div>,
    document.body,
  );
}

const SOURCE_BADGE_CLASSES: Record<IssueStatusEventSource, string> = {
  manual: "border-indigo-200 bg-indigo-50 text-indigo-700",
  batch: "border-violet-200 bg-violet-50 text-violet-700",
  form_edit: "border-slate-200 bg-slate-100 text-slate-700",
  ai_triage: "border-sky-200 bg-sky-50 text-sky-700",
  ai_regression: "border-rose-200 bg-rose-50 text-rose-700",
  human_verdict: "border-amber-200 bg-amber-50 text-amber-800",
  split: "border-teal-200 bg-teal-50 text-teal-700",
};

export interface IssueStatusHistoryCardProps {
  events: IssueStatusEventRead[];
}

export function IssueStatusHistoryCard({
  events,
}: IssueStatusHistoryCardProps) {
  const { t, formatDateTime, formatIssueStatus } = useAppI18n();

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <History className="h-4 w-4 text-indigo-600" />
          <h2 className="text-sm font-semibold text-slate-900">
            {t("issueWorkflow.historyTitle", { count: events.length })}
          </h2>
        </div>
      </div>

      {events.length === 0 ? (
        <p className="text-xs text-slate-500">
          {t("issueWorkflow.historyEmpty")}
        </p>
      ) : (
        <ol className="relative space-y-4 border-l border-slate-200 pl-4">
          {events.map((ev) => (
            <li key={ev.id} className="relative">
              <span
                className={`absolute -left-[21px] top-1.5 h-2.5 w-2.5 rounded-full ring-4 ring-white ${STATUS_DOT_CLASSES[ev.to_status]}`}
              />
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-1.5">
                  {ev.from_status ? (
                    <>
                      <span
                        className={`inline-flex whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-medium ${STATUS_BADGE_CLASSES[ev.from_status]}`}
                      >
                        {formatIssueStatus(ev.from_status)}
                      </span>
                      <ArrowRight className="h-3 w-3 text-slate-400" />
                    </>
                  ) : (
                    <span className="text-[11px] font-medium text-slate-500">
                      {t("issueWorkflow.createdPrefix")}
                    </span>
                  )}
                  <span
                    className={`inline-flex whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-medium ${STATUS_BADGE_CLASSES[ev.to_status]}`}
                  >
                    {formatIssueStatus(ev.to_status)}
                  </span>
                  <span
                    className={`inline-flex whitespace-nowrap rounded border px-1.5 py-0.5 text-[10px] font-medium ${SOURCE_BADGE_CLASSES[ev.source]}`}
                  >
                    {t(`issueWorkflow.sources.${ev.source}`)}
                  </span>
                </div>
                <span className="text-[11px] text-slate-400">
                  {formatDateTime(ev.created_at)}
                </span>
              </div>

              {(ev.fixed_in_version || ev.crash_id || ev.note) && (
                <div className="mt-1.5 space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    {ev.fixed_in_version && (
                      <span className="inline-flex whitespace-nowrap rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 font-mono text-[11px] text-emerald-700">
                        {t("issues.fixedVersionBadge", {
                          version: ev.fixed_in_version,
                        })}
                      </span>
                    )}
                    {ev.crash_id && (
                      <Link
                        to={`/crashes/${ev.crash_id}`}
                        className="inline-flex items-center font-mono text-[11px] font-medium text-indigo-600 hover:underline"
                      >
                        {t("issueWorkflow.relatedCrash", {
                          id: ev.crash_id.slice(0, 8),
                        })}
                      </Link>
                    )}
                  </div>
                  {ev.note && (
                    <p className="rounded-md bg-slate-50 px-2.5 py-1.5 text-xs text-slate-600">
                      {ev.note}
                    </p>
                  )}
                </div>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
