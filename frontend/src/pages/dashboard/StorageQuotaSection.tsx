import { Link } from "react-router-dom";
import {
  AlertOctagon,
  AlertTriangle,
  ArrowRight,
  HardDrive,
} from "lucide-react";
import { useAppI18n } from "../../i18n";
import {
  formatGb,
  getBarColorClass,
  type NormalizedStorageSummary,
} from "./helpers";

interface StorageSummaryProps {
  normalizedStorage: NormalizedStorageSummary;
}

export function SafetyFloorBanner({ normalizedStorage }: StorageSummaryProps) {
  const { t } = useAppI18n();

  if (!normalizedStorage.isBreached && !normalizedStorage.isNearFloor) {
    return null;
  }

  return (
    <div
      role="alert"
      className={`flex flex-wrap items-center justify-between gap-3 rounded-xl border px-4 py-3.5 text-sm shadow-sm ${
        normalizedStorage.isBreached
          ? "border-rose-300 bg-rose-50 text-rose-900"
          : "border-amber-300 bg-amber-50 text-amber-900"
      }`}
    >
      <div className="flex items-start gap-3">
        {normalizedStorage.isBreached ? (
          <AlertOctagon className="mt-0.5 h-5 w-5 shrink-0 text-rose-600" />
        ) : (
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-amber-600" />
        )}
        <div>
          <div className="font-semibold">
            {normalizedStorage.isBreached
              ? t("dashboard.floorBreachedTitle", {
                  free: normalizedStorage.storage.host_free_gb.toFixed(1),
                  floor: normalizedStorage.floorGb,
                })
              : t("dashboard.floorNearTitle", {
                  free: normalizedStorage.storage.host_free_gb.toFixed(1),
                  floor: normalizedStorage.floorGb,
                })}
          </div>
          <p className="mt-0.5 text-xs opacity-90">
            {normalizedStorage.isBreached
              ? t("dashboard.floorBreachedDesc")
              : t("dashboard.floorNearDesc")}
          </p>
        </div>
      </div>
      <Link
        to="/admin"
        className={`inline-flex items-center gap-1 rounded-md px-3 py-1.5 text-xs font-medium shadow-sm ${
          normalizedStorage.isBreached
            ? "bg-rose-600 text-white hover:bg-rose-700"
            : "bg-amber-600 text-white hover:bg-amber-700"
        }`}
      >
        {t("dashboard.manageStorageGc")}
        <ArrowRight className="h-3.5 w-3.5" />
      </Link>
    </div>
  );
}

export function StorageQuotaCard({ normalizedStorage }: StorageSummaryProps) {
  const { t } = useAppI18n();

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <HardDrive className="h-5 w-5 text-indigo-600" />
          <div>
            <h2 className="text-sm font-semibold text-slate-900">
              {t("dashboard.storageTitle")}
            </h2>
            <p className="text-xs text-slate-500">
              {t("dashboard.storageSubtitle")}
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span
            className={`rounded-full border px-2.5 py-1 font-medium ${
              normalizedStorage.isBreached
                ? "border-rose-200 bg-rose-50 text-rose-700"
                : normalizedStorage.isNearFloor
                  ? "border-amber-200 bg-amber-50 text-amber-700"
                  : "border-emerald-200 bg-emerald-50 text-emerald-700"
            }`}
          >
            {t("dashboard.hostFreeBadge", {
              free: normalizedStorage.storage.host_free_gb.toFixed(1),
              floor: normalizedStorage.floorGb,
            })}
          </span>
          <Link
            to="/admin"
            className="rounded-md border border-slate-200 bg-white px-2.5 py-1 font-medium text-slate-700 hover:bg-slate-50"
          >
            {t("dashboard.quotaGcSettings")}
          </Link>
        </div>
      </div>

      {/* Total Cap Bar */}
      <div className="mt-4 rounded-lg border border-slate-200 bg-slate-50 p-3.5">
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
          <span className="font-semibold text-slate-800">
            {t("dashboard.totalDiskUsage")}
          </span>
          <span className="font-mono text-slate-700">
            {formatGb(normalizedStorage.totalUsedGb)} /{" "}
            {formatGb(normalizedStorage.totalQuotaGb)} (
            {normalizedStorage.totalPct.toFixed(1)}%)
          </span>
        </div>
        <div className="mt-2 h-2.5 w-full overflow-hidden rounded-full bg-slate-200">
          <div
            className={`h-full rounded-full transition-all ${getBarColorClass(
              normalizedStorage.totalPct,
            )}`}
            style={{ width: `${normalizedStorage.totalPct}%` }}
          />
        </div>
      </div>

      {/* Per-Partition Progress Bars */}
      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {normalizedStorage.partitions.map((part) => (
          <div
            key={part.key}
            className="rounded-lg border border-slate-200 p-3"
          >
            <div className="flex items-center justify-between gap-2 text-xs">
              <span
                className="truncate font-medium text-slate-800"
                title={part.key}
              >
                {part.label}
              </span>
              <span className="shrink-0 font-mono text-[11px] font-semibold text-slate-700">
                {part.pct.toFixed(1)}%
              </span>
            </div>
            <div className="mt-1.5 h-2 w-full overflow-hidden rounded-full bg-slate-100">
              <div
                className={`h-full rounded-full ${getBarColorClass(part.pct)}`}
                style={{ width: `${part.pct}%` }}
              />
            </div>
            <div className="mt-1.5 flex items-center justify-between font-mono text-[11px] text-slate-500">
              <span>{formatGb(part.usedGb)}</span>
              <span>
                {t("dashboard.capLabel", {
                  quota: formatGb(part.quotaGb),
                })}
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
