import { useAppI18n } from "../../i18n";
import { WINDOW_LINES, type LogScrollTarget } from "./types";

export interface WindowNavigatorProps {
  resolvedActiveLogId: string;
  sessionOnlyMode: boolean;
  windowStart: number;
  windowEnd: number;
  effectiveTotalLines: number;
  totalLines: number;
  onJumpToLine: (lineNumber: number, align?: LogScrollTarget["align"]) => void;
  onJumpToSessionIndex: (
    sessionIdx: number,
    align?: LogScrollTarget["align"],
  ) => void;
}

export function WindowNavigator({
  resolvedActiveLogId,
  sessionOnlyMode,
  windowStart,
  windowEnd,
  effectiveTotalLines,
  totalLines,
  onJumpToLine,
  onJumpToSessionIndex,
}: WindowNavigatorProps) {
  const { t, formatNumber } = useAppI18n();

  return (
    <nav
      aria-label={t("log.navAria")}
      className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-700"
    >
      <div className="flex items-center gap-2">
        <button
          type="button"
          aria-label={t("log.prevWindowAria")}
          disabled={windowStart === 0}
          onClick={() =>
            sessionOnlyMode
              ? onJumpToSessionIndex(windowStart - WINDOW_LINES + 1, "start")
              : onJumpToLine(windowStart - WINDOW_LINES + 1, "start")
          }
          className="rounded border border-slate-300 bg-white px-2 py-1 hover:bg-slate-100 disabled:opacity-40"
        >
          {t("log.prevWindow")}
        </button>
        <span aria-live="polite" className="font-mono">
          {t("log.windowRange", {
            start: formatNumber(windowStart + 1),
            end: formatNumber(windowEnd),
            total: formatNumber(effectiveTotalLines),
          })}
        </span>
        <button
          type="button"
          aria-label={t("log.nextWindowAria")}
          disabled={windowEnd === effectiveTotalLines}
          onClick={() =>
            sessionOnlyMode
              ? onJumpToSessionIndex(windowEnd + 1, "start")
              : onJumpToLine(windowEnd + 1, "start")
          }
          className="rounded border border-slate-300 bg-white px-2 py-1 hover:bg-slate-100 disabled:opacity-40"
        >
          {t("log.nextWindow")}
        </button>
      </div>
      {!sessionOnlyMode && (
        <form
          key={resolvedActiveLogId}
          onSubmit={(event) => {
            event.preventDefault();
            const line = new FormData(event.currentTarget).get("line");
            onJumpToLine(Number(line));
          }}
          className="flex items-center gap-1.5"
        >
          <input
            aria-label={t("log.lineNumberAria")}
            name="line"
            type="number"
            min={1}
            max={totalLines}
            required
            placeholder={t("log.lineNumberPlaceholder")}
            className="w-32 rounded border border-slate-300 bg-white px-2 py-1 font-mono"
          />
          <button
            type="submit"
            className="rounded border border-slate-300 bg-white px-2 py-1 hover:bg-slate-100"
          >
            {t("log.goToLine")}
          </button>
        </form>
      )}
    </nav>
  );
}
