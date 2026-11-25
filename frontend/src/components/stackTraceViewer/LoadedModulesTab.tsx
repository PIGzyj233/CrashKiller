import { Search } from "lucide-react";
import { useAppI18n } from "../../i18n";
import type { CompatModule, ModuleCounts, ModuleFilter } from "./types";
import { getModuleDisplayName, getModuleStatus } from "./utils";

export interface LoadedModulesTabProps {
  modules: CompatModule[];
  filteredModules: CompatModule[];
  moduleCounts: ModuleCounts;
  moduleQuery: string;
  moduleFilter: ModuleFilter;
  onModuleQueryChange: (value: string) => void;
  onModuleFilterChange: (filter: ModuleFilter) => void;
}

export function LoadedModulesTab({
  modules,
  filteredModules,
  moduleCounts,
  moduleQuery,
  moduleFilter,
  onModuleQueryChange,
  onModuleFilterChange,
}: LoadedModulesTabProps) {
  const { t } = useAppI18n();

  const filterPills = [
    { id: "all" as const, label: t("stack.filterAll", { count: modules.length }) },
    {
      id: "loaded" as const,
      label: t("stack.filterLoaded", { count: moduleCounts.loaded }),
    },
    {
      id: "missing" as const,
      label: t("stack.filterMissing", { count: moduleCounts.missing }),
    },
    ...(moduleCounts.unreferenced > 0
      ? [
          {
            id: "unreferenced" as const,
            label: t("stack.filterUnreferenced", {
              count: moduleCounts.unreferenced,
            }),
          },
        ]
      : []),
    {
      id: "corrupt" as const,
      label: t("stack.filterCorrupt", { count: moduleCounts.corrupt }),
    },
  ];

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-slate-50/70 px-4 py-2.5">
        <div className="relative min-w-[220px] flex-1 sm:max-w-xs">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-400" />
          <input
            type="text"
            value={moduleQuery}
            onChange={(e) => onModuleQueryChange(e.target.value)}
            placeholder={t("stack.searchModulesPlaceholder")}
            aria-label={t("stack.searchModulesAria")}
            className="h-8 w-full rounded-md border border-slate-300 bg-white pl-8 pr-3 text-xs text-slate-800 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
          />
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          {filterPills.map((pill) => (
            <button
              key={pill.id}
              type="button"
              onClick={() => onModuleFilterChange(pill.id)}
              className={`rounded-md border px-2.5 py-1 text-xs font-medium transition-colors ${
                moduleFilter === pill.id
                  ? "border-indigo-300 bg-indigo-50 text-indigo-700"
                  : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
              }`}
            >
              {pill.label}
            </button>
          ))}
        </div>
      </div>

      <div className="ck-scrollbar max-h-[520px] overflow-auto">
        {filteredModules.length === 0 ? (
          <div className="px-6 py-10 text-center text-sm text-slate-500">
            {modules.length === 0
              ? t("stack.noModulesMetadata")
              : t("stack.noModulesMatch")}
          </div>
        ) : (
          <table className="min-w-full divide-y divide-slate-200 text-left text-xs">
            <thead className="sticky top-0 z-10 bg-slate-50 text-[11px] font-semibold uppercase tracking-wider text-slate-500">
              <tr>
                <th className="px-4 py-2.5">{t("stack.colModule")}</th>
                <th className="px-4 py-2.5">{t("stack.colVersion")}</th>
                <th className="px-4 py-2.5">{t("stack.colAddressRange")}</th>
                <th className="px-4 py-2.5">{t("stack.colDebugFileId")}</th>
                <th className="px-4 py-2.5">{t("stack.colSymbols")}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 bg-white">
              {filteredModules.map((mod, idx) => {
                const modStatus = getModuleStatus(mod);
                return (
                  <tr
                    key={`${mod.filename ?? mod.module_name ?? mod.debug_id ?? "mod"}-${mod.base_addr ?? idx}-${idx}`}
                    className="hover:bg-slate-50"
                  >
                    <td className="max-w-[220px] px-4 py-2.5">
                      <div
                        className="truncate font-mono font-medium text-slate-900"
                        title={mod.filename ?? undefined}
                      >
                        {getModuleDisplayName(mod, t("stack.unknownModule"))}
                      </div>
                      <div
                        className="truncate font-mono text-[11px] text-slate-400"
                        title={mod.filename ?? undefined}
                      >
                        {mod.filename || "-"}
                      </div>
                    </td>
                    <td className="whitespace-nowrap px-4 py-2.5 font-mono text-slate-600">
                      {mod.version || "-"}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2.5 font-mono text-[11px] text-slate-600">
                      {mod.base_addr || "-"}
                      {mod.end_addr ? ` - ${mod.end_addr}` : ""}
                    </td>
                    <td className="max-w-[240px] px-4 py-2.5">
                      <div className="truncate font-mono text-slate-700">
                        {mod.debug_file || mod.code_file || "-"}
                      </div>
                      {(mod.debug_id || mod.code_id) && (
                        <div className="truncate font-mono text-[11px] text-slate-400">
                          {mod.debug_id || mod.code_id}
                        </div>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2.5">
                      {modStatus === "loaded" ? (
                        <span className="inline-flex rounded border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700">
                          {t("stack.statusLoaded")}
                        </span>
                      ) : modStatus === "corrupt" ? (
                        <span className="inline-flex rounded border border-rose-200 bg-rose-50 px-2 py-0.5 text-[11px] font-medium text-rose-700">
                          {t("stack.statusCorrupt")}
                        </span>
                      ) : modStatus === "unreferenced" ? (
                        <span className="inline-flex rounded border border-slate-200 bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600">
                          {t("stack.statusUnreferenced")}
                        </span>
                      ) : (
                        <span className="inline-flex rounded border border-amber-200 bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-800">
                          {t("stack.statusMissing")}
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
