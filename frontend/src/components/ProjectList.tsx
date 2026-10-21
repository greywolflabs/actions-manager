/* eslint-disable no-restricted-syntax -- Legacy: TODO migrate inline styles to Tailwind CSS classes */
import React, { useMemo, useState } from "react";
import { useNavigate } from "react-router";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  Folder,
  FolderKanban,
  GitPullRequest,
  Globe,
  GripVertical,
  Lock,
  type LucideIcon,
  PencilLine,
  Plus,
  RefreshCw,
  SlidersHorizontal,
  Tag,
  Workflow,
} from "lucide-react";
import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  arrayMove,
  rectSortingStrategy,
  sortableKeyboardCoordinates,
  useSortable,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { Project } from "../api/projects";
import { getProjectTypeConfig } from "../utils/projectTypeConfig";
import { PROJECT_COLOR_STYLES, normalizeProjectColorKey } from "../utils/projectColors";
import { cn } from "../lib/utils";
import { Card, CardContent } from "./ui/card";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Label } from "./ui/label";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "./ui/dropdown-menu";
import ProjectCardMenu from "./ProjectCardMenu";
import {
  PROJECT_STATE,
  STATE_DISPLAY,
  type ProjectStateTone,
  formatUpdatedAt,
  getDriftCheckedAtShortLabel,
  getDriftIndicator,
  getProjectState,
  getProjectWorkflows,
  getStateBadgeClasses,
  normalizeProjectName,
  projectHasNeedsAttention,
  projectHasWorkflowStatus,
} from "../utils/projectCardState";

// Props interface for the ProjectList component
interface ProjectListProps {
  user?: string;
  projects?: Project[];
  onCreateProject?: () => void;
  isCreateProjectDisabled?: boolean;
  /**
   * Persist a manual card order (issue #1804). Receives the complete list of
   * project IDs in their new order — the backend rejects partial lists, which
   * is why reordering is disabled while a filter is active.
   */
  onReorder?: (orderedIds: number[]) => void;
  /** Message shown when a reorder failed to save and the order was rolled back. */
  reorderError?: string | null;
  /** The plan usage meter, shown beside New Project; the parent owns the counts. */
  planUsage?: React.ReactNode;
  /** The "2 of 3 remaining" line under the New project box; omitted when the plan has no limit. */
  newProjectRemaining?: string | null;
}

/**
 * Sortable wrapper around one project card (issue #1804).
 *
 * Exists because useSortable is a hook and so cannot be called inside the
 * projects .map(). It renders the same outer element the card always had, so
 * the grid remains the card's direct parent.
 *
 * The handle is passed back via a render prop because it has to live inside the
 * card's one `pointer-events-auto` region — the card body is
 * `pointer-events-none` beneath a full-bleed navigation button, so a handle
 * anywhere else would be unclickable.
 */
const SortableProjectCard: React.FC<{
  sortId: number;
  testId: string;
  disabled: boolean;
  disabledReason: string;
  children: (dragHandle: React.ReactNode) => React.ReactNode;
}> = ({ sortId, testId, disabled, disabledReason, children }) => {
  const {
    attributes,
    listeners,
    setNodeRef,
    setActivatorNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: sortId, disabled });

  const dragHandle = (
    <button
      type="button"
      ref={setActivatorNodeRef}
      {...attributes}
      {...listeners}
      disabled={disabled}
      // Dragging must never open the project sitting underneath.
      onClick={(e) => e.stopPropagation()}
      title={disabled ? disabledReason : "Drag to reorder"}
      aria-label={disabled ? disabledReason : "Reorder project"}
      data-testid={`project-drag-handle-${testId}`}
      className={cn(
        "flex h-8 w-8 items-center justify-center rounded border border-transparent text-slate-400",
        // Out of the way until the card is hovered or the handle focused; always there on touch screens.
        "opacity-0 transition-opacity focus-visible:opacity-100 group-hover:opacity-100 pointer-coarse:opacity-100",
        disabled
          ? "cursor-not-allowed group-hover:opacity-40 pointer-coarse:opacity-40"
          : "cursor-grab hover:border-slate-300 hover:bg-slate-100 hover:text-slate-700 active:cursor-grabbing dark:hover:border-slate-600 dark:hover:bg-slate-800 dark:hover:text-slate-100",
      )}
    >
      <GripVertical className="h-4 w-4" aria-hidden="true" />
    </button>
  );

  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      data-testid={`project-row-${testId}`}
      className={cn("group relative", isDragging && "z-10 opacity-80 ring-2 ring-slate-400 rounded-lg")}
    >
      {children(dragHandle)}
    </div>
  );
};

