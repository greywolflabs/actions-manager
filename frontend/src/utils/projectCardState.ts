import type { Project, WorkflowData } from "../api/projects";

/*
 * The state a project card shows: its badge, drift indicator and dates. The
 * Projects page and the Managed Files section render cards from these, so the
 * two can never describe the same project differently.
 */

// Constants for project state values
export const PROJECT_STATE = {
  NEW: "new" as const,
  DRAFT: "draft" as const,
  OPEN: "open" as const,
  SYNCED: "synced" as const,
  DELETING: "deleting" as const,
};

export const STATE_DISPLAY = {
  NEW: "New" as const,
  DRAFT: "Draft" as const,
  OPEN: "Under Review" as const,
  SYNCED: "Synced" as const,
  DELETING: "Deleting" as const,
};

// The drift status a check that could not finish leaves.
const CHECK_FAILED = "check_failed";

const WARNING_STATUS_VALUES = new Set([
  "blocked",
  "drift",
  "drifted",
  "error",
  "failed",
  "failed_sync",
  "needs_attention",
]);

const normalizeStatusValue = (value: unknown): string => (
  typeof value === "string" ? value.trim().toLowerCase() : ""
);

export const normalizeProjectName = (project: Project): string => (
  project.project_name?.trim()
  || project.name?.trim()
  || ""
);

export const getProjectWorkflows = (project: Project): WorkflowData[] => [
  ...(project.workflows ?? []),
  ...(project.rxworkflows ?? []),
];

export const projectHasWorkflowStatus = (project: Project, statusValues: Set<string>): boolean => (
  getProjectWorkflows(project).some((workflow) => statusValues.has(normalizeStatusValue(workflow.workflowStatus)))
);

const getCachedDriftStatus = (project: Project): string => {
  const projectFields = project as Project & Record<string, unknown>;
  const status = normalizeStatusValue(projectFields.drift_status);
  return status || "unknown";
};

const hasCachedDriftSummary = (project: Project): boolean => {
  const projectFields = project as Project & Record<string, unknown>;
  return (
    projectFields.drift_status !== undefined
    || projectFields.last_drift_check_at !== undefined
  );
};

export const projectHasNeedsAttention = (project: Project): boolean => {
  const cachedStatus = getCachedDriftStatus(project);
  const cachedNeedsAttention = hasCachedDriftSummary(project)
    && (cachedStatus === "drifted" || cachedStatus === CHECK_FAILED);

  const projectFields = project as Project & Record<string, unknown>;
  const warningFields = [
    projectFields.drift_status,
    projectFields.sync_status,
    projectFields.workflow_status,
    projectFields.workflowStatus,
    projectFields.status,
  ];

  return cachedNeedsAttention
    || projectFields.has_drift === true
    || projectFields.blocked === true
    || warningFields.some((value) => WARNING_STATUS_VALUES.has(normalizeStatusValue(value)))
    || projectHasWorkflowStatus(project, WARNING_STATUS_VALUES);
};

const projectHasDrift = (project: Project): boolean => {
  if (hasCachedDriftSummary(project)) {
    return getCachedDriftStatus(project) === "drifted";
  }

  const projectFields = project as Project & Record<string, unknown>;
  const driftValues = new Set(["drift", "drifted", "drift_detected"]);
  const driftFields = [
    projectFields.drift_status,
    projectFields.workflow_status,
    projectFields.workflowStatus,
    projectFields.status,
  ];

  return projectFields.has_drift === true
    || driftFields.some((value) => driftValues.has(normalizeStatusValue(value)))
    || projectHasWorkflowStatus(project, driftValues);
};

export type ProjectStateTone = "neutral" | "info" | "success" | "warning" | "danger";

/**
 * Where the project is in its delivery lifecycle, drift aside. A Managed Files
 * card shows this beside its separate drift badge; the Projects page shows
 * getProjectState, which puts drift first.
 */
