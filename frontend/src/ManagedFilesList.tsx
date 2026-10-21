import React, { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { Check, FileStack, FileText, FolderGit2, Info, Plus, RefreshCw } from "lucide-react";
import { listProjects, type Project } from "./api/projects";
import { fetchManagedFiles, type ManagedFilesSection } from "./api/managedFiles";
import type { Plan } from "./api/user";
import NewManagedFilesProjectDialog from "./components/NewManagedFilesProjectDialog";
import ProjectCardMenu from "./components/ProjectCardMenu";
import { Button } from "./components/ui/button";
import { Card, CardContent } from "./components/ui/card";
import { cn } from "./lib/utils";
import UsageMeter from "./components/UsageMeter";
import { apiErrorMessage } from "./utils/apiErrorMessage";
import { fileKindIcon } from "./utils/fileKinds";
import { NEUTRAL_PROJECT_STYLE } from "./utils/projectColors";
import {
  formatUpdatedAt,
  getDriftCheckedAtShortLabel,
  getDriftIndicator,
  getPrStateBadge,
  getStateBadgeClasses,
  normalizeProjectName,
} from "./utils/projectCardState";

const BADGE = "inline-flex shrink-0 items-center rounded-full border px-2 py-0.5 text-[11px] font-semibold tracking-wide";
const MUTED = "text-text-secondary dark:text-secondary-dark";

const repoCountLabel = (count: number): string => `${count} ${count === 1 ? "repository" : "repositories"}`;

/** Who changed the file on GitHub, from the repositories the last drift check flagged. */
const driftNote = (driftedRepos: string[]): string | null => {
  if (driftedRepos.length === 0) return null;
  const others = driftedRepos.length - 1;
  const who = others > 0 ? `${driftedRepos[0]} and ${others} more` : driftedRepos[0];
  return `${who} changed the file on GitHub`;
};

const meterLabel = (owned: number, limit: number | null): string =>
  limit === null ? `You own ${owned} (no limit)` : `You own ${owned} of ${limit}`;

const LimitMeter: React.FC<Readonly<{ owned: number; limit: number | null }>> = ({ owned, limit }) => (
  <UsageMeter
    label={meterLabel(owned, limit)}
    value={owned}
    max={limit}
    ariaLabel="Managed Files projects you own"
    testId="managed-files-meter"
  />
);

interface ManagedFilesCardProps {
  readonly project: Project;
  readonly kindLabel: string;
  /** The signed-in user: cards of anyone else's projects say who owns them. */
  readonly user: string;
  readonly onOpen: () => void;
}

const ManagedFilesCard: React.FC<ManagedFilesCardProps> = ({ project, kindLabel, user, onOpen }) => {
  const name = normalizeProjectName(project);
  const id = project.project_id ?? project.id;
  const state = getPrStateBadge(project);
  const drift = getDriftIndicator(project);
  const driftChecked = getDriftCheckedAtShortLabel(project);
  const note = driftNote(project.drifted_repos ?? []);
  const KindIcon = fileKindIcon(project.file_kind);
  const ownedByOther = Boolean(project.owner) && project.owner !== user;

  return (
    <div className="relative" data-testid={`managed-files-card-${id}`}>
      <button
        type="button"
        className="absolute inset-0 rounded-lg focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-slate-400"
        onClick={onOpen}
        aria-label={`Open project ${name}`}
      />
      <Card className="pointer-events-none relative h-full border-slate-200 bg-white/85 shadow-sm transition-shadow hover:border-slate-300 hover:shadow-md dark:border-slate-700/80 dark:bg-slate-900/70 dark:hover:border-slate-600">
        <CardContent className="p-0">
          <div className="flex items-start gap-3 p-4">
            <div
              data-testid={`managed-files-icon-${id}`}
              className={cn("flex h-10 w-10 shrink-0 items-center justify-center rounded-md", NEUTRAL_PROJECT_STYLE.iconBg)}
            >
              <KindIcon className={cn("h-5 w-5", NEUTRAL_PROJECT_STYLE.icon)} aria-hidden="true" />
            </div>
            <div className="min-w-0 flex-1">
              <span className="block min-w-0 truncate font-semibold text-text-primary dark:text-text-primary-dark">
                {name}
              </span>
              <span data-testid={`managed-files-kind-${id}`} className={cn("mt-0.5 block truncate text-xs", MUTED)}>
                {kindLabel}
              </span>
            </div>
            <div className="pointer-events-auto flex shrink-0 flex-wrap items-center justify-end gap-1.5">
              <span
                data-testid={`managed-files-status-${id}`}
                className={cn(BADGE, "gap-1", getStateBadgeClasses(state.tone))}
              >
                {state.tone === "success" && <Check className="h-3 w-3" aria-hidden="true" />}
                {state.label}
              </span>
              {drift && (
                <span data-testid={`managed-files-drift-${id}`} className={cn(BADGE, getStateBadgeClasses(drift.tone))}>
                  {drift.label}
                </span>
              )}
              <ProjectCardMenu project={project} onOpen={onOpen} />
            </div>
          </div>
          <div className={cn("space-y-2 border-t border-slate-200 px-4 py-3 text-xs dark:border-slate-700/80", MUTED)}>
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1" data-testid={`managed-files-repos-${id}`}>
              <span className="inline-flex items-center gap-1.5">
                <FolderGit2 className="h-3.5 w-3.5" aria-hidden="true" />
                {repoCountLabel(project.repo_count ?? 0)}
              </span>
              {project.file_path && (
                <span className="inline-flex min-w-0 items-center gap-1.5">
                  <FileText className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                  <code className="truncate font-mono">{project.file_path}</code>
                </span>
              )}
            </div>
            {note && (
              <p className="text-amber-700 dark:text-amber-300" data-testid={`managed-files-drift-note-${id}`}>
                {note}
              </p>
            )}
            {ownedByOther && (
              <p data-testid={`managed-files-owner-${id}`}>Owned by {project.owner}</p>
            )}
            <p>Updated {formatUpdatedAt(project)}</p>
            {driftChecked && (
              <p
                data-testid={`managed-files-drift-checked-${id}`}
                className="inline-flex items-center gap-1.5 text-text-secondary/80 dark:text-secondary-dark/80"
              >
                <RefreshCw className="h-3 w-3" aria-hidden="true" />
                {driftChecked}
              </p>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
};

const remainingLabel = (owned: number, limit: number | null): string | null =>
  limit === null ? null : `${Math.max(limit - owned, 0)} of ${limit} remaining`;

interface ManagedFilesListProps {
  readonly user: string;
  /** The signed-in user's plan, for which repository visibilities they may pick. */
  readonly plan?: Plan;
}

/**
 * The Managed Files section (#2367): projects that each manage one repository
 * file, such as CODEOWNERS. They are listed only here, never on the Projects page.
 */
export default function ManagedFilesList({ user, plan }: ManagedFilesListProps): React.ReactElement {
  const navigate = useNavigate();
  const [projects, setProjects] = useState<Project[]>([]);
  const [section, setSection] = useState<ManagedFilesSection | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);

  const load = useCallback(async (): Promise<void> => {
    try {
      const [list, info] = await Promise.all([listProjects("managed_files"), fetchManagedFiles()]);
      setProjects(list);
      setSection(info);
      setLoadError(null);
    } catch (error) {
      setLoadError(apiErrorMessage(error, "Couldn't load Managed Files."));
    } finally {
      setLoading(false);
    }
  }, [user]);

  useEffect(() => {
    void load();
  }, [load]);

  const owned = section?.owned_count ?? 0;
  const limit = section?.limit ?? null;
  const atLimit = limit !== null && owned >= limit;
  const canCreate = section !== null && !atLimit;
  const remaining = remainingLabel(owned, limit);
  const kindLabel = (kind: string | null | undefined): string =>
    section?.kinds.find((entry) => entry.kind === kind)?.label ?? kind ?? "";

  const openProject = (project: Project): void => {
    // The id tells the page which project it is when another owner's has the same name (#2240).
    navigate(`/project/${user}/${normalizeProjectName(project)}`, { state: { projectId: project.project_id } });
  };

  const handleCreated = (): void => {
    setDialogOpen(false);
    // Counted at once, so the slot and the limit are right before the refetch lands.
    setSection((previous) => previous && { ...previous, owned_count: previous.owned_count + 1 });
    void load();
  };

  const newProjectButton = (testId: string): React.ReactElement => (
    <Button
      type="button"
      onClick={() => setDialogOpen(true)}
      disabled={!canCreate}
      title={atLimit ? section?.limit_message ?? undefined : undefined}
      data-testid={testId}
      className="inline-flex items-center gap-1.5"
    >
      <Plus className="h-4 w-4" aria-hidden="true" />
      New project
    </Button>
  );

  return (
    <div className="w-full" data-testid="managed-files-list">
      <div className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 className="text-3xl font-bold text-gray-900 dark:text-white">Managed Files</h2>
          <p className="mt-2 max-w-2xl text-sm text-gray-600 dark:text-slate-300">
            Repository files that one project owns per repository. Edit a file once and deliver it to every
            repository in the project with a pull request campaign.
          </p>
        </div>
        {section && (
          <div className="flex flex-col gap-2 sm:items-end">
            <div className="flex items-center gap-4">
              <LimitMeter owned={owned} limit={limit} />
              {newProjectButton("new-managed-files-project-button")}
            </div>
            {atLimit && section.limit_message && (
              <p className="text-xs text-amber-700 dark:text-amber-300" data-testid="managed-files-limit-message">
                {section.limit_message}
              </p>
            )}
          </div>
        )}
      </div>

      <div
        className="mb-6 flex items-start gap-2 rounded-lg border border-slate-200 bg-slate-50/60 p-3 text-sm text-slate-600 dark:border-slate-700/80 dark:bg-slate-900/40 dark:text-slate-300"
        data-testid="managed-files-info"
      >
        <Info className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
        <p>
          Each repository can belong to one project per file kind. Repositories already in a project are greyed
          out when you choose repositories.
        </p>
      </div>

      {loading && <p className={cn("text-sm", MUTED)}>Loading Managed Files…</p>}
      {loadError && (
        <p role="alert" className="text-sm text-red-600 dark:text-red-400">{loadError}</p>
      )}

      {!loading && !loadError && projects.length === 0 && (
        <div
          className="flex flex-col items-center gap-3 rounded-lg border border-dashed border-slate-300 px-6 py-12 text-center dark:border-slate-700"
          data-testid="managed-files-empty"
        >
          <div className={cn("flex h-12 w-12 items-center justify-center rounded-md", NEUTRAL_PROJECT_STYLE.iconBg)}>
            <FileStack className={cn("h-6 w-6", NEUTRAL_PROJECT_STYLE.icon)} aria-hidden="true" />
          </div>
          <p className="font-semibold text-text-primary dark:text-text-primary-dark">No Managed Files projects yet</p>
          <p className={cn("max-w-md text-sm", MUTED)}>
            Create a project to manage one file, such as CODEOWNERS, across a group of repositories.
          </p>
          {newProjectButton("managed-files-empty-new-button")}
        </div>
      )}

      {projects.length > 0 && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {projects.map((project) => (
            <ManagedFilesCard
              key={project.project_id ?? project.id}
              project={project}
              kindLabel={kindLabel(project.file_kind)}
              user={user}
              onOpen={() => openProject(project)}
            />
          ))}
          {canCreate && (
            <button
              type="button"
              onClick={() => setDialogOpen(true)}
              data-testid="managed-files-new-card"
              className="flex min-h-40 flex-col items-center justify-center gap-1 rounded-lg border-2 border-dashed border-slate-300 text-slate-600 transition-colors hover:border-slate-400 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:border-slate-500 dark:hover:bg-slate-900/40"
            >
              <Plus className="h-5 w-5" aria-hidden="true" />
              <span className="font-semibold">New project</span>
              {remaining && <span className="text-xs text-slate-500 dark:text-slate-400">{remaining}</span>}
            </button>
          )}
        </div>
      )}

      {section && (
        <NewManagedFilesProjectDialog
          open={dialogOpen}
          user={user}
          section={section}
          plan={plan}
          onClose={() => setDialogOpen(false)}
          onCreated={handleCreated}
        />
      )}
    </div>
  );
}
