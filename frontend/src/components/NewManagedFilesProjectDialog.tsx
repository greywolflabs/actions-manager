import React, { useEffect, useMemo, useState } from "react";
import { saveProject } from "../api/projects";
import type { ManagedFilesSection } from "../api/managedFiles";
import type { Plan } from "../api/user";
import { useNewProjectRepos } from "../hooks/useNewProjectData";
import { useTakenRepos } from "../hooks/useTakenRepos";
import { apiErrorMessage } from "../utils/apiErrorMessage";
import { repoMatchesVisibilityScope, type VisibilityScope } from "../utils/newProjectValidation";
import { toast } from "../utils/toast";
import RepositoryBranchSelector from "./RepositoryBranchSelector";
import VisibilityScopeOptions from "./VisibilityScopeOptions";
import { Button } from "./ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";
import { Input } from "./ui/input";

const FIELD_LABEL = "text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400";

/** Which of the section's project slots a new project takes. */
const slotSubtitle = (owned: number, limit: number | null): string | null => {
  if (limit === null) return null;
  if (owned + 1 === limit) return `This uses your last project (${limit} of ${limit}).`;
  return `This uses project ${owned + 1} of ${limit}.`;
};

const selectedCountLabel = (count: number): string =>
  `${count} ${count === 1 ? "repository" : "repositories"} selected`;

interface NewManagedFilesProjectDialogProps {
  readonly open: boolean;
  readonly user: string;
  readonly section: ManagedFilesSection;
  readonly plan?: Plan;
  readonly onClose: () => void;
  /** Called once the server has created the project. */
  readonly onCreated: (projectName: string) => void;
}

type FormProps = Omit<NewManagedFilesProjectDialogProps, "open">;

/**
 * The dialog's contents. Mounted only while the dialog is open, so each
 * opening starts from an empty form and the current repository list.
 */
