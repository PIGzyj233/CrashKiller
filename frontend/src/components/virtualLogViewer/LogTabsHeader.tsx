import { FileText, PanelRightClose, PanelRightOpen } from "lucide-react";
import type { CrashLogRead } from "../../api/types";
import { useAppI18n } from "../../i18n";
import { formatBytes } from "./utils";

export interface LogTabsHeaderProps {
  logs: CrashLogRead[];
  resolvedActiveLogId: string;
  sidebarOpen: boolean;
  onSelectLog: (logId: string) => void;
  onToggleSidebar: () => void;
}

export function LogTabsHeader({
  logs,
  resolvedActiveLogId,
  sidebarOpen,
  onSelectLog,
  onToggleSidebar,
}: LogTabsHeaderProps) {
  const { t, formatNumber } = useAppI18n();

  return (
    <div className="ck-scrollbar flex items-center justify-between gap-2 overflow-x-auto border-b border-slate-200 bg-slate-50 px-3 py-2">
      <div className="flex items-center gap-1.5" role="tablist">
        {logs.map((log) => {
          const isActive = log.id === resolvedActiveLogId;
          const errCount = log.digest_json?.error_count ?? 0;
          const sessMatches = log.digest_json?.session?.match_count ?? 0;
          return (
            <button
              key={log.id}
              type="button"
              role="tab"
              aria-selected={isActive}
              onClick={() => onSelectLog(log.id)}
              className={`inline-flex shrink-0 items-center gap-2 rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors ${
                isActive
                  ? "border-indigo-300 bg-white text-indigo-700 shadow-sm"
                  : "border-transparent bg-transparent text-slate-600 hover:bg-slate-200/60 hover:text-slate-900"
              }`}
            >
              <FileText className="h-3.5 w-3.5 shrink-0" />
              <span className="max-w-[180px] truncate">{log.filename}</span>
              <span className="font-mono text-[11px] text-slate-400">
                {t("log.linesShort", { lines: formatNumber(log.total_lines) })}{" "}
                · {formatBytes(log.raw_size)}
              </span>
              {sessMatches > 0 && (
                <span className="rounded-full bg-emerald-100 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-emerald-700">
                  {t("log.sessionHitsShort", {
                    count: sessMatches,
                  })}
                </span>
              )}
              {errCount > 0 && (
                <span className="rounded-full bg-rose-100 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-rose-700">
                  {t("log.errorsShort", { count: errCount })}
                </span>
              )}
            </button>
          );
        })}
      </div>

      <button
        type="button"
        onClick={onToggleSidebar}
        className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-slate-300 bg-white px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
        title={
          sidebarOpen ? t("log.hideDigestTitle") : t("log.showDigestTitle")
        }
      >
        {sidebarOpen ? (
          <PanelRightClose className="h-3.5 w-3.5" />
        ) : (
          <PanelRightOpen className="h-3.5 w-3.5" />
        )}
        <span className="hidden sm:inline">{t("log.digestButton")}</span>
      </button>
    </div>
  );
}