const REORDER_DISABLED_REASON = "Clear filters to reorder projects";

const LOCAL_CHANGE_STATUS_VALUES = new Set([
  "committed_locally",
  "draft",
  "local_changes",
  "new",
  "unsaved",
]);

const UNDER_REVIEW_STATUS_VALUES = new Set([
  "open",
  "under_review",
]);

const SYNCED_STATUS_VALUES = new Set([
  "synced",
  "synced_with_github",
]);

const getProjectWorkflowCount = (project: Project): number => {
  const count = project.workflow_count;
  if (typeof count === "number" && Number.isFinite(count) && count >= 0) return count;
  return getProjectWorkflows(project).length;
};

const formatProjectMeta = (project: Project): string | null => {
  const repoCount = project.selected_repos?.length ?? 0;
  const parts: string[] = [];

  if (repoCount > 0) parts.push(`${repoCount} repo${repoCount === 1 ? "" : "s"}`);

  return parts.length > 0 ? parts.join(" · ") : null;
};

// Single set of categories drives both the compact status pills and the Status select,
// so the two controls can never fall out of sync with each other.
type StatusFilterValue = "all" | "synced" | "draft" | "review" | "needs_attention";

const STATUS_FILTER_OPTIONS: Array<{
  value: StatusFilterValue;
  label: string;
  tone: ProjectStateTone;
  icon: LucideIcon;
}> = [
  { value: "all", label: "All", tone: "neutral", icon: FolderKanban },
  { value: "synced", label: "Synced", tone: "success", icon: CheckCircle2 },
  { value: "draft", label: "Draft / Local Changes", tone: "warning", icon: PencilLine },
  { value: "review", label: "Under Review", tone: "info", icon: GitPullRequest },
  { value: "needs_attention", label: "Needs Attention", tone: "danger", icon: AlertTriangle },
];

const matchesStatusFilter = (
  project: Project,
  filter: StatusFilterValue,
  needsAttention: boolean,
): boolean => {
  if (filter === "all") return true;
  if (filter === "needs_attention") return needsAttention;
  if (needsAttention) return false; // same isOperational gate the summary cards used

  if (filter === "synced") return getProjectState(project).label === STATE_DISPLAY.SYNCED;

  if (filter === "draft") {
    return project.pr_state === PROJECT_STATE.DRAFT
      || project.pr_state === PROJECT_STATE.NEW
      || projectHasWorkflowStatus(project, LOCAL_CHANGE_STATUS_VALUES);
  }

  // A project being deleted is waiting on its removal pull requests' review.
  return project.pr_state === PROJECT_STATE.OPEN
    || project.pr_state === PROJECT_STATE.DELETING
    || projectHasWorkflowStatus(project, UNDER_REVIEW_STATUS_VALUES);
};

