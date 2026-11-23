import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  NavLink,
  Outlet,
  useNavigate,
  useSearchParams,
} from "react-router-dom";
import {
  Bug,
  FileCode2,
  FolderGit2,
  Layers,
  LayoutDashboard,
  Menu,
  RefreshCw,
  Settings,
  ShieldAlert,
  ShieldCheck,
  Upload,
  X,
} from "lucide-react";
import { getHealth, listProjects } from "../api/client";
import type { ProjectRead } from "../api/types";
import { useAppI18n } from "../i18n";

const PROJECT_STORAGE_KEY = "crashkiller.selectedProjectId";

export interface ProjectContextValue {
  projects: ProjectRead[];
  selectedProjectId: string;
  setSelectedProjectId: (id: string) => void;
  selectedProject: ProjectRead | undefined;
  refreshProjects: () => void;
  isLoadingProjects: boolean;
}

const ProjectContext = createContext<ProjectContextValue | undefined>(undefined);

function readStoredProjectId(): string {
  try {
    return window.localStorage.getItem(PROJECT_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

function writeStoredProjectId(id: string): void {
  try {
    if (id) {
      window.localStorage.setItem(PROJECT_STORAGE_KEY, id);
    } else {
      window.localStorage.removeItem(PROJECT_STORAGE_KEY);
    }
  } catch {
    // Ignore storage quota / privacy mode errors
  }
}

export function ProjectProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const urlProjectParam = searchParams.get("project");

  const [selectedProjectId, setSelectedProjectIdState] = useState<string>(() => {
    if (urlProjectParam !== null) {
      return urlProjectParam;
    }
    return readStoredProjectId();
  });

  const prevUrlProjectRef = useRef<string | null>(urlProjectParam);

  // Persist initial URL-provided project ID once on mount
  useEffect(() => {
    if (urlProjectParam !== null) {
      writeStoredProjectId(urlProjectParam);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Sync external URL ?project= changes (e.g. deep links, browser back/forward)
  useEffect(() => {
    if (urlProjectParam !== prevUrlProjectRef.current) {
      prevUrlProjectRef.current = urlProjectParam;
      if (urlProjectParam !== null && urlProjectParam !== selectedProjectId) {
        setSelectedProjectIdState(urlProjectParam);
        writeStoredProjectId(urlProjectParam);
      }
    }
  }, [urlProjectParam, selectedProjectId]);

  const projectsQuery = useQuery({
    queryKey: ["projects"],
    queryFn: listProjects,
    staleTime: 30_000,
  });

  const projects = useMemo(
    () => projectsQuery.data ?? [],
    [projectsQuery.data],
  );

  const setSelectedProjectId = useCallback(
    (id: string) => {
      setSelectedProjectIdState(id);
      writeStoredProjectId(id);
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (id) {
            next.set("project", id);
          } else {
            next.delete("project");
          }
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  // Clear stale project ID only after a successful projects fetch confirms it does not exist
  useEffect(() => {
    if (
      projectsQuery.isSuccess &&
      selectedProjectId !== "" &&
      !projects.some((p) => p.id === selectedProjectId)
    ) {
      setSelectedProjectId("");
    }
  }, [projectsQuery.isSuccess, projects, selectedProjectId, setSelectedProjectId]);

  const selectedProject = useMemo(
    () => projects.find((p) => p.id === selectedProjectId),
    [projects, selectedProjectId],
  );

  const refreshProjects = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["projects"] });
  }, [queryClient]);

  const value = useMemo<ProjectContextValue>(
    () => ({
      projects,
      selectedProjectId,
      setSelectedProjectId,
      selectedProject,
      refreshProjects,
      isLoadingProjects: projectsQuery.isLoading,
    }),
    [
      projects,
      selectedProjectId,
      setSelectedProjectId,
      selectedProject,
      refreshProjects,
      projectsQuery.isLoading,
    ],
  );

  return (
    <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>
  );
}

export function useProjectContext(): ProjectContextValue {
  const ctx = useContext(ProjectContext);
  if (!ctx) {
    throw new Error("useProjectContext must be used within a ProjectProvider");
  }
  return ctx;
}

interface NavItem {
  to: string;
  labelKey:
    | "nav.dashboard"
    | "nav.issues"
    | "nav.crashes"
    | "nav.symbols"
    | "nav.admin"
    | "nav.settings";
  icon: typeof LayoutDashboard;
  end: boolean;
}

const NAV_ITEMS: NavItem[] = [
  { to: "/", labelKey: "nav.dashboard", icon: LayoutDashboard, end: true },
  { to: "/issues", labelKey: "nav.issues", icon: Layers, end: false },
  { to: "/crashes", labelKey: "nav.crashes", icon: Bug, end: false },
  { to: "/symbols", labelKey: "nav.symbols", icon: FileCode2, end: false },
  { to: "/admin", labelKey: "nav.admin", icon: ShieldCheck, end: false },
  { to: "/settings", labelKey: "nav.settings", icon: Settings, end: false },
];

export interface LayoutProps {
  children?: ReactNode;
  onQuickUpload?: () => void;
}

export function Layout({ children, onQuickUpload }: LayoutProps) {
  const { t } = useAppI18n();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const navigate = useNavigate();
  const {
    projects,
    selectedProjectId,
    setSelectedProjectId,
    refreshProjects,
    isLoadingProjects,
  } = useProjectContext();

  const healthQuery = useQuery({
    queryKey: ["health"],
    queryFn: getHealth,
    refetchInterval: 30_000,
    retry: 0,
    staleTime: 15_000,
  });

  const isBackendHealthy =
    healthQuery.isSuccess && healthQuery.data.status === "ok";

  const handleQuickUpload = () => {
    if (onQuickUpload) {
      onQuickUpload();
      return;
    }
    navigate("/crashes?upload=1");
  };

  const sidebarContent = (
    <div className="flex h-full flex-col bg-slate-950 text-slate-100">
      <div className="flex h-14 items-center justify-between border-b border-slate-800 px-4">
        <div className="flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-indigo-600 text-white">
            <ShieldAlert className="h-4 w-4" />
          </div>
          <div>
            <div className="text-sm font-semibold tracking-tight text-white">
              CrashKiller
            </div>
            <div className="text-[11px] text-slate-400">
              {t("app.subtitle")}
            </div>
          </div>
        </div>
        <button
          type="button"
          onClick={() => setMobileNavOpen(false)}
          className="rounded-md p-1 text-slate-400 hover:bg-slate-800 hover:text-white lg:hidden"
          aria-label={t("nav.closeMenu")}
        >
          <X className="h-5 w-5" />
        </button>
      </div>

      <nav className="flex-1 space-y-1 px-3 py-4" aria-label={t("nav.mainLabel")}>
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon;
          return (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              onClick={() => setMobileNavOpen(false)}
              className={({ isActive }) =>
                `flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                  isActive
                    ? "bg-indigo-600 text-white shadow-sm"
                    : "text-slate-300 hover:bg-slate-900 hover:text-white"
                }`
              }
            >
              <Icon className="h-4 w-4 shrink-0" />
              <span>{t(item.labelKey)}</span>
            </NavLink>
          );
        })}
      </nav>

      <div className="border-t border-slate-800 px-4 py-3">
        <div className="flex items-center justify-between text-xs">
          <span className="text-slate-400">{t("nav.backendApi")}</span>
          <span
            className="inline-flex items-center gap-1.5 font-medium"
            data-testid="health-status-indicator"
          >
            <span
              className={`h-2 w-2 rounded-full ${
                healthQuery.isPending
                  ? "animate-pulse bg-amber-400"
                  : isBackendHealthy
                    ? "bg-emerald-400"
                    : "bg-rose-500"
              }`}
            />
            <span
              className={
                healthQuery.isPending
                  ? "text-amber-300"
                  : isBackendHealthy
                    ? "text-emerald-300"
                    : "text-rose-300"
              }
            >
              {healthQuery.isPending
                ? t("nav.statusChecking")
                : isBackendHealthy
                  ? t("nav.statusOnline")
                  : t("nav.statusOffline")}
            </span>
          </span>
        </div>
      </div>
    </div>
  );

  return (
    <div className="flex min-h-screen bg-slate-50 text-slate-900">
      {/* Desktop sidebar */}
      <aside className="hidden w-64 shrink-0 border-r border-slate-800 lg:block">
        <div className="sticky top-0 h-screen">{sidebarContent}</div>
      </aside>

      {/* Mobile drawer */}
      {mobileNavOpen && (
        <div className="fixed inset-0 z-40 flex lg:hidden">
          <div
            className="fixed inset-0 bg-slate-900/60"
            onClick={() => setMobileNavOpen(false)}
            aria-hidden="true"
          />
          <aside className="relative z-50 w-64 max-w-[80vw] shrink-0">
            {sidebarContent}
          </aside>
        </div>
      )}

      {/* Main content area */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex h-14 items-center justify-between gap-3 border-b border-slate-200 bg-white px-4 sm:px-6">
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => setMobileNavOpen(true)}
              className="rounded-md p-1.5 text-slate-600 hover:bg-slate-100 hover:text-slate-900 lg:hidden"
              aria-label={t("nav.openMenu")}
            >
              <Menu className="h-5 w-5" />
            </button>

            <div className="flex items-center gap-2">
              <FolderGit2 className="hidden h-4 w-4 text-slate-500 sm:block" />
              <label
                htmlFor="global-project-selector"
                className="sr-only"
              >
                {t("nav.projectScope")}
              </label>
              <select
                id="global-project-selector"
                aria-label={t("nav.globalProjectSelector")}
                value={selectedProjectId}
                onChange={(e) => setSelectedProjectId(e.target.value)}
                disabled={isLoadingProjects && projects.length === 0}
                className="h-9 min-w-[180px] max-w-[280px] rounded-md border border-slate-300 bg-white px-2.5 text-sm font-medium text-slate-800 shadow-sm focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:bg-slate-100"
              >
                <option value="">{t("nav.allProjects")}</option>
                {projects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name} ({project.key})
                  </option>
                ))}
              </select>
              <button
                type="button"
                onClick={refreshProjects}
                title={t("nav.refreshProjects")}
                aria-label={t("nav.refreshProjects")}
                className="inline-flex h-9 w-9 items-center justify-center rounded-md border border-slate-200 bg-white text-slate-600 hover:bg-slate-50 hover:text-slate-900"
              >
                <RefreshCw
                  className={`h-4 w-4 ${isLoadingProjects ? "animate-spin" : ""}`}
                />
              </button>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={handleQuickUpload}
              className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:ring-offset-1"
            >
              <Upload className="h-4 w-4" />
              <span className="hidden sm:inline">{t("nav.upload")}</span>
            </button>
          </div>
        </header>

        <main className="mx-auto w-full max-w-7xl flex-1 p-4 sm:p-6">
          {children ?? <Outlet />}
        </main>
      </div>
    </div>
  );
}

export default Layout;
