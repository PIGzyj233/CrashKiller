import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Copy,
  RefreshCw,
  RotateCcw,
  Search,
  Trash2,
} from "lucide-react";
import { ApiError } from "../../api/client";
import type { ProjectRead, SymbolRead } from "../../api/types";
import { useAppI18n } from "../../i18n";
import { formatBytes, PAGE_SIZE } from "./helpers";

interface ProjectInventoryTabProps {
  selectedProjectId: string;
  projectById: Map<string, ProjectRead>;
  searchInput: string;
  appVersionInput: string;
  peUnwindFilter: "all" | "true" | "false";
  offset: number;
  symbolsList: SymbolRead[];
  symbolsTotal: number;
  isPending: boolean;
  isFetching: boolean;
  isError: boolean;
  error: unknown;
  copiedText: string | null;
  onSearchChange: (val: string) => void;
  onAppVersionChange: (val: string) => void;
  onPeUnwindFilterChange: (val: "all" | "true" | "false") => void;
  onResetFilters: () => void;
  onRefetch: () => void;
  onCopy: (text: string) => void;
  onRequestUpload: () => void;
  onRequestDelete: (sym: SymbolRead) => void;
  onPrevPage: () => void;
  onNextPage: () => void;
}

export function ProjectInventoryTab({
  selectedProjectId,
  projectById,
  searchInput,
  appVersionInput,
  peUnwindFilter,
  offset,
  symbolsList,
  symbolsTotal,
  isPending,
  isFetching,
  isError,
  error,
  copiedText,
  onSearchChange,
  onAppVersionChange,
  onPeUnwindFilterChange,
  onResetFilters,
  onRefetch,
  onCopy,
  onRequestUpload,
  onRequestDelete,
  onPrevPage,
  onNextPage,
}: ProjectInventoryTabProps) {
  const { t, formatDateTime, formatNumber } = useAppI18n();

  const fromIdx = symbolsTotal === 0 ? 0 : offset + 1;
  const toIdx = Math.min(symbolsTotal, offset + PAGE_SIZE);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3 rounded-xl border border-slate-200 bg-white p-3.5 shadow-sm">
        <div className="relative min-w-[220px] flex-1">
          <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
          <input
            type="text"
            value={searchInput}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder={t("symbols.searchPlaceholder")}
            className="h-9 w-full rounded-md border border-slate-300 pl-9 pr-3 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
          />
        </div>

        <input
          type="text"
          value={appVersionInput}
          onChange={(e) => onAppVersionChange(e.target.value)}
          placeholder={t("symbols.appVersionPlaceholder")}
          aria-label={t("symbols.filterAppVersionAria")}
          className="h-9 w-44 rounded-md border border-slate-300 px-3 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
        />

        <select
          aria-label={t("symbols.filterPeUnwindAria")}
          value={peUnwindFilter}
          onChange={(e) =>
            onPeUnwindFilterChange(
              e.target.value as "all" | "true" | "false",
            )
          }
          className="h-9 rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-800"
        >
          <option value="all">{t("symbols.peAll")}</option>
          <option value="true">{t("symbols.peFull")}</option>
          <option value="false">{t("symbols.peMissing")}</option>
        </select>

        {(searchInput || appVersionInput || peUnwindFilter !== "all") && (
          <button
            type="button"
            onClick={onResetFilters}
            className="inline-flex h-9 items-center gap-1 rounded-md px-2.5 text-xs font-medium text-slate-600 hover:bg-slate-100"
          >
            <RotateCcw className="h-3.5 w-3.5" />
            {t("common.reset")}
          </button>
        )}

        <button
          type="button"
          onClick={onRefetch}
          disabled={isFetching}
          className="inline-flex h-9 items-center gap-1.5 rounded-md border border-slate-200 bg-white px-3 text-xs font-medium text-slate-700 hover:bg-slate-50"
        >
          <RefreshCw
            className={`h-3.5 w-3.5 ${isFetching ? "animate-spin" : ""}`}
          />
          {t("common.refresh")}
        </button>
      </div>

      {isError && (
        <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          <span>
            {error instanceof ApiError
              ? error.message
              : t("symbols.loadInventoryFailed")}
          </span>
          <button
            type="button"
            onClick={onRefetch}
            className="rounded border border-rose-300 bg-white px-2.5 py-1 text-xs font-medium text-rose-700"
          >
            {t("common.retry")}
          </button>
        </div>
      )}

      <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-200 text-left text-sm">
            <thead className="bg-slate-50 text-xs font-medium uppercase tracking-wider text-slate-500">
              <tr>
                <th className="px-4 py-3">
                  {t("symbols.colModuleDebugFile")}
                </th>
                <th className="px-4 py-3">{t("symbols.colDebugId")}</th>
                <th className="px-4 py-3">{t("symbols.colAppVersion")}</th>
                <th className="px-4 py-3">{t("symbols.colPeUnwind")}</th>
                <th className="px-4 py-3">{t("symbols.colSizes")}</th>
                <th className="px-4 py-3">{t("symbols.colLastUsed")}</th>
                <th className="px-4 py-3 text-right">
                  {t("symbols.colActions")}
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
                    {t("symbols.loadingInventory")}
                  </td>
                </tr>
              ) : symbolsList.length === 0 ? (
                <tr>
                  <td
                    colSpan={7}
                    className="px-4 py-12 text-center text-sm text-slate-500"
                  >
                    {t("symbols.emptyInventoryPrefix")}
                    <button
                      type="button"
                      onClick={onRequestUpload}
                      className="font-medium text-indigo-600 hover:underline"
                    >
                      {t("symbols.emptyInventoryAction")}
                    </button>
                  </td>
                </tr>
              ) : (
                symbolsList.map((sym: SymbolRead) => {
                  const project = sym.project_id
                    ? projectById.get(sym.project_id)
                    : undefined;
                  return (
                    <tr key={sym.id} className="hover:bg-slate-50">
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-2">
                          <span className="font-medium text-slate-900">
                            {sym.module_name}
                          </span>
                          {!selectedProjectId && project && (
                            <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-600">
                              {project.key}
                            </span>
                          )}
                        </div>
                        <div className="font-mono text-xs text-slate-500">
                          {sym.debug_file}
                          {sym.code_file ? ` · ${sym.code_file}` : ""}
                        </div>
                      </td>

                      <td className="px-4 py-3">
                        <div className="flex items-center gap-1.5">
                          <code className="font-mono text-xs text-slate-800">
                            {sym.debug_id}
                          </code>
                          <button
                            type="button"
                            onClick={() => onCopy(sym.debug_id)}
                            className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
                            title={t("symbols.copyDebugId")}
                          >
                            {copiedText === sym.debug_id ? (
                              <Check className="h-3.5 w-3.5 text-emerald-600" />
                            ) : (
                              <Copy className="h-3.5 w-3.5" />
                            )}
                          </button>
                        </div>
                      </td>

                      <td className="px-4 py-3 font-mono text-xs text-slate-700">
                        {sym.app_version ?? "—"}
                      </td>

                      <td className="px-4 py-3">
                        {sym.has_pe_unwind ? (
                          <span className="inline-flex items-center gap-1 rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-700">
                            <CheckCircle2 className="h-3.5 w-3.5" />
                            {t("symbols.peUnwindOk")}
                          </span>
                        ) : (
                          <span
                            data-testid="missing-pe-unwind-badge"
                            className="inline-flex items-center gap-1 rounded-full border border-amber-300 bg-amber-50 px-2.5 py-0.5 text-xs font-medium text-amber-800"
                          >
                            <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-amber-600" />
                            {t("symbols.peUnwindMissing")}
                          </span>
                        )}
                      </td>

                      <td className="px-4 py-3 font-mono text-xs text-slate-600">
                        <div>.sym: {formatBytes(sym.sym_size)}</div>
                        <div className="text-[11px] text-slate-400">
                          raw:{" "}
                          {sym.raw_size > 0
                            ? formatBytes(sym.raw_size)
                            : t("symbols.rawNotArchived")}
                        </div>
                      </td>

                      <td className="px-4 py-3 text-xs text-slate-600">
                        {formatDateTime(sym.last_used_at ?? sym.created_at)}
                      </td>

                      <td className="px-4 py-3 text-right">
                        <button
                          type="button"
                          onClick={() => onRequestDelete(sym)}
                          aria-label={t("symbols.deleteSymbolAria", {
                            file: sym.debug_file,
                          })}
                          className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-rose-600 hover:border-rose-200 hover:bg-rose-50"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                          {t("common.delete")}
                        </button>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 bg-slate-50 px-4 py-3 text-xs text-slate-600">
          <span>
            {t("common.showingRange", {
              from: fromIdx,
              to: toIdx,
              total: formatNumber(symbolsTotal),
            })}
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={offset === 0}
              onClick={onPrevPage}
              className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1 font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
              {t("common.previous")}
            </button>
            <button
              type="button"
              disabled={offset + PAGE_SIZE >= symbolsTotal}
              onClick={onNextPage}
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
