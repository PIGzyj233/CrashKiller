import { Link } from "react-router-dom";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  FileCode,
  Info,
} from "lucide-react";
import type { MissingSymbolInfo } from "../../api/types";
import { useAppI18n } from "../../i18n";
import type { CrashingMissingInfo } from "./types";

export interface MissingSymbolsBannerProps {
  consolidatedMissing: MissingSymbolInfo[];
  hasParsedThreads: boolean;
  crashingMissingInfo: CrashingMissingInfo;
  expanded: boolean;
  onToggleExpanded: () => void;
}

export function MissingSymbolsBanner({
  consolidatedMissing,
  hasParsedThreads,
  crashingMissingInfo,
  expanded,
  onToggleExpanded,
}: MissingSymbolsBannerProps) {
  const { t } = useAppI18n();

  if (consolidatedMissing.length === 0) {
    return null;
  }

  const affectedFrames = crashingMissingInfo.affectedFrameCount;
  const affectsCrashingStack = !hasParsedThreads || affectedFrames > 0;

  const bannerTitle = !hasParsedThreads
    ? consolidatedMissing.length === 1
      ? t("stack.missingBannerTitleOne")
      : t("stack.missingBannerTitleOther", {
          count: consolidatedMissing.length,
        })
    : affectedFrames > 0
      ? consolidatedMissing.length === 1
        ? t("stack.missingBannerAffectedOne", { affected: affectedFrames })
        : t("stack.missingBannerAffectedOther", {
            count: consolidatedMissing.length,
            affected: affectedFrames,
          })
      : consolidatedMissing.length === 1
        ? t("stack.missingBannerUnaffectedOne")
        : t("stack.missingBannerUnaffectedOther", {
            count: consolidatedMissing.length,
          });

  return (
    <div
      className={
        affectsCrashingStack
          ? "border-b border-amber-200 bg-amber-50 px-4 py-2.5 text-amber-950"
          : "border-b border-slate-200 bg-slate-50/75 px-4 py-2 text-slate-600"
      }
      role={affectsCrashingStack ? "alert" : undefined}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <button
          type="button"
          onClick={onToggleExpanded}
          aria-expanded={expanded}
          className={`flex items-center gap-2 text-left text-xs ${
            affectsCrashingStack
              ? "font-semibold text-amber-900 hover:text-amber-950"
              : "font-medium text-slate-600 hover:text-slate-900"
          }`}
        >
          {expanded ? (
            <ChevronDown className="h-3.5 w-3.5 shrink-0" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5 shrink-0" />
          )}
          {affectsCrashingStack ? (
            <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-amber-600" />
          ) : (
            <Info className="h-3.5 w-3.5 shrink-0 text-slate-400" />
          )}
          <span>{bannerTitle}</span>
          <span
            className={`ml-1 rounded px-1.5 py-0.5 text-[11px] font-normal ${
              affectsCrashingStack
                ? "bg-amber-100/80 text-amber-800"
                : "bg-slate-200/70 text-slate-600"
            }`}
          >
            {expanded
              ? t("stack.missingBannerCollapse")
              : t("stack.missingBannerExpand")}
          </span>
        </button>
        <Link
          to="/symbols"
          className={`inline-flex items-center gap-1 rounded-md border px-2.5 py-1 text-xs font-medium shadow-sm ${
            affectsCrashingStack
              ? "border-amber-300 bg-white text-amber-900 hover:bg-amber-100"
              : "border-slate-200 bg-white text-slate-600 hover:bg-slate-100 hover:text-slate-900"
          }`}
        >
          <FileCode className="h-3.5 w-3.5" />
          {t("stack.uploadSymbols")}
        </Link>
      </div>

      {expanded && (
        <div className="ck-scrollbar mt-2.5 grid max-h-72 gap-1.5 overflow-y-auto pr-1 sm:grid-cols-2">
          {crashingMissingInfo.sortedMissing.map((item) => {
            const onCrashingStack =
              crashingMissingInfo.isItemOnCrashingStack(item);
            return (
              <div
                key={`${item.debug_file}::${item.debug_id}`}
                className={`flex flex-col rounded-md border px-2.5 py-1.5 text-xs ${
                  onCrashingStack
                    ? "border-amber-300 bg-amber-50/70"
                    : affectsCrashingStack
                      ? "border-amber-200/80 bg-white/80"
                      : "border-slate-200 bg-white"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="flex min-w-0 items-center gap-1.5">
                    <span className="truncate font-semibold text-slate-900">
                      {item.module_name || item.debug_file}
                    </span>
                    {onCrashingStack && (
                      <span className="shrink-0 rounded border border-amber-300 bg-amber-100 px-1 py-0.5 text-[10px] font-semibold text-amber-900">
                        {t("stack.missingBannerCrashingTag")}
                      </span>
                    )}
                  </div>
                  <span
                    className={`shrink-0 font-mono text-[11px] ${
                      affectsCrashingStack
                        ? "text-amber-800"
                        : "text-slate-500"
                    }`}
                  >
                    {item.debug_file}
                  </span>
                </div>
                <div className="mt-0.5 truncate font-mono text-[11px] text-slate-600">
                  {t("stack.debugIdLabel", { id: item.debug_id })}
                </div>
                {(item.code_file || item.code_id) && (
                  <div className="mt-0.5 truncate font-mono text-[11px] text-slate-500">
                    {t("stack.codeLabel", { file: item.code_file ?? "-" })}
                    {item.code_id ? ` (${item.code_id})` : ""}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