const ProjectList: React.FC<ProjectListProps> = ({
    user,
    projects = [],
    onCreateProject,
    isCreateProjectDisabled = false,
    onReorder,
    reorderError = null,
    planUsage,
    newProjectRemaining = null,
}) => {
  const navigate = useNavigate(); // ✅ React Router Navigation
  const [searchQuery, setSearchQuery] = useState<string>("");
  const [projectTypeFilter, setProjectTypeFilter] = useState<"all" | "standard" | "rwx">("all");
  const [visibilityFilter, setVisibilityFilter] = useState<"all" | "public" | "private">("all");
  const [namingModeFilter, setNamingModeFilter] = useState<"all" | "prefix" | "no_prefix">("all");
  const [statusFilter, setStatusFilter] = useState<StatusFilterValue>("all");

  const handleLoadProject = (projectName: string, projectId?: number | string): void => {
    console.log("📌 Debug: Loading Project:", projectName);

    if (!user) {
      console.error("❌ Error: GitHub user is missing!");
      return;
    }

    // Navigate to the project URL; the parent's useEffect will trigger
    // loadProjectFromAPI (which has a staleness guard) to load the data.
    // The id tells the page which project it is when another owner's has the same name (#2240).
    navigate(`/project/${user}/${projectName}`, { state: { projectId } });
  };

  const filtersActive = (
    searchQuery.trim().length > 0
    || projectTypeFilter !== "all"
    || visibilityFilter !== "all"
    || namingModeFilter !== "all"
    || statusFilter !== "all"
  );

  // A filtered grid shows a subset, and the backend only accepts a complete
  // order — saving from here would drop the hidden projects.
  const sensors = useSensors(
    // A small distance keeps a click on the handle from registering as a drag.
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const clearFilters = (): void => {
    setSearchQuery("");
    setProjectTypeFilter("all");
    setVisibilityFilter("all");
    setNamingModeFilter("all");
    setStatusFilter("all");
  };

  const needsAttentionProjectCodes = useMemo(() => new Set(
    projects
      .filter(projectHasNeedsAttention)
      .map((project) => project.project_code),
  ), [projects]);

  const statusCounts = useMemo(() => {
    const counts = {} as Record<StatusFilterValue, number>;
    STATUS_FILTER_OPTIONS.forEach(({ value }) => {
      counts[value] = projects.filter((project) => matchesStatusFilter(
        project,
        value,
        needsAttentionProjectCodes.has(project.project_code),
      )).length;
    });
    return counts;
  }, [projects, needsAttentionProjectCodes]);

  const filteredProjects = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();

    return projects.filter((project) => {
      const name = normalizeProjectName(project);
      const projectType = project.project_type ?? "standard";
      const visibility = project.repository_visibility_scope ?? "public";
      const usePrefix = project.use_prefix !== false;

      if (query && !name.toLowerCase().includes(query)) return false;
      if (projectTypeFilter !== "all" && projectType !== projectTypeFilter) return false;
      if (visibilityFilter !== "all" && visibility !== visibilityFilter) return false;
      if (namingModeFilter !== "all") {
        const namingMode = usePrefix ? "prefix" : "no_prefix";
        if (namingMode !== namingModeFilter) return false;
      }
      if (!matchesStatusFilter(project, statusFilter, needsAttentionProjectCodes.has(project.project_code))) {
        return false;
      }
      return true;
    });
  }, [projects, searchQuery, projectTypeFilter, visibilityFilter, namingModeFilter, statusFilter, needsAttentionProjectCodes]);

  const sortableIds = useMemo(
    () => filteredProjects.map((p) => Number(p.project_id ?? p.id)),
    [filteredProjects],
  );

  const handleDragEnd = (event: DragEndEvent): void => {
    const { active, over } = event;
    if (!over || active.id === over.id || filtersActive) return;

    const oldIndex = sortableIds.indexOf(Number(active.id));
    const newIndex = sortableIds.indexOf(Number(over.id));
    if (oldIndex === -1 || newIndex === -1) return;

    // Reorder the full project list, not the filtered view — the backend
    // requires every accessible id. Reordering is disabled while filtering, so
    // filteredProjects and projects hold the same members here.
    const allIds = projects.map((p) => Number(p.project_id ?? p.id));
    const fromIndex = allIds.indexOf(Number(active.id));
    const toIndex = allIds.indexOf(Number(over.id));
    if (fromIndex === -1 || toIndex === -1) return;

    onReorder?.(arrayMove(allIds, fromIndex, toIndex));
  };

  const newProjectCard = onCreateProject && !isCreateProjectDisabled && (
    <button
      type="button"
      onClick={onCreateProject}
      data-testid="projects-new-card"
      className="flex min-h-40 flex-col items-center justify-center gap-1 rounded-lg border-2 border-dashed border-slate-300 text-slate-600 transition-colors hover:border-slate-400 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:border-slate-500 dark:hover:bg-slate-900/40"
    >
      <Plus className="h-5 w-5" aria-hidden="true" />
      <span className="font-semibold">New project</span>
      {newProjectRemaining && <span className="text-xs text-slate-500 dark:text-slate-400">{newProjectRemaining}</span>}
    </button>
  );

  return (
    <div className="w-full space-y-6">

      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between" data-testid="projects-heading-row">
        <div>
          <h2 className="text-3xl font-bold text-gray-900 dark:text-white">Projects</h2>
          <p className="mt-2 max-w-2xl text-sm text-gray-600 dark:text-slate-300">
            Manage GitHub Actions across repository groups.
          </p>
        </div>
        {(planUsage || onCreateProject) && (
          <div className="flex flex-wrap items-center gap-4">
            {planUsage}
            {onCreateProject && (
              <Button
                type="button"
                onClick={onCreateProject}
                disabled={isCreateProjectDisabled}
                data-testid="new-project-button"
                className="inline-flex items-center gap-1.5"
              >
                <Plus className="h-4 w-4" aria-hidden="true" />
                New Project
              </Button>
            )}
          </div>
        )}
      </div>

      <section aria-label="Project status summary" className="flex flex-wrap gap-2">
        {STATUS_FILTER_OPTIONS.map(({ value, label, tone, icon: Icon }) => {
          const isActive = statusFilter === value;
          return (
            <button
              key={value}
              type="button"
              data-testid={`project-status-pill-${value}`}
              aria-pressed={isActive}
              onClick={() => setStatusFilter(isActive ? "all" : value)}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-semibold transition-colors",
                isActive
                  ? getStateBadgeClasses(tone)
                  : "border-slate-200 bg-white/70 text-slate-600 hover:bg-slate-100 dark:border-slate-700/80 dark:bg-slate-900/40 dark:text-slate-300 dark:hover:bg-slate-800",
              )}
            >
              <Icon className="h-3.5 w-3.5" aria-hidden="true" />
              {label}
              <span className="tabular-nums opacity-80">{statusCounts[value]}</span>
            </button>
          );
        })}
      </section>

      <div
        className="rounded-lg border border-slate-200 bg-slate-50/60 p-4 shadow-sm dark:border-slate-700/80 dark:bg-slate-900/40"
        data-testid="projects-toolbar"
      >
        <div className="flex flex-col gap-4 xl:flex-row xl:items-end xl:justify-between">
          <div className="flex flex-col gap-4 md:flex-row md:flex-wrap md:items-end md:gap-3">
            <div className="w-full md:w-[280px] md:flex-none">
              <Label htmlFor="project-search" className="text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                Search
              </Label>
              <Input
                id="project-search"
                data-testid="project-search-input"
                placeholder="Search projects..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="bg-white/80 text-slate-900 shadow-sm placeholder:text-slate-400 dark:bg-slate-950/40 dark:text-slate-100 dark:placeholder:text-slate-500"
              />
            </div>

            <div className="w-full md:w-[180px] md:flex-none">
              <Label htmlFor="project-type-filter" className="text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                Type
              </Label>
              <select
                id="project-type-filter"
                data-testid="project-type-filter"
                value={projectTypeFilter}
                onChange={(e) => setProjectTypeFilter(e.target.value as typeof projectTypeFilter)}
                className="h-10 w-full rounded-md border border-slate-200 bg-white/80 px-3 text-sm text-slate-900 shadow-sm transition focus:outline-hidden focus:ring-2 focus:ring-sky-500/25 dark:border-slate-700 dark:bg-slate-950/40 dark:text-slate-100"
                style={{ colorScheme: "light dark" }}
              >
                <option value="all">All</option>
                <option value="standard">Standard</option>
                <option value="rwx">Reusable (RWX)</option>
              </select>
            </div>

            <div className="w-full md:w-[200px] md:flex-none">
              <Label htmlFor="project-status-filter" className="text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                Status
              </Label>
              <select
                id="project-status-filter"
                data-testid="project-status-filter"
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value as StatusFilterValue)}
                className="h-10 w-full rounded-md border border-slate-200 bg-white/80 px-3 text-sm text-slate-900 shadow-sm transition focus:outline-hidden focus:ring-2 focus:ring-sky-500/25 dark:border-slate-700 dark:bg-slate-950/40 dark:text-slate-100"
                style={{ colorScheme: "light dark" }}
              >
                {STATUS_FILTER_OPTIONS.map(({ value, label }) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </select>
            </div>

            <div className="w-full md:w-auto md:flex-none">
              <span className="block text-[11px] font-semibold uppercase tracking-wide text-transparent select-none">
                More
              </span>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    type="button"
                    variant="outline"
                    data-testid="more-filters-trigger"
                    className="inline-flex h-10 items-center gap-1.5"
                  >
                    <SlidersHorizontal className="h-4 w-4" aria-hidden="true" />
                    More Filters
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent
                  align="start"
                  className="w-64 p-3"
                  onCloseAutoFocus={(e) => e.preventDefault()}
                >
                  <div className="space-y-3">
                    <div>
                      <Label htmlFor="project-visibility-filter" className="text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                        Visibility
                      </Label>
                      <select
                        id="project-visibility-filter"
                        data-testid="project-visibility-filter"
                        value={visibilityFilter}
                        onChange={(e) => setVisibilityFilter(e.target.value as typeof visibilityFilter)}
                        className="h-10 w-full rounded-md border border-slate-200 bg-white/80 px-3 text-sm text-slate-900 shadow-sm transition focus:outline-hidden focus:ring-2 focus:ring-sky-500/25 dark:border-slate-700 dark:bg-slate-950/40 dark:text-slate-100"
                        style={{ colorScheme: "light dark" }}
                      >
                        <option value="all">All</option>
                        <option value="public">Public</option>
                        <option value="private">Private</option>
                      </select>
                    </div>

                    <div>
                      <Label htmlFor="project-naming-mode-filter" className="text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                        Naming Mode
                      </Label>
                      <select
                        id="project-naming-mode-filter"
                        data-testid="project-naming-mode-filter"
                        value={namingModeFilter}
                        onChange={(e) => setNamingModeFilter(e.target.value as typeof namingModeFilter)}
                        className="h-10 w-full rounded-md border border-slate-200 bg-white/80 px-3 text-sm text-slate-900 shadow-sm transition focus:outline-hidden focus:ring-2 focus:ring-sky-500/25 dark:border-slate-700 dark:bg-slate-950/40 dark:text-slate-100"
                        style={{ colorScheme: "light dark" }}
                      >
                        <option value="all">All</option>
                        <option value="prefix">Prefix</option>
                        <option value="no_prefix">No Prefix</option>
                      </select>
                    </div>
                  </div>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          </div>

          <div className="flex flex-col items-start gap-2 sm:flex-row sm:items-center xl:flex-col xl:items-end">
            <p className="text-sm text-slate-600 dark:text-slate-300" data-testid="projects-filtered-count">
              Showing <span className="font-medium text-slate-900 dark:text-slate-100">{filteredProjects.length}</span> of{" "}
              <span className="font-medium text-slate-900 dark:text-slate-100">{projects.length}</span>
            </p>
            {filtersActive && (
              <Button
                variant="outline"
                size="sm"
                onClick={clearFilters}
                data-testid="clear-project-filters"
                className="border-slate-300/70 text-slate-700 hover:bg-slate-100 dark:border-slate-600/80 dark:text-slate-200 dark:hover:bg-slate-800"
              >
                Clear Filters
              </Button>
            )}
          </div>
        </div>
      </div>

      {projects.length === 0 && (
        <div className="text-center py-12 px-6">
          <p className="text-text-secondary dark:text-secondary-dark">
            No saved projects yet.
          </p>
        </div>
      )}
      {projects.length === 0 && newProjectCard && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">{newProjectCard}</div>
      )}
      {projects.length > 0 && filteredProjects.length === 0 && (
        <div className="py-10 px-6 text-center">
          <p className="text-sm text-muted-foreground">
            No projects match your current search and filters.
          </p>
          {filtersActive && (
            <div className="mt-4">
              <Button variant="outline" size="sm" onClick={clearFilters} data-testid="clear-project-filters-empty">
                Clear Filters
              </Button>
            </div>
          )}
        </div>
      )}
      {reorderError && (
        <div
          role="alert"
          data-testid="project-reorder-error"
          className="mx-1 mb-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800 dark:border-red-800 dark:bg-red-900/20 dark:text-red-200"
        >
          {reorderError}
        </div>
      )}

      {filteredProjects.length > 0 && (
        <DndContext
          sensors={sensors}
          collisionDetection={closestCenter}
          onDragEnd={handleDragEnd}
        >
          <SortableContext items={sortableIds} strategy={rectSortingStrategy}>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {filteredProjects
            .map((project) => {
              const projectName = normalizeProjectName(project);
              const usePrefix = project.use_prefix !== false;
              const isPrivateScope = (project.repository_visibility_scope ?? "public").toString().toLowerCase() === "private";
              const projectTypeLabel = getProjectTypeConfig(project.project_type).label;
              const derivedState = getProjectState(project);
              const projectMeta = formatProjectMeta(project);
              const workflowCount = getProjectWorkflowCount(project);
              const workflowLabel = workflowCount === 0
                ? "No workflows"
                : `${workflowCount} workflow${workflowCount === 1 ? "" : "s"}`;
              const workflowLabelClasses = workflowCount === 0
                ? "text-slate-500 dark:text-slate-400"
                : "text-text-secondary dark:text-secondary-dark";
              const driftIndicator = getDriftIndicator(project);
              const driftCheckedAtLabel = getDriftCheckedAtShortLabel(project);
              const driftStatusNote = project.drift_error_summary || null;
              const projectColorKey = normalizeProjectColorKey(project.project_color);
              const projectColorStyles = PROJECT_COLOR_STYLES[projectColorKey];
              const rowId = project.project_id ?? project.id ?? project.project_code;

              return (
                <SortableProjectCard
                  key={project.project_id ?? project.id}
                  sortId={Number(project.project_id ?? project.id)}
                  testId={projectName || project.project_code || String(project.project_id ?? project.id)}
                  disabled={filtersActive}
                  disabledReason={REORDER_DISABLED_REASON}
                >
                  {(dragHandle) => (
                    <>
                  <button
                    type="button"
                    className="absolute inset-0 rounded-lg focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-slate-400"
                    onClick={() => handleLoadProject(projectName, project.project_id ?? project.id)}
                    aria-label={`Open project ${projectName || project.project_code || "project"}`}
                  />
                  <Card className="pointer-events-none relative h-full border-slate-200 bg-white/85 shadow-sm transition-shadow hover:border-slate-300 hover:shadow-md dark:border-slate-700/80 dark:bg-slate-900/70 dark:hover:border-slate-600">
                    <CardContent className="p-0">
                      <div className="flex items-start gap-3 p-4">
                        <div
                          data-testid={`project-icon-${rowId}`}
                          className={cn("flex h-10 w-10 shrink-0 items-center justify-center rounded-md", projectColorStyles.iconBg)}
                        >
                          <Folder className={cn("h-5 w-5", projectColorStyles.icon)} aria-hidden="true" />
                        </div>
                        <div className="min-w-0 flex-1">
                          <span className="block min-w-0 truncate font-semibold text-text-primary dark:text-text-primary-dark">
                            {projectName || "—"}
                          </span>
                          <span
                            aria-label={`Project type: ${projectTypeLabel}`}
                            className="mt-0.5 block truncate text-xs text-text-secondary dark:text-secondary-dark"
                          >
                            {projectTypeLabel}
                          </span>
                        </div>
                        <div className="pointer-events-auto flex shrink-0 flex-wrap items-center justify-end gap-1.5">
                          <span
                            data-testid={`project-status-${rowId}`}
                            className={`inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-semibold tracking-wide ${getStateBadgeClasses(derivedState.tone)}`}
                            title={derivedState.label}
                          >
                            {derivedState.tone === "success" && <Check className="h-3 w-3" aria-hidden="true" />}
                            {derivedState.label}
                          </span>
                          {driftIndicator && (
                            <span
                              data-testid={`project-drift-indicator-${rowId}`}
                              className={`inline-flex items-center rounded-full border px-1.5 py-0.5 text-[10px] font-semibold tracking-wide ${getStateBadgeClasses(driftIndicator.tone)}`}
                            >
                              {driftIndicator.label}
                            </span>
                          )}
                          {dragHandle}
                          <ProjectCardMenu
                            project={project}
                            onOpen={() => handleLoadProject(projectName, project.project_id ?? project.id)}
                          />
                        </div>
                      </div>
                      <div className="space-y-2 border-t border-slate-200 px-4 py-3 text-xs text-text-secondary dark:border-slate-700/80 dark:text-secondary-dark">
                        <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
                          <span data-testid={`project-workflows-${rowId}`} className={`inline-flex items-center gap-1.5 ${workflowLabelClasses}`}>
                            <Workflow className="h-3.5 w-3.5" aria-hidden="true" />
                            {workflowLabel}
                          </span>
                          <span data-testid={`project-scope-${rowId}`} className="inline-flex flex-wrap items-center gap-x-4 gap-y-1">
                            <span className="inline-flex items-center gap-1.5">
                              {isPrivateScope ? <Lock className="h-3.5 w-3.5" aria-hidden="true" /> : <Globe className="h-3.5 w-3.5" aria-hidden="true" />}
                              {isPrivateScope ? "Private repos" : "Public repos"}
                            </span>
                            <span className="inline-flex items-center gap-1.5">
                              <Tag className="h-3.5 w-3.5" aria-hidden="true" />
                              {usePrefix ? "Prefix" : "No prefix"}
                            </span>
                          </span>
                        </div>
                        <p>
                          Updated {formatUpdatedAt(project)}
                          {project.last_modified_by ? ` by ${project.last_modified_by}` : ""}
                          {projectMeta ? ` · ${projectMeta}` : ""}
                        </p>
                        {driftCheckedAtLabel && (
                          <p
                            data-testid={`project-drift-checked-${rowId}`}
                            className="inline-flex items-center gap-1.5 text-text-secondary/80 dark:text-secondary-dark/80"
                          >
                            <RefreshCw className="h-3 w-3" aria-hidden="true" />
                            {driftCheckedAtLabel}
                          </p>
                        )}
                        {driftStatusNote && (
                          <p
                            data-testid={`project-drift-reason-${rowId}`}
                            title={driftStatusNote}
                            className="line-clamp-2 text-[11px] text-amber-700 dark:text-amber-300"
                          >
                            {driftStatusNote}
                          </p>
                        )}
                      </div>
                    </CardContent>
                  </Card>
                    </>
                  )}
                </SortableProjectCard>
              );
            })}
          {newProjectCard}
        </div>
          </SortableContext>
        </DndContext>
      )}
    </div>
  );
};

export default ProjectList;
