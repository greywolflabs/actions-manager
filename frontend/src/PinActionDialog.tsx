import React, { useCallback, useEffect, useState } from "react";
import { Check, ChevronDown, Loader2 } from "lucide-react";
import { applyPin, previewPin, type PinPreview, type PinProjectChange, type PinWorkflowChange } from "./api/actionUsage";
import { Button } from "./components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "./components/ui/dialog";
import { cn } from "./lib/utils";
import { apiErrorMessage } from "./utils/apiErrorMessage";

const MUTED = "text-text-secondary dark:text-secondary-dark";
const PILL = "inline-flex shrink-0 items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] font-semibold";
const NOTE = "px-3 pb-2 text-xs text-amber-700 dark:text-amber-300";

/** How applying went in one project: under way, or what came of it. */
type Outcome = { state: "running" } | { state: "done" } | { state: "error"; message: string };

const plural = (count: number, one: string, many: string): string => `${count} ${count === 1 ? one : many}`;

const shortSha = (sha: string): string => sha.slice(0, 7);

/** The workflows an update changes: those without an open pull request. */
const updatable = (project: PinProjectChange): PinWorkflowChange[] => project.workflows.filter((workflow) => !workflow.blocked);

/** One side of a changed line. Indentation is dropped: it never changes, and it only crowds out the part that does. */
const ChangedLine: React.FC<Readonly<{ label: string; text: string; tint: string }>> = ({ label, text, tint }) => (
  <div className={cn("flex gap-3 px-2 py-0.5", tint)}>
    <span className={cn("w-10 shrink-0", MUTED)}>{label}</span>
    <span className="whitespace-pre">{text.trim()}</span>
  </div>
);

const WorkflowDiff: React.FC<Readonly<{ workflow: PinWorkflowChange }>> = ({ workflow }) => (
  <div className="mt-2" data-testid="pin-workflow">
    <code className="text-xs">{workflow.workflow_file}</code>
    {workflow.blocked && <p className="text-xs text-amber-700 dark:text-amber-300" data-testid="pin-blocked">{workflow.blocked} It is not updated.</p>}
    <div className="mt-1 overflow-x-auto rounded border border-slate-200 font-mono text-xs dark:border-slate-700">
      {workflow.lines.map((line) => (
        <React.Fragment key={line.line}>
          <ChangedLine label="Now" text={line.before} tint="bg-red-500/10" />
          <ChangedLine label="After" text={line.after} tint="bg-emerald-500/10" />
        </React.Fragment>
      ))}
    </div>
  </div>
);

const OutcomeBadge: React.FC<Readonly<{ outcome: Outcome | undefined }>> = ({ outcome }) => {
  if (!outcome) return null;
  if (outcome.state === "running") {
    return <span className={cn("inline-flex items-center gap-1 text-xs", MUTED)}><Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />Updating…</span>;
  }
  if (outcome.state === "done") {
    return (
      <span className={cn(PILL, "border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-700/70 dark:bg-emerald-950/40 dark:text-emerald-200")}>
        <Check className="h-3 w-3" aria-hidden="true" />Updated
      </span>
    );
  }
  return <span className={cn(PILL, "border-red-300 bg-red-50 text-red-800 dark:border-red-700/70 dark:bg-red-950/40 dark:text-red-200")}>Failed</span>;
};

interface ProjectRowProps {
  readonly project: PinProjectChange;
  readonly included: boolean;
  readonly expanded: boolean;
  readonly outcome: Outcome | undefined;
  readonly locked: boolean;
  readonly onInclude: (included: boolean) => void;
  readonly onExpand: () => void;
}