const NewManagedFilesProjectForm: React.FC<FormProps> = ({ user, section, plan, onClose, onCreated }) => {
  const [name, setName] = useState("");
  const [kind, setKind] = useState(section.kinds[0]?.kind ?? "");
  const [visibility, setVisibility] = useState<VisibilityScope>("public");
  const [selected, setSelected] = useState<string[]>([]);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { repos, loading, error: reposError } = useNewProjectRepos(user, "standard", () => undefined);
  const scopedRepos = useMemo(
    () => repos.filter((repo) => repoMatchesVisibilityScope(repo, visibility)),
    [repos, visibility],
  );
  const scopedNames = useMemo(() => scopedRepos.map((repo) => repo.full_name || repo.name), [scopedRepos]);
  const { taken, error: takenError } = useTakenRepos(kind, scopedNames);

  // A repository another project of the kind turns out to hold can't stay picked.
  useEffect(() => {
    setSelected((previous) => previous.filter((repoName) => !taken.has(repoName)));
  }, [taken]);

  const subtitle = slotSubtitle(section.owned_count, section.limit);
  const canCreate = name.trim().length > 0 && selected.length > 0 && kind !== "" && !creating;

  const handleVisibilityChange = (next: VisibilityScope): void => {
    if (next === visibility) return;
    setVisibility(next);
    // The two scopes list different repositories.
    setSelected([]);
  };

  const handleCreate = async (): Promise<void> => {
    setCreating(true);
    setError(null);
    try {
      await saveProject({
        project_name: name.trim(),
        selected_repos: selected,
        workflows: [],
        rxworkflows: [],
        branch_regex: "",
        branch_option: "default",
        branch_max_age_days: 30,
        reusable_workflows_enabled: false,
        use_prefix: true,
        project_type: "managed_files",
        file_kind: kind,
        repository_visibility_scope: visibility,
      });
      toast.success(`Created ${name.trim()}.`);
      onCreated(name.trim());
    } catch (err) {
      setError(apiErrorMessage(err, "Couldn't create the project. Please try again."));
    } finally {
      setCreating(false);
    }
  };

  return (
    <>
      <DialogHeader>
        <DialogTitle>New Managed Files project</DialogTitle>
        {subtitle && (
          <DialogDescription data-testid="managed-files-slot">{subtitle}</DialogDescription>
        )}
      </DialogHeader>

      <div className="space-y-5">
        <div className="space-y-1.5">
          <label htmlFor="managed-files-project-name" className={FIELD_LABEL}>Project name</label>
          <Input
            id="managed-files-project-name"
            data-testid="managed-files-project-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Payments services"
          />
        </div>

        <div className="space-y-1.5">
          <label htmlFor="managed-files-kind" className={FIELD_LABEL}>File kind</label>
          <select
            id="managed-files-kind"
            data-testid="managed-files-kind"
            value={kind}
            onChange={(event) => setKind(event.target.value)}
            className="h-10 w-full rounded-md border border-slate-200 bg-white/80 px-3 text-sm text-slate-900 shadow-sm transition focus:outline-hidden focus:ring-2 focus:ring-sky-500/25 dark:border-slate-700 dark:bg-slate-950/40 dark:text-slate-100 scheme-light-dark"
          >
            {section.kinds.map((entry) => (
              <option key={entry.kind} value={entry.kind}>{entry.label}</option>
            ))}
          </select>
          <p className="text-xs text-slate-500 dark:text-slate-400">
            The file this project manages. It can&apos;t be changed after the project is created.
          </p>
        </div>

        <VisibilityScopeOptions
          visibilityScope={visibility}
          onChange={handleVisibilityChange}
          privateAllowedByTier={plan?.private_repositories ?? false}
          privateRepositoriesHelp={plan?.private_repositories_help}
          privateRefusedMessage={plan?.limit_messages.private_repositories}
        />

        <div className="space-y-1.5">
          <div className="flex items-baseline justify-between gap-3">
            <span className={FIELD_LABEL}>Repositories</span>
            <span className="text-xs text-slate-500 dark:text-slate-400" data-testid="managed-files-selected-count">
              {selectedCountLabel(selected.length)}
            </span>
          </div>
          <RepositoryBranchSelector
            compact
            availableRepositories={scopedRepos}
            selectedRepositoryNames={selected}
            unavailableRepos={taken}
            loading={loading}
            error={reposError}
            onSelectRepository={(repoName) => setSelected((previous) => [...previous, repoName])}
            onRemoveRepository={(repoName) => setSelected((previous) => previous.filter((r) => r !== repoName))}
            searchPlaceholder="Search repositories"
            resetSearchKey={visibility}
            data-testid="managed-files-repo-picker"
          />
          {takenError && (
            <p className="text-xs text-amber-700 dark:text-amber-300" data-testid="managed-files-taken-error">
              {takenError}
            </p>
          )}
        </div>

        {error && (
          <p role="alert" className="text-sm text-red-600 dark:text-red-400" data-testid="managed-files-create-error">
            {error}
          </p>
        )}
      </div>

      <DialogFooter className="gap-2">
        <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
        <Button
          type="button"
          onClick={() => void handleCreate()}
          disabled={!canCreate}
          data-testid="managed-files-create-button"
        >
          {creating ? "Creating…" : "Create project"}
        </Button>
      </DialogFooter>
    </>
  );
};

/** Creates a Managed Files project: a name, a file kind and the repositories it manages. */
const NewManagedFilesProjectDialog: React.FC<NewManagedFilesProjectDialogProps> = ({ open, onClose, ...form }) => (
  <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
    <DialogContent
      className="max-h-[90vh] max-w-2xl overflow-y-auto"
      data-testid="new-managed-files-dialog"
      // Without a limit there is no subtitle to describe the dialog.
      {...(slotSubtitle(form.section.owned_count, form.section.limit) ? {} : { "aria-describedby": undefined })}
    >
      {open && <NewManagedFilesProjectForm onClose={onClose} {...form} />}
    </DialogContent>
  </Dialog>
);

export default NewManagedFilesProjectDialog;
