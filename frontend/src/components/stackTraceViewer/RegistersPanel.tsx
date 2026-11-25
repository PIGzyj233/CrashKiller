import { useMemo } from "react";
import { ChevronDown, ChevronRight, Cpu, Search } from "lucide-react";
import { useAppI18n } from "../../i18n";

export interface RegistersPanelProps {
  threadRegisters: Record<string, string> | null;
  expanded: boolean;
  onToggleExpanded: () => void;
  filter: string;
  onFilterChange: (value: string) => void;
}

export function RegistersPanel({
  threadRegisters,
  expanded,
  onToggleExpanded,
  filter,
  onFilterChange,
}: RegistersPanelProps) {
  const { t } = useAppI18n();

  const registerEntries = useMemo(() => {
    if (!threadRegisters) {
      return [];
    }
    const entries = Object.entries(threadRegisters);
    const q = filter.trim().toLowerCase();
    if (!q) {
      return entries;
    }
    return entries.filter(
      ([name, value]) =>
        name.toLowerCase().includes(q) ||
        String(value ?? "").toLowerCase().includes(q),
    );
  }, [threadRegisters, filter]);

  if (!threadRegisters || Object.keys(threadRegisters).length === 0) {
    return null;
  }

  return (
    <div className="border-t border-slate-200 bg-slate-50/50">
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5">
        <button
          type="button"
          onClick={onToggleExpanded}
          className="inline-flex items-center gap-1.5 text-xs font-semibold text-slate-700 hover:text-slate-900"
        >
          {expanded ? (
            <ChevronDown className="h-4 w-4" />
          ) : (
            <ChevronRight className="h-4 w-4" />
          )}
          <Cpu className="h-3.5 w-3.5 text-slate-500" />
          <span>
            {t("stack.cpuRegisters", {
              count: Object.keys(threadRegisters).length,
            })}
          </span>
        </button>

        {expanded && (
          <div className="relative">
            <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-400" />
            <input
              type="text"
              value={filter}
              onChange={(e) => onFilterChange(e.target.value)}
              placeholder={t("stack.filterRegistersPlaceholder")}
              aria-label={t("stack.filterRegistersAria")}
              className="h-7 w-44 rounded-md border border-slate-300 bg-white pl-7 pr-2 text-xs text-slate-800 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />
          </div>
        )}
      </div>

      {expanded && (
        <div className="ck-scrollbar grid max-h-64 grid-cols-2 gap-2 overflow-y-auto px-4 pb-3 sm:grid-cols-3 lg:grid-cols-4">
          {registerEntries.length === 0 ? (
            <p className="col-span-full py-2 text-xs text-slate-500">
              {t("stack.noRegistersMatch", { query: filter })}
            </p>
          ) : (
            registerEntries.map(([regName, regValue]) => (
              <div
                key={regName}
                className="flex items-center justify-between gap-2 rounded-md border border-slate-200 bg-white px-2.5 py-1.5"
              >
                <span className="font-mono text-[11px] font-semibold uppercase text-slate-500">
                  {regName}
                </span>
                <span className="select-all truncate font-mono text-xs text-slate-900">
                  {String(regValue ?? "")}
                </span>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
