import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, HardDrive, Loader2, Trash2 } from "lucide-react";
import { ApiError, triggerGC } from "../../api/client";
import type { GCReport } from "../../api/types";
import { useAppI18n } from "../../i18n";
import { formatBytes, normalizeGCPartitions } from "./helpers";

export function GarbageCollectionSection() {
  const { t, formatNumber } = useAppI18n();
  const queryClient = useQueryClient();

  const [forceEmergencyGc, setForceEmergencyGc] = useState(false);
  const [gcReport, setGcReport] = useState<GCReport | null>(null);

  const gcMutation = useMutation({
    mutationFn: (forceEmergency: boolean) => triggerGC(forceEmergency),
    onSuccess: (report) => {
      setGcReport(report);
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  const gcPartitionRows = useMemo(
    () => (gcReport ? normalizeGCPartitions(gcReport.partitions) : []),
    [gcReport],
  );

  return (
    <section className="space-y-4 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-2">
          <HardDrive className="h-5 w-5 text-indigo-600" />
          <div>
            <h2 className="text-base font-semibold text-slate-900">
              {t("admin.gc.title")}
            </h2>
            <p className="text-xs text-slate-500">
              {t("admin.gc.subtitleBefore")}
              <code className="font-mono">is_pinned=true</code>
              {t("admin.gc.subtitleAfter")}
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <label className="inline-flex cursor-pointer items-center gap-2 text-xs text-slate-700">
            <input
              type="checkbox"
              checked={forceEmergencyGc}
              onChange={(e) => setForceEmergencyGc(e.target.checked)}
              className="h-4 w-4 rounded border-slate-300 text-rose-600"
            />
            {t("admin.gc.forceEmergency")}
          </label>

          <button
            type="button"
            disabled={gcMutation.isPending}
            onClick={() => gcMutation.mutate(forceEmergencyGc)}
            className="inline-flex items-center gap-1.5 rounded-md bg-slate-900 px-3.5 py-2 text-xs font-medium text-white shadow-sm hover:bg-slate-800 disabled:opacity-50"
          >
            {gcMutation.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Trash2 className="h-3.5 w-3.5" />
            )}
            {gcMutation.isPending
              ? t("admin.gc.running")
              : t("admin.gc.triggerButton")}
          </button>
        </div>
      </div>

      {gcMutation.isError && (
        <div className="flex items-center gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span>
            {gcMutation.error instanceof ApiError
              ? gcMutation.error.message
              : t("admin.gc.failed")}
          </span>
        </div>
      )}

      {gcReport && (
        <div className="space-y-4 rounded-lg border border-slate-200 bg-slate-50 p-4">
          <div className="grid gap-3 sm:grid-cols-4">
            <div className="rounded-lg border border-slate-200 bg-white p-3">
              <div className="text-[11px] font-medium text-slate-500">
                {t("admin.gc.freedSpace")}
              </div>
              <div className="mt-1 font-mono text-base font-bold text-slate-900">
                {formatBytes(gcReport.freed_bytes)}
              </div>
              <div className="font-mono text-[11px] text-slate-400">
                {t("common.bytes", {
                  value: formatNumber(gcReport.freed_bytes),
                })}
              </div>
            </div>

            <div className="rounded-lg border border-slate-200 bg-white p-3">
              <div className="text-[11px] font-medium text-slate-500">
                {t("admin.gc.deletedFiles")}
              </div>
              <div className="mt-1 font-mono text-base font-bold text-slate-900">
                {formatNumber(gcReport.deleted_files_count)}
              </div>
            </div>

            <div className="rounded-lg border border-slate-200 bg-white p-3">
              <div className="text-[11px] font-medium text-slate-500">
                {t("admin.gc.skippedPinned")}
              </div>
              <div className="mt-1 font-mono text-base font-bold text-amber-700">
                {formatNumber(gcReport.skipped_pinned_count)}
              </div>
            </div>

            <div className="rounded-lg border border-slate-200 bg-white p-3">
              <div className="text-[11px] font-medium text-slate-500">
                {t("admin.gc.emergencyMode")}
              </div>
              <div className="mt-1.5">
                <span
                  className={`inline-flex rounded-full border px-2.5 py-0.5 text-xs font-medium ${
                    gcReport.emergency_triggered
                      ? "border-rose-200 bg-rose-50 text-rose-700"
                      : "border-emerald-200 bg-emerald-50 text-emerald-700"
                  }`}
                >
                  {gcReport.emergency_triggered
                    ? t("admin.gc.triggered")
                    : t("admin.gc.standard")}
                </span>
              </div>
            </div>
          </div>

          {gcPartitionRows.length > 0 && (
            <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
              <table className="min-w-full divide-y divide-slate-200 text-left text-xs">
                <thead className="bg-slate-50 font-medium text-slate-500">
                  <tr>
                    <th className="px-3 py-2">{t("admin.gc.colPartition")}</th>
                    <th className="px-3 py-2">{t("admin.gc.colUsageQuota")}</th>
                    <th className="px-3 py-2">{t("admin.gc.colUsagePct")}</th>
                    <th className="px-3 py-2">{t("admin.gc.colFiles")}</th>
                    <th className="px-3 py-2">
                      {t("admin.gc.colFreedDeleted")}
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {gcPartitionRows.map((row) => (
                    <tr key={row.name}>
                      <td className="px-3 py-2 font-mono font-medium text-slate-800">
                        {row.name}
                      </td>
                      <td className="px-3 py-2 font-mono text-slate-600">
                        {row.usedBytes !== undefined &&
                        row.quotaBytes !== undefined
                          ? `${formatBytes(row.usedBytes)} / ${formatBytes(
                              row.quotaBytes,
                            )}`
                          : "—"}
                      </td>
                      <td className="px-3 py-2 font-mono text-slate-600">
                        {row.usagePercent !== undefined
                          ? `${row.usagePercent.toFixed(1)}%`
                          : "—"}
                      </td>
                      <td className="px-3 py-2 font-mono text-slate-600">
                        {row.fileCount !== undefined
                          ? formatNumber(row.fileCount)
                          : "—"}
                      </td>
                      <td className="px-3 py-2 font-mono text-slate-600">
                        {row.freedBytes !== undefined ||
                        row.deletedFilesCount !== undefined
                          ? `${
                              row.freedBytes !== undefined
                                ? formatBytes(row.freedBytes)
                                : "—"
                            } (${t("common.filesCount", {
                              count: row.deletedFilesCount ?? 0,
                            })})`
                          : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
