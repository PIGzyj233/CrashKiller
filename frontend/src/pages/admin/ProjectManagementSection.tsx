import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, X } from "lucide-react";
import {
  ApiError,
  createProject,
  getProjectApiKey,
  getStoredProjectApiKey,
  removeStoredProjectApiKey,
  rotateProjectKey,
  setStoredProjectApiKey,
  syncProjectApiKey,
  updateProject,
} from "../../api/client";
import type {
  AITriageMode,
  ProjectRead,
  ProjectSettings,
} from "../../api/types";
import { useProjectContext } from "../../components/Layout";
import { useAppI18n } from "../../i18n";
import { CreateProjectForm } from "./CreateProjectForm";
import {
  copyTextToClipboard,
  parseLineInput,
  parseListInput,
  type RevealedApiKeyInfo,
} from "./helpers";
import {
  ProjectEditorForm,
  type ProjectEditorValues,
} from "./ProjectEditorForm";
import { ProjectListPanel } from "./ProjectListPanel";

interface ProjectManagementSectionProps {
  onKeyRevealed: (info: RevealedApiKeyInfo) => void;
}

const EMPTY_EDITOR_VALUES: ProjectEditorValues = {
  name: "",
  aiTriageMode: "",
  sessionIdLabel: "",
  noiseModulesText: "",
  noiseFunctionsText: "",
  customHandlersText: "",
  logRetentionDays: "",
  dumpRetentionDays: "",
};

async function fetchAndResolveProjectApiKey(
  proj: ProjectRead,
): Promise<string | null> {
  const localKey = getStoredProjectApiKey(proj.key);
  try {
    const res = await getProjectApiKey(proj.id);
    if (res.api_key) {
      setStoredProjectApiKey(proj.key, res.api_key);
      return res.api_key;
    }
    if (localKey) {
      try {
        const synced = await syncProjectApiKey(proj.id, localKey);
        if (synced.api_key) {
          setStoredProjectApiKey(proj.key, synced.api_key);
          return synced.api_key;
        }
      } catch (syncErr) {
        if (syncErr instanceof ApiError && syncErr.status === 400) {
          removeStoredProjectApiKey(proj.key);
          return null;
        }
        return localKey;
      }
    }
    return null;
  } catch (err) {
    if (
      localKey &&
      err instanceof ApiError &&
      (err.status === 404 || err.status === 405)
    ) {
      return localKey;
    }
    throw err;
  }
}

