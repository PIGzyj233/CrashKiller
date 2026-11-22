import { Component, type ErrorInfo, type ReactNode } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AlertTriangle, RotateCcw } from "lucide-react";
import { Layout, ProjectProvider } from "./components/Layout";
import { DashboardPage } from "./pages/DashboardPage";
import { IssuesPage } from "./pages/IssuesPage";
import { IssueDetailPage } from "./pages/IssueDetailPage";
import { CrashesPage } from "./pages/CrashesPage";
import { CrashDetailPage } from "./pages/CrashDetailPage";
import { SymbolsPage } from "./pages/SymbolsPage";
import { SettingsPage } from "./pages/SettingsPage";

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

class AppErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  public state: ErrorBoundaryState = { error: null };

  public static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  public componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("Unhandled UI error:", error, info.componentStack);
  }

  private handleReset = (): void => {
    this.setState({ error: null });
  };

  private handleGoHome = (): void => {
    this.setState({ error: null });
    window.location.assign("/");
  };

  public render(): ReactNode {
    if (this.state.error) {
      return (
        <div className="flex min-h-screen items-center justify-center bg-slate-50 p-6">
          <div className="w-full max-w-lg rounded-xl border border-rose-200 bg-white p-6 shadow-sm">
            <div className="flex items-start gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-rose-50 text-rose-600">
                <AlertTriangle className="h-5 w-5" />
              </div>
              <div className="min-w-0 flex-1">
                <h1 className="text-base font-semibold text-slate-900">
                  Something went wrong
                </h1>
                <p className="mt-1 text-sm text-slate-600">
                  An unexpected rendering error occurred in the console. You can
                  retry the current view or return to the dashboard.
                </p>
                <pre className="mt-3 max-h-36 overflow-auto rounded-lg bg-slate-950 p-3 font-mono text-xs text-rose-200">
                  {this.state.error.message || "Unknown render error"}
                </pre>
                <div className="mt-4 flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    onClick={this.handleReset}
                    className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700"
                  >
                    <RotateCcw className="h-4 w-4" />
                    Try Again
                  </button>
                  <button
                    type="button"
                    onClick={this.handleGoHome}
                    className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
                  >
                    Return to Dashboard
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}

export function App() {
  return (
    <BrowserRouter>
      <ProjectProvider>
        <AppErrorBoundary>
          <Routes>
            <Route element={<Layout />}>
              <Route path="/" element={<DashboardPage />} />
              <Route path="/issues" element={<IssuesPage />} />
              <Route path="/issues/:issueId" element={<IssueDetailPage />} />
              <Route path="/crashes" element={<CrashesPage />} />
              <Route path="/crashes/:crashId" element={<CrashDetailPage />} />
              <Route path="/symbols" element={<SymbolsPage />} />
              <Route path="/settings" element={<SettingsPage />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </AppErrorBoundary>
      </ProjectProvider>
    </BrowserRouter>
  );
}

export default App;
