import type zhCNDashboard from "../zh-CN/dashboard";
import type { DeepStringify } from "./types";

const enUSDashboard: DeepStringify<typeof zhCNDashboard> = {
  dashboard: {
    title: "Dashboard",
    subtitle:
      "Crash telemetry, AI triage overview, symbol health, and disk quotas",
    scope: "Scope: {{scope}}",
    loadingProjects: "Loading projects...",
    projectsCount: "{{count}} project(s)",
    trendWindowAria: "Trend window days",
    daysLabel: "{{days}}d",
    loadFailed: "Failed to load dashboard telemetry.",
    floorBreachedTitle:
      "Host Safety Floor Breached ({{free}} GB free < {{floor}} GB floor)",
    floorNearTitle:
      "Approaching Host Safety Floor ({{free}} GB free, floor: {{floor}} GB)",
    floorBreachedDesc:
      "Emergency GC protection is active and raw symbol archiving is paused until host free disk space recovers above the safety floor.",
    floorNearDesc:
      "Free space on the host volume is approaching the safety floor threshold. Consider running garbage collection or lowering retention days.",
    manageStorageGc: "Manage Storage & GC",
    kpiTotalCrashes: "Total Crashes",
    kpiViewAllCrashes: "View all crash reports →",
    kpiAnalyzed: "Analyzed",
    kpiFailedCount: "{{value}} failed",
    kpiCompletedPipeline: "Completed pipeline & triage",
    kpiPendingActive: "Pending / Active",
    kpiPendingDesc: "Queued or parsing/triaging →",
    kpiOpenIssues: "Open Issues",
    kpiTotalTrackedIssues: "{{value}} total tracked issues",
    kpiOpenStatuses: "NEW / KNOWN / REGRESSION",
    kpiMissingSymbols: "Missing Symbols",
    kpiIndexedSymbols: "{{value}} indexed symbols",
    kpiViewMissingQueue: "View missing PDB queue →",
    trendTitle: "Crash Volume Trend ({{days}}d)",
    trendSubtitle: "Daily ingested crash reports and analyzed completions",
    trendEmpty: "No crash trend data in the last {{days}} days.",
    seriesTotalCrashes: "Total Crashes",
    seriesAnalyzed: "Analyzed",
    verdictTitle: "AI Verdict Distribution",
    verdictSubtitle: "Breakdown of automated ReAct triage verdicts",
    verdictEmpty: "No AI triage verdicts recorded yet.",
    issueStatusTitle: "Issue Status Distribution",
    issueStatusSubtitle: "Tracked issues by lifecycle state",
    allIssuesLink: "All Issues",
    issueStatusEmpty: "No issues found for current scope.",
    issuesTooltipLabel: "Issues",
    missingSymbolsTitle: "Top Missing Symbols",
    missingSymbolsSubtitle:
      "Unindexed modules impacting minidump stack resolution",
    openMissingQueue: "Open Missing Queue",
    missingSymbolsEmpty: "All observed crash modules have indexed symbols.",
    colModuleDebugFile: "Module / Debug File",
    colDebugId: "Debug ID",
    colCrashes: "Crashes",
    colLatestCrash: "Latest Crash",
    colAction: "Action",
    resolveAction: "Resolve",
    storageTitle: "Storage Quotas & 150GB Hard Cap",
    storageSubtitle:
      "Per-partition usage vs configured quotas and 50GB host safety floor",
    hostFreeBadge: "Host Free: {{free}} GB (Floor: {{floor}} GB)",
    quotaGcSettings: "Quota & GC Settings",
    totalDiskUsage: "Total Platform Disk Usage",
    capLabel: "Cap: {{quota}}",
    partitions: {
      logs: "Logs (.log.zst)",
      dumps: "Minidumps (.dmp.zst)",
      "symbols/sym": "Breakpad Symbols (.sym)",
      "symbols/raw": "Raw Symbols Archive",
      "symbols/public_cache": "Public Symbol Cache",
      db: "Database (PostgreSQL)",
      staging: "Staging Buffer",
    },
  },
};

export default enUSDashboard;