export function ProjectManagementSection({
  onKeyRevealed,
}: ProjectManagementSectionProps) {
  const { t } = useAppI18n();
  const { projects, selectedProjectId, refreshProjects } = useProjectContext();
  const queryClient = useQueryClient();

  // Create Project form
  const [newProjectKey, setNewProjectKey] = useState("");
  const [newProjectName, setNewProjectName] = useState("");
  const [newProjectTriageMode, setNewProjectTriageMode] = useState<
    AITriageMode | ""
  >("");
  const [newProjectSessionIdLabel, setNewProjectSessionIdLabel] = useState("");

  // Edit Project state
  const [editingProjectId, setEditingProjectId] = useState<string>("");
  const [confirmRotateProjectId, setConfirmRotateProjectId] = useState<
    string | null
  >(null);
  const [showEditorApiKey, setShowEditorApiKey] = useState(false);
  const [visibleCardKeyIds, setVisibleCardKeyIds] = useState<
    Record<string, boolean>
  >({});
  const [resolvedProjectKeys, setResolvedProjectKeys] = useState<
    Record<string, string | null>
  >({});
  const [loadingCardKeyId, setLoadingCardKeyId] = useState<string | null>(null);
  const [copiedProjectKeyTarget, setCopiedProjectKeyTarget] = useState<
    string | null
  >(null);
  const [projectKeyError, setProjectKeyError] = useState<{
    projectId: string;
    message: string;
  } | null>(null);
  const [editorValues, setEditorValues] =
    useState<ProjectEditorValues>(EMPTY_EDITOR_VALUES);
  const [projectSavedMsg, setProjectSavedMsg] = useState<string | null>(null);

  // Pick initial editing project
  useEffect(() => {
    if (projects.length === 0) {
      setEditingProjectId("");
      return;
    }
    if (
      !editingProjectId ||
      !projects.some((p) => p.id === editingProjectId)
    ) {
      setEditingProjectId(
        selectedProjectId && projects.some((p) => p.id === selectedProjectId)
          ? selectedProjectId
          : projects[0].id,
      );
    }
  }, [projects, selectedProjectId, editingProjectId]);

  const editingProject = useMemo(
    () => projects.find((p) => p.id === editingProjectId),
    [projects, editingProjectId],
  );

  const editingProjectKeyQuery = useQuery({
    queryKey: ["project-api-key", editingProject?.id],
    queryFn: () => fetchAndResolveProjectApiKey(editingProject!),
    enabled: Boolean(editingProject),
    staleTime: 60_000,
  });

  useEffect(() => {
    if (!editingProject || editingProjectKeyQuery.data === undefined) {
      return;
    }
    setResolvedProjectKeys((prev) =>
      prev[editingProject.id] === editingProjectKeyQuery.data
        ? prev
        : { ...prev, [editingProject.id]: editingProjectKeyQuery.data },
    );
  }, [editingProject, editingProjectKeyQuery.data]);

  const syncedProjectIdRef = useRef<string | null>(null);

  const syncProjectForm = (proj: ProjectRead) => {
    const s = proj.settings_json ?? {};
    setEditorValues({
      name: proj.name,
      aiTriageMode: s.ai_triage_mode ?? "",
      sessionIdLabel:
        typeof s.session_id_label === "string" ? s.session_id_label : "",
      noiseModulesText: (s.noise_modules ?? []).join("\n"),
      noiseFunctionsText: (s.noise_functions ?? []).join("\n"),
      customHandlersText: (s.custom_crash_handlers ?? []).join("\n"),
      logRetentionDays:
        typeof s.log_retention_days === "number"
          ? String(s.log_retention_days)
          : "",
      dumpRetentionDays:
        typeof s.dump_retention_days === "number"
          ? String(s.dump_retention_days)
          : "",
    });
  };

  useEffect(() => {
    if (!editingProject) {
      syncedProjectIdRef.current = null;
      setProjectSavedMsg(null);
      return;
    }
    if (syncedProjectIdRef.current !== editingProject.id) {
      syncedProjectIdRef.current = editingProject.id;
      syncProjectForm(editingProject);
      setProjectSavedMsg(null);
      setShowEditorApiKey(false);
    }
  }, [editingProject]);

  const createProjectMutation = useMutation({
    mutationFn: createProject,
    onSuccess: (created) => {
      setStoredProjectApiKey(created.key, created.api_key);
      queryClient.setQueryData(
        ["project-api-key", created.id],
        created.api_key,
      );
      setResolvedProjectKeys((prev) => ({
        ...prev,
        [created.id]: created.api_key,
      }));
      onKeyRevealed({
        projectKey: created.key,
        projectName: created.name,
        apiKey: created.api_key,
        action: "created",
      });
      setNewProjectKey("");
      setNewProjectName("");
      setNewProjectTriageMode("");
      setNewProjectSessionIdLabel("");
      setEditingProjectId(created.id);
      refreshProjects();
    },
  });

  const updateProjectMutation = useMutation({
    mutationFn: ({
      projectId,
      name,
      settings_json,
    }: {
      projectId: string;
      name: string;
      settings_json: ProjectSettings;
    }) => updateProject(projectId, { name, settings_json }),
    onSuccess: (updated) => {
      if (syncedProjectIdRef.current === updated.id) {
        syncProjectForm(updated);
        setProjectSavedMsg(
          t("admin.projects.savedMsg", { name: updated.name }),
        );
      }
      refreshProjects();
    },
  });

  const rotateKeyMutation = useMutation({
    mutationFn: (projectId: string) => rotateProjectKey(projectId),
    onSuccess: (rotated) => {
      setStoredProjectApiKey(rotated.key, rotated.api_key);
      queryClient.setQueryData(
        ["project-api-key", rotated.id],
        rotated.api_key,
      );
      setResolvedProjectKeys((prev) => ({
        ...prev,
        [rotated.id]: rotated.api_key,
      }));
      setProjectKeyError((prev) =>
        prev?.projectId === rotated.id ? null : prev,
      );
      const matched = projects.find((p) => p.id === rotated.id);
      onKeyRevealed({
        projectKey: rotated.key,
        projectName: matched?.name ?? rotated.key,
        apiKey: rotated.api_key,
        action: "rotated",
      });
      setConfirmRotateProjectId(null);
      refreshProjects();
    },
  });

  const handleCreateProject = (e: FormEvent) => {
    e.preventDefault();
    const key = newProjectKey.trim();
    const name = newProjectName.trim();
    if (!key || !name) {
      return;
    }
    const settings_json: ProjectSettings = {};
    if (newProjectTriageMode) {
      settings_json.ai_triage_mode = newProjectTriageMode;
    }
    if (newProjectSessionIdLabel.trim()) {
      settings_json.session_id_label = newProjectSessionIdLabel.trim();
    }
    createProjectMutation.mutate({ key, name, settings_json });
  };

  const handleEditorFieldChange = <K extends keyof ProjectEditorValues>(
    field: K,
    value: ProjectEditorValues[K],
  ) => {
    setEditorValues((prev) => ({ ...prev, [field]: value }));
  };

  const handleSaveProject = (e: FormEvent) => {
    e.preventDefault();
    if (!editingProject || !editorValues.name.trim()) {
      return;
    }
    const nextSettings: ProjectSettings = {
      ...(editingProject.settings_json ?? {}),
      ai_triage_mode: editorValues.aiTriageMode || null,
      noise_modules: parseListInput(editorValues.noiseModulesText),
      noise_functions: parseLineInput(editorValues.noiseFunctionsText),
      custom_crash_handlers: parseLineInput(editorValues.customHandlersText),
    };

    if (editorValues.sessionIdLabel.trim()) {
      nextSettings.session_id_label = editorValues.sessionIdLabel.trim();
    } else {
      delete nextSettings.session_id_label;
    }

    const logRet = Number(editorValues.logRetentionDays.trim());
    if (
      editorValues.logRetentionDays.trim() &&
      Number.isInteger(logRet) &&
      logRet > 0
    ) {
      nextSettings.log_retention_days = logRet;
    } else {
      delete nextSettings.log_retention_days;
    }

    const dumpRet = Number(editorValues.dumpRetentionDays.trim());
    if (
      editorValues.dumpRetentionDays.trim() &&
      Number.isInteger(dumpRet) &&
      dumpRet > 0
    ) {
      nextSettings.dump_retention_days = dumpRet;
    } else {
      delete nextSettings.dump_retention_days;
    }

    setProjectSavedMsg(null);
    updateProjectMutation.mutate({
      projectId: editingProject.id,
      name: editorValues.name.trim(),
      settings_json: nextSettings,
    });
  };

  const ensureProjectApiKey = async (
    proj: ProjectRead,
  ): Promise<string | null> => {
    setProjectKeyError((prev) => (prev?.projectId === proj.id ? null : prev));
    setLoadingCardKeyId(proj.id);
    try {
      const key = await queryClient.fetchQuery({
        queryKey: ["project-api-key", proj.id],
        queryFn: () => fetchAndResolveProjectApiKey(proj),
        staleTime: 60_000,
      });
      setResolvedProjectKeys((prev) => ({ ...prev, [proj.id]: key }));
      return key;
    } catch (err) {
      setProjectKeyError({
        projectId: proj.id,
        message:
          err instanceof ApiError
            ? err.message
            : t("admin.projects.loadKeyFailed"),
      });
      return null;
    } finally {
      setLoadingCardKeyId((prev) => (prev === proj.id ? null : prev));
    }
  };

  const handleToggleCardApiKey = async (proj: ProjectRead) => {
    const isCurrentlyVisible = Boolean(visibleCardKeyIds[proj.id]);
    if (isCurrentlyVisible) {
      setVisibleCardKeyIds((prev) => ({ ...prev, [proj.id]: false }));
      return;
    }
    setVisibleCardKeyIds((prev) => ({ ...prev, [proj.id]: true }));
    if (resolvedProjectKeys[proj.id] === undefined) {
      await ensureProjectApiKey(proj);
    }
  };

  const handleCopyProjectApiKey = async (
    proj: ProjectRead,
    targetKey: string,
  ) => {
    let keyToCopy =
      proj.id === editingProject?.id && editingProjectKeyQuery.data !== undefined
        ? editingProjectKeyQuery.data
        : resolvedProjectKeys[proj.id];

    if (keyToCopy === undefined) {
      keyToCopy = await ensureProjectApiKey(proj);
    }
    if (!keyToCopy) {
      setVisibleCardKeyIds((prev) => ({ ...prev, [proj.id]: true }));
      return;
    }
    const ok = await copyTextToClipboard(keyToCopy);
    if (ok) {
      setProjectKeyError((prev) => (prev?.projectId === proj.id ? null : prev));
      setCopiedProjectKeyTarget(targetKey);
      window.setTimeout(() => {
        setCopiedProjectKeyTarget((curr) =>
          curr === targetKey ? null : curr,
        );
      }, 1800);
    } else {
      setProjectKeyError({
        projectId: proj.id,
        message: t("admin.projects.copyKeyFailed"),
      });
      if (targetKey.startsWith("editor:")) {
        setShowEditorApiKey(true);
      } else {
        setVisibleCardKeyIds((prev) => ({ ...prev, [proj.id]: true }));
      }
    }
  };

  return (
    <section className="space-y-5 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
      <div className="flex items-center gap-2">
        <KeyRound className="h-5 w-5 text-indigo-600" />
        <div>
          <h2 className="text-base font-semibold text-slate-900">
            {t("admin.projects.title")}
          </h2>
          <p className="text-xs text-slate-500">
            {t("admin.projects.subtitle")}
          </p>
        </div>
      </div>

      <CreateProjectForm
        projectKey={newProjectKey}
        projectName={newProjectName}
        triageMode={newProjectTriageMode}
        sessionIdLabel={newProjectSessionIdLabel}
        isPending={createProjectMutation.isPending}
        isError={createProjectMutation.isError}
        error={createProjectMutation.error}
        onKeyChange={setNewProjectKey}
        onNameChange={setNewProjectName}
        onTriageModeChange={setNewProjectTriageMode}
        onSessionIdLabelChange={setNewProjectSessionIdLabel}
        onSubmit={handleCreateProject}
      />

      {rotateKeyMutation.isError && (
        <div className="flex items-center justify-between rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
          <span>
            {rotateKeyMutation.error instanceof ApiError
              ? rotateKeyMutation.error.message
              : t("admin.projects.rotateFailed")}
          </span>
          <button
            type="button"
            onClick={() => rotateKeyMutation.reset()}
            className="rounded p-0.5 text-rose-600 hover:bg-rose-100"
            aria-label={t("admin.projects.dismissRotateError")}
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      {/* Projects List + Per-Project Settings Editor */}
      {projects.length === 0 ? (
        <p className="text-sm text-slate-500">{t("admin.projects.empty")}</p>
      ) : (
        <div className="grid gap-6 lg:grid-cols-12">
          <ProjectListPanel
            projects={projects}
            editingProjectId={editingProjectId}
            editingProjectKeyData={editingProjectKeyQuery.data}
            isEditingProjectKeyLoading={editingProjectKeyQuery.isLoading}
            resolvedProjectKeys={resolvedProjectKeys}
            visibleCardKeyIds={visibleCardKeyIds}
            loadingCardKeyId={loadingCardKeyId}
            copiedProjectKeyTarget={copiedProjectKeyTarget}
            projectKeyError={projectKeyError}
            confirmRotateProjectId={confirmRotateProjectId}
            isRotatePending={rotateKeyMutation.isPending}
            onSelectProject={setEditingProjectId}
            onToggleCardApiKey={(proj) => void handleToggleCardApiKey(proj)}
            onCopyProjectApiKey={(proj, targetKey) =>
              void handleCopyProjectApiKey(proj, targetKey)
            }
            onRequestRotate={(projectId) => {
              rotateKeyMutation.reset();
              setConfirmRotateProjectId(projectId);
            }}
            onCancelRotate={() => setConfirmRotateProjectId(null)}
            onConfirmRotate={(projectId) => rotateKeyMutation.mutate(projectId)}
          />

          {editingProject && (
            <ProjectEditorForm
              project={editingProject}
              values={editorValues}
              projectSavedMsg={projectSavedMsg}
              isSaving={updateProjectMutation.isPending}
              saveError={
                updateProjectMutation.isError
                  ? updateProjectMutation.error
                  : null
              }
              apiKeyData={editingProjectKeyQuery.data}
              isApiKeyLoading={editingProjectKeyQuery.isLoading}
              isApiKeyError={editingProjectKeyQuery.isError}
              apiKeyError={editingProjectKeyQuery.error}
              showEditorApiKey={showEditorApiKey}
              copiedProjectKeyTarget={copiedProjectKeyTarget}
              projectKeyError={projectKeyError}
              onFieldChange={handleEditorFieldChange}
              onToggleShowEditorApiKey={() =>
                setShowEditorApiKey((prev) => !prev)
              }
              onCopyApiKey={(proj, targetKey) =>
                void handleCopyProjectApiKey(proj, targetKey)
              }
              onSubmit={handleSaveProject}
            />
          )}
        </div>
      )}
    </section>
  );
}