const ProjectRow: React.FC<ProjectRowProps> = ({ project, included, expanded, outcome, locked, onInclude, onExpand }) => (
  <li className="rounded-lg border border-border dark:border-border-dark" data-testid="pin-project">
    <div className="flex items-center gap-3 p-3">
      <input
        type="checkbox"
        checked={project.editable && included && updatable(project).length > 0}
        disabled={!project.editable || locked || updatable(project).length === 0}
        onChange={(event) => onInclude(event.target.checked)}
        aria-label={`Include ${project.project_name}`}
        data-testid="pin-include"
      />
      <div className="min-w-0 flex-1">
        <p className="truncate font-semibold">{project.project_name}</p>
        <p className={cn("text-xs", MUTED)}>{plural(updatable(project).length, "workflow", "workflows")}</p>
      </div>
      <OutcomeBadge outcome={outcome} />
      <Button type="button" variant="ghost" size="sm" onClick={onExpand} aria-expanded={expanded} data-testid="pin-show-changes">
        {expanded ? "Hide changes" : "Show changes"}
        <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", expanded && "rotate-180")} aria-hidden="true" />
      </Button>
    </div>
    {!project.editable && <p className={NOTE} data-testid="pin-not-editable">You can't change this project, so it is not included.</p>}
    {project.skipped_repos.length > 0 && (
      <p className={NOTE} data-testid="pin-skipped">
        These repositories have their own copy of the workflow and keep it: {project.skipped_repos.join(", ")}
      </p>
    )}
    {outcome?.state === "error" && <p role="alert" className="px-3 pb-2 text-xs text-red-600 dark:text-red-400">{outcome.message}</p>}
    {expanded && <div className="border-t border-border px-3 pb-3 dark:border-border-dark">{project.workflows.map((workflow) => <WorkflowDiff key={workflow.workflow_id} workflow={workflow} />)}</div>}
  </li>
);

interface TargetBarProps {
  readonly preview: PinPreview;
  readonly editing: boolean;
  readonly onToggle: () => void;
}

const TargetBar: React.FC<TargetBarProps> = ({ preview, editing, onToggle }) => (
  <div
    className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-slate-500/5 px-4 py-3 dark:border-border-dark"
    data-testid="pin-preview-result"
  >
    <div>
      <p className={cn("text-[11px] font-semibold uppercase tracking-wide", MUTED)}>
        {preview.source === "latest release" ? "Latest release" : "Selected version"}
      </p>
      <p className="font-mono text-sm font-semibold">
        {preview.ref} <span className={MUTED}>→</span> {shortSha(preview.sha)}
      </p>
    </div>
    <Button type="button" variant="ghost" size="sm" onClick={onToggle} data-testid="pin-change">
      {editing ? "Hide" : "Change"}
    </Button>
  </div>
);

interface RefFormProps {
  readonly value: string;
  readonly busy: boolean;
  readonly showLatest: boolean;
  readonly onChange: (value: string) => void;
  readonly onPreview: () => void;
  readonly onLatest: () => void;
}

const RefForm: React.FC<RefFormProps> = ({ value, busy, showLatest, onChange, onPreview, onLatest }) => (
  <form
    className="flex flex-wrap items-end gap-3"
    onSubmit={(event) => { event.preventDefault(); if (value.trim() && !busy) onPreview(); }}
  >
    <label className="flex flex-col gap-1 text-sm">
      <span>Tag, branch or commit SHA</span>
      <input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder="v5.0.0"
        data-testid="pin-ref"
        className="h-9 w-64 rounded-md border border-border bg-container px-3 dark:border-border-dark dark:bg-container-dark"
      />
    </label>
    <Button type="submit" variant="outline" disabled={busy || !value.trim()} data-testid="pin-preview">Preview</Button>
    {showLatest && (
      <Button type="button" variant="ghost" disabled={busy} onClick={onLatest} data-testid="pin-latest">Use latest release</Button>
    )}
  </form>
);

interface PinActionDialogProps {
  readonly action: string;
  readonly onClose: () => void;
  /** Called when a campaign has opened in any project, so the inventory can reload. */
  readonly onApplied: () => void;
}

/**
 * Update one action in every workflow that uses it (#2046). It opens on the action's latest
 * release already previewed, so the usual update is two clicks: choose which projects to
 * include, then update them one after the other, so a failure in one project leaves the rest
 * to go on. Each workflow is saved in its project as Commit Locally saves it; no pull
 * request is opened, so delivering the change stays the project's own Create Pull Requests.
 */
export default function PinActionDialog({ action, onClose, onApplied }: PinActionDialogProps): React.ReactElement {
  const [ref, setRef] = useState("");
  const [preview, setPreview] = useState<PinPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [editing, setEditing] = useState(false);
  const [included, setIncluded] = useState<Record<number, boolean>>({});
  const [expanded, setExpanded] = useState<Record<number, boolean>>({});
  const [outcomes, setOutcomes] = useState<Record<number, Outcome>>({});

  const choose = (value: string): void => {
    setRef(value);
    setPreview(null);
    setOutcomes({});
  };

  // No ref asks for the latest release, which is what almost everyone wants, so
  // the dialog opens already showing that change. Typing another ref overrides it.
  const showPreview = useCallback(async (requested?: string): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      const found = await previewPin(action, requested);
      setPreview(found);
      setRef(found.ref);
      setIncluded(Object.fromEntries(found.projects.map((project) => [project.project_id, project.editable])));
      setExpanded({});
      setOutcomes({});
      setEditing(false);
    } catch (error_) {
      setPreview(null);
      setError(apiErrorMessage(error_, "Couldn't preview the change."));
      setEditing(true);
    } finally {
      setBusy(false);
    }
  }, [action]);

  useEffect(() => {
    void showPreview();
  }, [showPreview]);

  const setOutcome = (projectId: number, outcome: Outcome): void =>
    setOutcomes((previous) => ({ ...previous, [projectId]: outcome }));

  const applyTo = async (found: PinPreview, project: PinProjectChange): Promise<void> => {
    setOutcome(project.project_id, { state: "running" });
    try {
      await applyPin({ project_id: project.project_id, action, sha: found.sha, comment: found.comment });
      setOutcome(project.project_id, { state: "done" });
    } catch (error_) {
      setOutcome(project.project_id, { state: "error", message: apiErrorMessage(error_, "Couldn't update the workflows.") });
    }
  };

  const nothingToChange = preview?.projects.length === 0;
  const hasProjects = (preview?.projects.length ?? 0) > 0;
  const chosen = preview?.projects.filter((project) => project.editable && included[project.project_id] && updatable(project).length > 0) ?? [];

  const applyAll = async (found: PinPreview): Promise<void> => {
    setBusy(true);
    for (const project of chosen) {
      await applyTo(found, project);
    }
    setBusy(false);
    // Whatever opened or saved shows in the inventory now, not after a reload.
    onApplied();
  };

  const finished = chosen.length > 0 && chosen.every((project) => outcomes[project.project_id]?.state === "done");
  const workflowCount = chosen.reduce((total, project) => total + updatable(project).length, 0);

  return (
    <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
      <DialogContent className="max-h-[85vh] max-w-2xl overflow-y-auto" data-testid="pin-dialog">
        <DialogHeader>
          <DialogTitle>Update {action} in all workflows</DialogTitle>
          <DialogDescription>
            Updates every workflow that uses it to one commit, by default the latest release, and saves each in its
            project as a local commit. No pull requests are opened: create them from a project when you are ready.
          </DialogDescription>
        </DialogHeader>

        {busy && !preview && !error && (
          <p className={cn("flex items-center gap-2 text-sm", MUTED)} data-testid="pin-looking-up">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            Looking up the latest release of {action}…
          </p>
        )}

        {preview && <TargetBar preview={preview} editing={editing} onToggle={() => setEditing((open) => !open)} />}

        {editing && (
          <RefForm
            value={ref}
            busy={busy}
            showLatest={preview?.source !== "latest release"}
            onChange={choose}
            onPreview={() => void showPreview(ref.trim())}
            onLatest={() => void showPreview()}
          />
        )}

        {error && <p role="alert" className="text-sm text-red-600 dark:text-red-400">{error}</p>}

        {nothingToChange && (
          <p className={cn("text-sm", MUTED)} data-testid="pin-nothing">
            Nothing to update: no workflow uses {action} at another version.
          </p>
        )}

        {preview && hasProjects && (
          <>
            <p className={cn("text-sm", MUTED)} data-testid="pin-summary">
              {plural(chosen.length, "project", "projects")} · {plural(workflowCount, "workflow", "workflows")} selected
            </p>
            <ul className="space-y-2">
              {preview.projects.map((project) => (
                <ProjectRow
                  key={project.project_id}
                  project={project}
                  included={Boolean(included[project.project_id])}
                  expanded={Boolean(expanded[project.project_id])}
                  outcome={outcomes[project.project_id]}
                  locked={busy}
                  onInclude={(value) => setIncluded((previous) => ({ ...previous, [project.project_id]: value }))}
                  onExpand={() => setExpanded((previous) => ({ ...previous, [project.project_id]: !previous[project.project_id] }))}
                />
              ))}
            </ul>
          </>
        )}

        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose} disabled={busy}>
            {finished ? "Done" : "Close"}
          </Button>
          {preview && hasProjects && !finished && (
            <Button type="button" onClick={() => void applyAll(preview)} disabled={busy || chosen.length === 0} data-testid="pin-apply">
              Update {plural(workflowCount, "workflow", "workflows")}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
