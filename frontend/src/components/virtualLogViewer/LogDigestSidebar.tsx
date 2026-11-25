import type { LogDigest, LogSessionDigest } from "../../api/types";
import { useAppI18n } from "../../i18n";
import type { SidebarTab } from "./types";
import { getDigestEntryLine, getDigestLevelBadgeClass } from "./utils";

export interface LogDigestSidebarProps {
  activeDigest: LogDigest | null;
  activeSessionDigest: LogSessionDigest | null;
  sidebarTab: SidebarTab;
  hasGcError: boolean;
  onSelectTab: (tab: SidebarTab) => void;
  onJumpToLine: (lineNumber: number) => void;
}

export function LogDigestSidebar({
  activeDigest,
  activeSessionDigest,
  sidebarTab,
  hasGcError,
  onSelectTab,
  onJumpToLine,
}: LogDigestSidebarProps) {
  const { t } = useAppI18n();

  const recentErrors = activeDigest?.recent_errors ?? [];
  const tailLines = activeDigest?.tail_lines ?? [];
  const sessionRecentErrors = activeSessionDigest?.recent_errors ?? [];
  const sessionTailLines = activeSessionDigest?.tail_lines ?? [];
  const postSessionLines = activeSessionDigest?.post_session_lines ?? [];

  return (
    <aside className="flex h-[520px] w-full shrink-0 flex-col border-t border-slate-200 bg-slate-900 text-slate-100 lg:w-80 lg:border-l lg:border-t-0">
      <div className="space-y-1.5 border-b border-slate-800 px-3 py-2">
        <div className="flex flex-wrap items-center gap-1">
          {activeSessionDigest && (
            <>
              <button
                type="button"
                onClick={() => onSelectTab("session_errors")}
                className={`rounded-md px-2 py-1 text-[11px] font-medium transition-colors ${
                  sidebarTab === "session_errors"
                    ? "bg-emerald-700/80 text-white"
                    : "text-emerald-300/80 hover:text-emerald-200"
                }`}
              >
                {t("log.tabSessionErrors", {
                  count: sessionRecentErrors.length,
                })}
              </button>
              <button
                type="button"
                onClick={() => onSelectTab("session_tail")}
                className={`rounded-md px-2 py-1 text-[11px] font-medium transition-colors ${
                  sidebarTab === "session_tail"
                    ? "bg-emerald-700/80 text-white"
                    : "text-emerald-300/80 hover:text-emerald-200"
                }`}
              >
                {t("log.tabSessionTail", {
                  count: sessionTailLines.length,
                })}
              </button>
            </>
          )}
          <button
            type="button"
            onClick={() => onSelectTab("errors")}
            className={`rounded-md px-2 py-1 text-[11px] font-medium transition-colors ${
              sidebarTab === "errors"
                ? "bg-slate-800 text-white"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            {t("log.tabErrors", { count: recentErrors.length })}
          </button>
          <button
            type="button"
            onClick={() => onSelectTab("tail")}
            className={`rounded-md px-2 py-1 text-[11px] font-medium transition-colors ${
              sidebarTab === "tail"
                ? "bg-slate-800 text-white"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            {t("log.tabTail", { count: tailLines.length })}
          </button>
        </div>
        {activeDigest && (
          <div className="flex items-center justify-between font-mono text-[10px] text-slate-400">
            <span>
              All: {activeDigest.error_count ?? 0}E /{" "}
              {activeDigest.warn_count ?? 0}W
            </span>
            {activeSessionDigest && (
              <span className="text-emerald-400">
                Sess: {activeSessionDigest.error_count}E /{" "}
                {activeSessionDigest.warn_count}W
              </span>
            )}
          </div>
        )}
      </div>

      <div className="ck-scrollbar flex-1 divide-y divide-slate-800/80 overflow-y-auto">
        {sidebarTab === "session_errors" ? (
          sessionRecentErrors.length === 0 ? (
            <div className="px-4 py-8 text-center text-xs text-slate-400">
              {t("log.noSessionDigestErrors")}
            </div>
          ) : (
            sessionRecentErrors.map((entry, idx) => {
              const lineNo = getDigestEntryLine(entry);
              const canJump = lineNo !== null && !hasGcError;
              return (
                <button
                  key={`serr-${lineNo ?? idx}-${idx}`}
                  type="button"
                  disabled={!canJump}
                  onClick={() => {
                    if (lineNo !== null) {
                      onJumpToLine(lineNo);
                    }
                  }}
                  className="w-full px-3 py-2 text-left transition-colors hover:bg-slate-800/70 disabled:cursor-default disabled:hover:bg-transparent"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span
                      className={`rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase ${getDigestLevelBadgeClass(
                        entry.level,
                      )}`}
                    >
                      {entry.level || "ERROR"}
                    </span>
                    <span className="font-mono text-[11px] text-emerald-300">
                      {lineNo !== null ? `L${lineNo}` : "-"}
                    </span>
                  </div>
                  {entry.timestamp && (
                    <div className="mt-1 font-mono text-[10px] text-slate-400">
                      {entry.timestamp}
                    </div>
                  )}
                  <p className="mt-1 line-clamp-2 break-all font-mono text-[11px] text-slate-200">
                    {entry.text}
                  </p>
                </button>
              );
            })
          )
        ) : sidebarTab === "session_tail" ? (
          sessionTailLines.length === 0 && postSessionLines.length === 0 ? (
            <div className="px-4 py-8 text-center text-xs text-slate-400">
              {t("log.noSessionDigestTail")}
            </div>
          ) : (
            <>
              {sessionTailLines.map((entry, idx) => {
                const lineNo = getDigestEntryLine(entry);
                const canJump = lineNo !== null && !hasGcError;
                return (
                  <button
                    key={`stail-${lineNo ?? idx}-${idx}`}
                    type="button"
                    disabled={!canJump}
                    onClick={() => {
                      if (lineNo !== null) {
                        onJumpToLine(lineNo);
                      }
                    }}
                    className="w-full px-3 py-2 text-left transition-colors hover:bg-slate-800/70 disabled:cursor-default disabled:hover:bg-transparent"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-mono text-[11px] text-emerald-300">
                        {lineNo !== null ? `L${lineNo}` : `#${idx + 1}`}
                      </span>
                      {entry.timestamp && (
                        <span className="truncate font-mono text-[10px] text-slate-400">
                          {entry.timestamp}
                        </span>
                      )}
                    </div>
                    <p className="mt-1 line-clamp-2 break-all font-mono text-[11px] text-slate-200">
                      {entry.text}
                    </p>
                  </button>
                );
              })}
              {postSessionLines.length > 0 && (
                <div className="bg-amber-950/30">
                  <div className="border-y border-amber-500/30 bg-amber-950/60 px-3 py-1.5 text-[10px] font-semibold text-amber-300">
                    {t("log.postSessionHeading", {
                      count: postSessionLines.length,
                    })}
                  </div>
                  <div className="divide-y divide-slate-800/80">
                    {postSessionLines.map((entry, idx) => {
                      const lineNo = getDigestEntryLine(entry);
                      const canJump = lineNo !== null && !hasGcError;
                      return (
                        <button
                          key={`post-${lineNo ?? idx}-${idx}`}
                          type="button"
                          disabled={!canJump}
                          onClick={() => {
                            if (lineNo !== null) {
                              onJumpToLine(lineNo);
                            }
                          }}
                          className="w-full px-3 py-2 text-left transition-colors hover:bg-amber-950/50 disabled:cursor-default"
                        >
                          <div className="flex items-center justify-between gap-2">
                            <span className="font-mono text-[11px] text-amber-300">
                              {lineNo !== null ? `L${lineNo}` : `#${idx + 1}`}
                            </span>
                            {entry.level && (
                              <span className="rounded border border-amber-500/30 bg-amber-500/10 px-1 py-0.5 font-mono text-[9px] text-amber-200">
                                {entry.level}
                              </span>
                            )}
                          </div>
                          <p className="mt-1 line-clamp-2 break-all font-mono text-[11px] text-amber-100/90">
                            {entry.text}
                          </p>
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}
            </>
          )
        ) : sidebarTab === "errors" ? (
          recentErrors.length === 0 ? (
            <div className="px-4 py-8 text-center text-xs text-slate-400">
              {t("log.noDigestErrors")}
            </div>
          ) : (
            recentErrors.map((entry, idx) => {
              const lineNo = getDigestEntryLine(entry);
              const canJump = lineNo !== null && !hasGcError;
              return (
                <button
                  key={`err-${lineNo ?? idx}-${idx}`}
                  type="button"
                  disabled={!canJump}
                  onClick={() => {
                    if (lineNo !== null) {
                      onJumpToLine(lineNo);
                    }
                  }}
                  className="w-full px-3 py-2 text-left transition-colors hover:bg-slate-800/70 disabled:cursor-default disabled:hover:bg-transparent"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span
                      className={`rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase ${getDigestLevelBadgeClass(
                        entry.level,
                      )}`}
                    >
                      {entry.level || "ERROR"}
                    </span>
                    <span className="font-mono text-[11px] text-indigo-300">
                      {lineNo !== null ? `L${lineNo}` : "-"}
                    </span>
                  </div>
                  {entry.timestamp && (
                    <div className="mt-1 font-mono text-[10px] text-slate-400">
                      {entry.timestamp}
                    </div>
                  )}
                  <p className="mt-1 line-clamp-2 break-all font-mono text-[11px] text-slate-200">
                    {entry.text}
                  </p>
                </button>
              );
            })
          )
        ) : tailLines.length === 0 ? (
          <div className="px-4 py-8 text-center text-xs text-slate-400">
            {t("log.noDigestTail")}
          </div>
        ) : (
          tailLines.map((entry, idx) => {
            const lineNo = getDigestEntryLine(entry);
            const canJump = lineNo !== null && !hasGcError;
            return (
              <button
                key={`tail-${lineNo ?? idx}-${idx}`}
                type="button"
                disabled={!canJump}
                onClick={() => {
                  if (lineNo !== null) {
                    onJumpToLine(lineNo);
                  }
                }}
                className="w-full px-3 py-2 text-left transition-colors hover:bg-slate-800/70 disabled:cursor-default disabled:hover:bg-transparent"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-mono text-[11px] text-indigo-300">
                    {lineNo !== null ? `L${lineNo}` : `#${idx + 1}`}
                  </span>
                  {entry.timestamp && (
                    <span className="truncate font-mono text-[10px] text-slate-400">
                      {entry.timestamp}
                    </span>
                  )}
                </div>
                <p className="mt-1 line-clamp-2 break-all font-mono text-[11px] text-slate-300">
                  {entry.text}
                </p>
              </button>
            );
          })
        )}
      </div>
    </aside>
  );
}