export const getPrStateBadge = (project: Project): { label: string; tone: ProjectStateTone } => {
  switch (project.pr_state ?? PROJECT_STATE.NEW) {
    case PROJECT_STATE.DELETING:
      return { label: STATE_DISPLAY.DELETING, tone: "danger" };
    case PROJECT_STATE.DRAFT:
      return { label: STATE_DISPLAY.DRAFT, tone: "warning" };
    case PROJECT_STATE.OPEN:
      return { label: STATE_DISPLAY.OPEN, tone: "info" };
    case PROJECT_STATE.SYNCED:
      return { label: STATE_DISPLAY.SYNCED, tone: "success" };
    case PROJECT_STATE.NEW:
    default:
      return { label: STATE_DISPLAY.NEW, tone: "neutral" };
  }
};

export const getProjectState = (project: Project): { label: string; tone: ProjectStateTone } => {
  // Ahead of drift: nothing checks a project being deleted, and it is going.
  if (project.pr_state === PROJECT_STATE.DELETING) {
    return getPrStateBadge(project);
  }
  const cachedDriftStatus = getCachedDriftStatus(project);
  if (projectHasDrift(project)) {
    return { label: "Drift Detected", tone: "danger" };
  }

  if (cachedDriftStatus === CHECK_FAILED) {
    return { label: "Needs Attention", tone: "warning" };
  }

  if (projectHasNeedsAttention(project)) {
    return { label: "Needs Sync", tone: "warning" };
  }

  return getPrStateBadge(project);
};

export const getDriftIndicator = (project: Project): { label: string; tone: ProjectStateTone } | null => {
  if (!hasCachedDriftSummary(project)) return null;

  const driftStatus = getCachedDriftStatus(project);
  if (driftStatus === "drifted") return { label: "Drift detected", tone: "danger" };
  if (driftStatus === CHECK_FAILED) return { label: "Needs attention", tone: "warning" };
  if (driftStatus === "unknown") return { label: "Not checked", tone: "neutral" };
  return null;
};

/** A project card's "Drift checked Oct 20 at 8:45 PM", from the last drift check; null when never checked. */
export const getDriftCheckedAtShortLabel = (project: Project): string | null => {
  const raw = (project as Project & Record<string, unknown>).last_drift_check_at;
  if (typeof raw !== "string" || !raw) return null;

  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return null;

  const day = date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const time = date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return `Drift checked ${day} at ${time}`;
};

export const getStateBadgeClasses = (tone: ProjectStateTone): string => {
  switch (tone) {
    case "danger":
      return "border-rose-500/30 bg-rose-500/10 text-rose-700 dark:border-rose-400/30 dark:bg-rose-400/10 dark:text-rose-200";
    case "warning":
      return "border-amber-500/30 bg-amber-500/10 text-amber-800 dark:border-amber-400/30 dark:bg-amber-400/10 dark:text-amber-200";
    case "success":
      return "border-emerald-500/30 bg-emerald-500/10 text-emerald-800 dark:border-emerald-400/30 dark:bg-emerald-400/10 dark:text-emerald-200";
    case "info":
      return "border-sky-500/30 bg-sky-500/10 text-sky-800 dark:border-sky-400/30 dark:bg-sky-400/10 dark:text-sky-200";
    case "neutral":
    default:
      return "border-slate-500/20 bg-slate-500/5 text-slate-700 dark:border-slate-400/20 dark:bg-slate-400/10 dark:text-slate-200";
  }
};

export const formatUpdatedAt = (project: Project): string => {
  const raw = project.updated_at || project.created_at;
  const date = raw ? new Date(raw) : null;

  if (!date || Number.isNaN(date.getTime())) return "—";

  const now = new Date();
  const includeYear = date.getFullYear() !== now.getFullYear();
  return new Intl.DateTimeFormat(undefined, includeYear
    ? { month: "short", day: "numeric", year: "numeric" }
    : { month: "short", day: "numeric" },
  ).format(date);
};
