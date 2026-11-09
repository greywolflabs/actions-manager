import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { Check, ChevronDown, ChevronRight, Info, Package, Search, ShieldAlert } from "lucide-react";
import {
  fetchActionUsage,
  fetchLatestVersion,
  updateWorkflow,
  type ActionUsage,
  type ActionUsageAction,
  type ActionUsageEntry,
  type ActionVersion,
  type LatestVersion,
} from "./api/actionUsage";
import PinActionDialog from "./PinActionDialog";
import { Button } from "./components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "./components/ui/dropdown-menu";
import { cn } from "./lib/utils";
import { apiErrorMessage } from "./utils/apiErrorMessage";
import { NEUTRAL_PROJECT_STYLE } from "./utils/projectColors";
import { toast } from "./utils/toast";

const MUTED = "text-text-secondary dark:text-secondary-dark";
const PILL = "inline-flex shrink-0 items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] font-semibold tracking-wide";
const AMBER = "border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-700/70 dark:bg-amber-950/40 dark:text-amber-200";
const GREEN = "border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-700/70 dark:bg-emerald-950/40 dark:text-emerald-200";
const QUIET = "border-slate-300 text-slate-600 dark:border-slate-600 dark:text-slate-300";
const HEADING = "text-[11px] font-semibold uppercase tracking-wider";

const plural = (count: number, one: string, many: string): string => `${count} ${count === 1 ? one : many}`;

/** A 40-character SHA is too long to scan; the first seven characters is how GitHub shows one. */
const shownRef = (version: ActionVersion): string => (version.kind === "sha" ? version.ref.slice(0, 7) : version.ref);

/** Whether any workflow of the action can be updated from here: one the caller edits, that is not a repository's own copy. */
const canUpdate = (action: ActionUsageAction): boolean =>
  action.versions.some((version) => version.usages.some((usage) => usage.editable && !usage.override));

const repoCount = (action: ActionUsageAction): number =>
  new Set(action.versions.flatMap((version) => version.usages.flatMap((usage) => usage.repos))).size;

type UpdateMode = "review" | "commit";

/** What the page knows about an action's latest version: being asked, or the answer. */
type Latest = { state: "loading" } | { state: "ready"; version: LatestVersion } | { state: "error"; message: string };

const UNDER_REVIEW = "under_review";

interface WorkflowRow {
  readonly usage: ActionUsageEntry;
  readonly version: ActionVersion;
}

const isLatest = (version: ActionVersion, latest: Latest | undefined): boolean =>
  latest?.state === "ready" && version.pinned && version.ref.toLowerCase() === latest.version.sha.toLowerCase();

/** The workflows of an action in one order: those that need an update first, then by project and file. */
const sortedRows = (action: ActionUsageAction, latest: Latest | undefined): WorkflowRow[] => {
  const rows = action.versions.flatMap((version) => version.usages.map((usage) => ({ usage, version })));
  const rank = ({ version }: WorkflowRow): number => {
    if (!version.pinned) return 0;
    return isLatest(version, latest) ? 2 : 1;
  };
  return rows.sort(
    (a, b) =>
      rank(a) - rank(b) ||
      a.usage.project_name.localeCompare(b.usage.project_name) ||
      a.usage.workflow_file.localeCompare(b.usage.workflow_file),
  );
};

const StatTile: React.FC<
  Readonly<{ label: string; value: number; testId: string; warn?: boolean; hint?: string; pressed?: boolean; onClick?: () => void }>
> = ({ label, value, testId, warn, hint, pressed, onClick }) => {
  const body = (
    <>
      <span className="text-2xl font-bold text-text-primary dark:text-text-primary-dark">{value}</span>
      <span className={cn("text-sm", MUTED)}>{label}</span>
      {hint && onClick && <span className="ml-auto text-xs font-medium text-amber-700 dark:text-amber-300">{hint}</span>}
    </>
  );
  const frame = cn(
    "flex items-baseline gap-3 rounded-lg border px-4 py-3 text-left",
    warn && value > 0
      ? "border-amber-300/70 bg-amber-50/60 dark:border-amber-700/50 dark:bg-amber-950/20"
      : "border-border bg-container dark:border-border-dark dark:bg-container-dark",
  );
  if (onClick && value > 0) {
    return (
      <button
        type="button"
        className={cn(frame, "cursor-pointer hover:border-amber-400 dark:hover:border-amber-600")}
        onClick={onClick}
        aria-pressed={pressed}
        data-testid={testId}
      >
        {body}
      </button>
    );
  }
  return <div className={frame} data-testid={testId}>{body}</div>;
};

const VersionChip: React.FC<Readonly<{ version: ActionVersion; withCount?: boolean }>> = ({ version, withCount }) => {
  const kind = version.pinned ? "pinned to a SHA" : `${version.kind}, not pinned to a SHA`;
  const count = withCount ? `, ${plural(version.workflow_count, "workflow", "workflows")}` : "";
  return (
    <span
      className={cn(PILL, "font-mono", version.pinned ? GREEN : AMBER)}
      title={`${version.ref} (${kind})`}
      aria-label={`${shownRef(version)}, ${kind}${count}`}
      data-testid="action-usage-version"
      data-pinned={version.pinned}
    >
      {version.pinned && <Check className="h-3 w-3" aria-hidden="true" />}
      {shownRef(version)}
      {withCount && <span className="font-sans font-medium opacity-70">× {version.workflow_count}</span>}
    </span>
  );
};

interface UpdateControlProps {
  readonly usage: ActionUsageEntry;
  readonly version: ActionVersion;
  readonly latest: Latest | undefined;
  readonly busy: boolean;
  readonly onUpdate: (mode: UpdateMode) => void;
}

/** What the row offers: update it to the latest, or why it cannot be. */
const UpdateControl: React.FC<UpdateControlProps> = ({ usage, version, latest, busy, onUpdate }) => {
  if (!usage.editable || usage.override) return null;
  if (usage.workflow_status === UNDER_REVIEW) {
    return (
      <Button type="button" size="sm" variant="outline" disabled title="It has an open pull request. Close or merge that first." data-testid="action-usage-update-blocked">
        Under review
      </Button>
    );
  }
  if (latest === undefined || latest.state === "loading") {
    return <span className={cn("text-xs", MUTED)} data-testid="action-usage-latest-loading">Checking…</span>;
  }
  if (latest.state === "error") {
    return (
      <Button type="button" size="sm" variant="outline" disabled title={latest.message} data-testid="action-usage-latest-error">
        Not available
      </Button>
    );
  }
  if (isLatest(version, latest)) {
    return (
      <span className="inline-flex items-center gap-1 text-sm font-semibold text-emerald-700 dark:text-emerald-300" data-testid="action-usage-up-to-date">
        <Check className="h-4 w-4" aria-hidden="true" />
        Up to date
      </span>
    );
  }
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button type="button" size="sm" disabled={busy} data-testid="action-usage-update">
          {busy ? "Updating…" : `Update to ${latest.version.ref}`}
          <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onSelect={() => onUpdate("review")} data-testid="action-usage-update-review">
          <div>
            <p className="font-medium">Update and review</p>
            <p className={cn("text-xs", MUTED)}>Opens it in the editor. Nothing is saved until you commit.</p>
          </div>
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => onUpdate("commit")} data-testid="action-usage-update-commit">
          <div>
            <p className="font-medium">Update and commit locally</p>
            <p className={cn("text-xs", MUTED)}>Saves it in the project now. No pull request.</p>
          </div>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
};

/** What the workflow's `uses` points at inside the action, when it is not the action itself. */
const pointsAt = (usage: ActionUsageEntry): string | null => {
  if (usage.paths.length === 0) return null;
  const shown = usage.paths.join(", ");
  return usage.paths.some((path) => path.includes(".github/workflows/")) ? `calls ${shown}` : `uses ${shown}`;
};

interface WorkflowTableProps {
  readonly rows: WorkflowRow[];
  readonly latest: Latest | undefined;
  readonly updatingId: number | null;
  readonly onUpdate: (usage: ActionUsageEntry, mode: UpdateMode) => void;
}

const WorkflowTable: React.FC<WorkflowTableProps> = ({ rows, latest, updatingId, onUpdate }) => (
  <div className="overflow-x-auto">
    <table className="w-full min-w-[640px] border-collapse text-sm">
      <thead>
        <tr className={cn("text-left", MUTED)}>
          <th className={cn(HEADING, "w-[34%] px-3 pb-2")} scope="col">Workflow</th>
          <th className={cn(HEADING, "px-3 pb-2")} scope="col">Project</th>
          <th className={cn(HEADING, "px-3 pb-2")} scope="col">Repositories</th>
          <th className={cn(HEADING, "px-3 pb-2")} scope="col">Version</th>
          <th className="px-3 pb-2"><span className="sr-only">Update</span></th>
        </tr>
      </thead>
      <tbody>
        {rows.map(({ usage, version }) => (
          <tr
            key={`${usage.workflow_id}-${usage.override ? usage.repos.join(",") : "base"}`}
            className="border-t border-border align-middle dark:border-border-dark"
            data-testid="action-usage-usage"
          >
            <td className="px-3 py-3">
              <div className="flex flex-wrap items-center gap-x-2">
                <code className="text-[13px]">{usage.workflow_file}</code>
                {usage.override && <span className={cn(PILL, QUIET)}>own copy</span>}
                {usage.project_type === "rwx" && <span className={cn(PILL, QUIET)}>reusable workflow</span>}
              </div>
              {pointsAt(usage) && <p className={cn("text-xs", MUTED)}>{pointsAt(usage)}</p>}
            </td>
            <td className="px-3 py-3">{usage.project_name}</td>
            <td className="px-3 py-3" title={usage.repos.join(", ")}>
              {usage.repos.length > 0 ? plural(usage.repos.length, "repository", "repositories") : "None yet"}
            </td>
            <td className="px-3 py-3"><VersionChip version={version} /></td>
            <td className="px-3 py-3 text-right">
              <UpdateControl
                usage={usage}
                version={version}
                latest={latest}
                busy={updatingId === usage.workflow_id}
                onUpdate={(mode) => onUpdate(usage, mode)}
              />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  </div>
);

const LatestLine: React.FC<Readonly<{ latest: Latest | undefined }>> = ({ latest }) => {
  if (latest === undefined || latest.state === "loading") {
    return <p className={cn("text-sm", MUTED)} data-testid="action-usage-latest-looking">Looking up the latest release…</p>;
  }
  if (latest.state === "error") {
    return (
      <div className="flex items-start gap-2.5 rounded-lg border border-border bg-slate-500/5 px-3.5 py-3 text-sm dark:border-border-dark" data-testid="action-usage-no-release">
        <Info className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
        <p>
          <span className="font-semibold">No release to update to.</span> {latest.message} To move these workflows to a specific tag, branch or
          commit, choose <span className="font-semibold">Update all</span> and then <span className="font-semibold">Change</span>.
        </p>
      </div>
    );
  }
  return (
    <p className="flex flex-wrap items-center gap-2 text-sm" data-testid="action-usage-latest">
      <span className={MUTED}>Latest release</span>
      <code className="rounded bg-slate-500/10 px-1.5 py-0.5 font-semibold">{latest.version.ref}</code>
      <span className={MUTED}>→</span>
      <code className="rounded bg-slate-500/10 px-1.5 py-0.5 font-semibold">{latest.version.sha.slice(0, 7)}</code>
    </p>
  );
};

interface ActionRowProps {
  readonly action: ActionUsageAction;
  readonly expanded: boolean;
  readonly latest: Latest | undefined;
  readonly updatingId: number | null;
  readonly onToggle: () => void;
  readonly onUpdate: (usage: ActionUsageEntry, mode: UpdateMode) => void;
  readonly onUpdateAll: (action: string) => void;
}

const ActionRow: React.FC<ActionRowProps> = ({ action, expanded, latest, updatingId, onToggle, onUpdate, onUpdateAll }) => {
  const unpinned = action.unpinned_workflow_count;
  const panelId = `action-usage-panel-${action.action.replace(/[^A-Za-z0-9_-]/g, "-")}`;
  return (
    <section className="overflow-hidden rounded-xl border border-border bg-container shadow-sm dark:border-border-dark dark:bg-container-dark" data-testid="action-usage-action">
      <div className="flex items-center gap-3 px-2 hover:bg-hover-bg dark:hover:bg-hover-dark-bg">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={expanded}
          aria-controls={panelId}
          className="flex min-w-0 flex-1 flex-wrap items-center gap-x-4 gap-y-2 rounded-lg px-3 py-4 text-left focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
          data-testid="action-usage-toggle"
        >
          <ChevronRight className={cn("h-5 w-5 shrink-0 transition-transform", MUTED, expanded && "rotate-90")} aria-hidden="true" />
          <span className={cn("flex h-10 w-10 shrink-0 items-center justify-center rounded-md", NEUTRAL_PROJECT_STYLE.iconBg)}>
            <Package className={cn("h-5 w-5", NEUTRAL_PROJECT_STYLE.icon)} aria-hidden="true" />
          </span>
          <h3 className="min-w-[12rem] break-all text-base font-semibold text-text-primary dark:text-text-primary-dark">{action.action}</h3>
          <span className="flex flex-wrap gap-2">
            {action.versions.map((version) => (
              <VersionChip key={version.ref} version={version} withCount />
            ))}
          </span>
          <span className={cn("ml-auto text-sm", MUTED)}>
            {plural(action.workflow_count, "workflow", "workflows")} · {plural(repoCount(action), "repository", "repositories")}
          </span>
          {unpinned > 0 ? (
            <span className={cn(PILL, AMBER)} data-testid="action-usage-status">{unpinned} not pinned</span>
          ) : (
            <span className={cn(PILL, GREEN)} data-testid="action-usage-status"><Check className="h-3 w-3" aria-hidden="true" />All pinned</span>
          )}
        </button>
        {canUpdate(action) && (
          <Button type="button" size="sm" variant="outline" onClick={() => onUpdateAll(action.action)} data-testid="action-usage-pin">
            Update all
          </Button>
        )}
      </div>
      {expanded && (
        <div id={panelId} className="space-y-3 border-t border-border bg-slate-500/5 px-5 pb-5 pt-4 dark:border-border-dark" data-testid="action-usage-panel">
          <LatestLine latest={latest} />
          <WorkflowTable rows={sortedRows(action, latest)} latest={latest} updatingId={updatingId} onUpdate={onUpdate} />
        </div>
      )}
    </section>
  );
};

const Empty: React.FC = () => (
  <div
    className="flex flex-col items-center gap-3 rounded-lg border border-dashed border-slate-300 px-6 py-12 text-center dark:border-slate-700"
    data-testid="action-usage-empty"
  >
    <div className={cn("flex h-12 w-12 items-center justify-center rounded-md", NEUTRAL_PROJECT_STYLE.iconBg)}>
      <Package className={cn("h-6 w-6", NEUTRAL_PROJECT_STYLE.icon)} aria-hidden="true" />
    </div>
    <p className="font-semibold text-text-primary dark:text-text-primary-dark">No actions in use yet</p>
    <p className={cn("max-w-md text-sm", MUTED)}>Actions appear here once a project has a workflow that uses one.</p>
  </div>
);

interface ToolbarProps {
  readonly query: string;
  readonly onQuery: (value: string) => void;
  readonly unpinnedOnly: boolean;
  readonly onUnpinnedOnly: (value: boolean) => void;
  readonly onExpandAll: () => void;
  readonly onCollapseAll: () => void;
}

const Toolbar: React.FC<ToolbarProps> = ({ query, onQuery, unpinnedOnly, onUnpinnedOnly, onExpandAll, onCollapseAll }) => {
  const segment = (active: boolean): string =>
    cn(
      "cursor-pointer rounded-md px-3 py-1 text-sm font-medium transition-colors",
      active
        ? "bg-container text-text-primary shadow-sm dark:bg-container-dark dark:text-text-primary-dark"
        : cn(MUTED, "hover:bg-container/60 hover:text-text-primary dark:hover:bg-container-dark/60 dark:hover:text-text-primary-dark"),
    );
  const link = "text-sm font-medium text-blue-600 hover:underline dark:text-blue-400";
  return (
    <div className="my-5 flex flex-wrap items-center gap-3">
      <div className="relative">
        <Search className={cn("pointer-events-none absolute left-3 top-2.5 h-4 w-4", MUTED)} aria-hidden="true" />
        <input
          type="search"
          value={query}
          onChange={(event) => onQuery(event.target.value)}
          placeholder="Filter actions"
          aria-label="Filter actions"
          data-testid="action-usage-filter"
          className="h-9 w-64 rounded-md border border-border bg-container pl-9 pr-3 text-sm dark:border-border-dark dark:bg-container-dark"
        />
      </div>
      <fieldset className="inline-flex min-w-0 rounded-lg border-0 bg-slate-500/10 p-0.5 dark:bg-slate-400/10">
        <legend className="sr-only">Show</legend>
        <button type="button" className={segment(!unpinnedOnly)} aria-pressed={!unpinnedOnly} onClick={() => onUnpinnedOnly(false)} data-testid="action-usage-all">
          All
        </button>
        <button type="button" className={segment(unpinnedOnly)} aria-pressed={unpinnedOnly} onClick={() => onUnpinnedOnly(true)} data-testid="action-usage-unpinned-only">
          <ShieldAlert className="mr-1 inline h-3.5 w-3.5" aria-hidden="true" />
          Not pinned
        </button>
      </fieldset>
      <span className="ml-auto flex items-center gap-3">
        <button type="button" className={link} onClick={onCollapseAll} data-testid="action-usage-collapse-all">Collapse all</button>
        <span className={MUTED} aria-hidden="true">|</span>
        <button type="button" className={link} onClick={onExpandAll} data-testid="action-usage-expand-all">Expand all</button>
      </span>
    </div>
  );
};

const HELP =
  "A tag or a branch can be moved after you review it, so an org policy that enforces SHA pinning rejects it. A full commit SHA cannot move.";

interface SummaryProps {
  readonly usage: ActionUsage;
  readonly unpinnedOnly: boolean;
  readonly onToggleUnpinned: () => void;
}

const Summary: React.FC<SummaryProps> = ({ usage, unpinnedOnly, onToggleUnpinned }) => {
  const { action_count, workflows_read, unpinned_workflows } = usage.summary;
  const { local, docker } = usage.other;
  return (
    <div data-testid="action-usage-summary">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <StatTile label="Actions in use" value={action_count} testId="action-usage-stat-actions" />
        <StatTile
          label="Workflows not pinned to a SHA"
          value={unpinned_workflows}
          testId="action-usage-stat-unpinned"
          warn
          hint={unpinnedOnly ? "Show all →" : "Show only these →"}
          pressed={unpinnedOnly}
          onClick={onToggleUnpinned}
        />
        <StatTile label="Workflows read" value={workflows_read} testId="action-usage-stat-workflows" />
      </div>
      <p className={cn("mt-2 flex items-center gap-1.5 text-xs", MUTED)}>
        <span title={HELP} aria-label={HELP} className="inline-flex" data-testid="action-usage-help">
          <Info className="h-3.5 w-3.5" aria-hidden="true" />
        </span>
        Why SHA pinning matters.
        {local + docker > 0 && ` ${local} local and ${docker} Docker references are not counted.`}
      </p>
    </div>
  );
};

interface ActionUsageListProps {
  /** The signed-in user, whose projects the page opens. */
  readonly user: string;
}

/**
 * Which actions the managed workflows run, at which refs (#2045), and a way to update them (#2046):
 * a workflow at a time, from its row, or every workflow that uses an action. Read from the stored
 * workflow YAML, so it shows what ActionsManager will deliver, not what is on GitHub today. An update
 * changes the workflow in its project; delivering it stays the project's Create Pull Requests.
 */
export default function ActionUsageList({ user }: ActionUsageListProps): React.ReactElement {
  const navigate = useNavigate();
  const [usage, setUsage] = useState<ActionUsage | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [unpinnedOnly, setUnpinnedOnly] = useState(false);
  const [updatingAll, setUpdatingAll] = useState<string | null>(null);
  const [latest, setLatest] = useState<Record<string, Latest>>({});
  const [updatingId, setUpdatingId] = useState<number | null>(null);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const load = useCallback(async (): Promise<void> => {
    try {
      setUsage(await fetchActionUsage());
      setLoadError(null);
    } catch (error) {
      setLoadError(apiErrorMessage(error, "Couldn't load action usage."));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Asked once per action, when it is opened, so the page does not call GitHub for every row.
  const asked = useRef(new Set<string>());
  const ensureLatest = useCallback((action: string): void => {
    if (asked.current.has(action)) return;
    asked.current.add(action);
    setLatest((known) => ({ ...known, [action]: { state: "loading" } }));
    fetchLatestVersion(action)
      .then((version) => setLatest((known) => ({ ...known, [action]: { state: "ready", version } })))
      .catch((error_) => setLatest((known) => ({
        ...known,
        [action]: { state: "error", message: apiErrorMessage(error_, "Couldn't look up the latest version.") },
      })));
  }, []);

  const toggle = (action: string): void => {
    const opening = !expanded[action];
    setExpanded((known) => ({ ...known, [action]: opening }));
    if (opening) ensureLatest(action);
  };

  const update = async (action: string, entry: ActionUsageEntry, mode: UpdateMode): Promise<void> => {
    const current = latest[action];
    if (current?.state !== "ready") return;
    const { sha, comment } = current.version;
    setUpdatingId(entry.workflow_id);
    try {
      const done = await updateWorkflow({ workflow_id: entry.workflow_id, action, sha, comment, commit: mode === "commit" });
      if (mode === "review") {
        navigate(`/project/${user}/${entry.project_name}`, {
          state: { projectId: entry.project_id, draftWorkflow: { name: done.workflow_name, content: done.content, reusable: done.reusable } },
        });
        return;
      }
      toast.success(`Updated ${done.workflow_file} in ${done.project_name} and committed it locally. Create pull requests from the project when you are ready.`);
      await load();
    } catch (error_) {
      toast.error(apiErrorMessage(error_, "Couldn't update the workflow."));
    } finally {
      setUpdatingId(null);
    }
  };

  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (usage?.actions ?? []).filter(
      (action) => action.action.toLowerCase().includes(needle) && (!unpinnedOnly || action.unpinned_workflow_count > 0),
    );
  }, [usage, query, unpinnedOnly]);

  const filtered = unpinnedOnly || query.trim() !== "";
  const showEverything = (): void => {
    setQuery("");
    setUnpinnedOnly(false);
  };

  const expandAll = (): void => {
    setExpanded(Object.fromEntries(shown.map((action) => [action.action, true])));
    shown.forEach((action) => ensureLatest(action.action));
  };

  return (
    <div className="w-full" data-testid="action-usage-list">
      <div className="mb-6">
        <h2 className="text-3xl font-bold text-gray-900 dark:text-white">Action Usage</h2>
        <p className="mt-2 max-w-2xl text-sm text-gray-600 dark:text-slate-300">
          The actions your workflows use, and which version each is on. Open an action to see its workflows and update them to the latest release.
        </p>
      </div>

      {usage === null && !loadError && <p className={cn("text-sm", MUTED)}>Loading action usage…</p>}
      {loadError && <p role="alert" className="text-sm text-red-600 dark:text-red-400">{loadError}</p>}

      {usage !== null && usage.actions.length === 0 && <Empty />}

      {usage !== null && usage.actions.length > 0 && (
        <>
          <Summary usage={usage} unpinnedOnly={unpinnedOnly} onToggleUnpinned={() => setUnpinnedOnly((on) => !on)} />
          <Toolbar
            query={query}
            onQuery={setQuery}
            unpinnedOnly={unpinnedOnly}
            onUnpinnedOnly={setUnpinnedOnly}
            onExpandAll={expandAll}
            onCollapseAll={() => setExpanded({})}
          />
          {filtered && (
            <p className={cn("mb-3 flex flex-wrap items-center gap-2 text-sm", MUTED)} data-testid="action-usage-filtered">
              Showing {shown.length} of {usage.actions.length} {usage.actions.length === 1 ? "action" : "actions"}.
              <button type="button" className="font-medium text-blue-600 hover:underline dark:text-blue-400" onClick={showEverything} data-testid="action-usage-show-all">
                Show all
              </button>
            </p>
          )}
          {shown.length === 0 ? (
            <p className={cn("text-sm", MUTED)} data-testid="action-usage-no-match">No actions match.</p>
          ) : (
            <div className="space-y-3">
              {shown.map((action) => (
                <ActionRow
                  key={action.action}
                  action={action}
                  expanded={Boolean(expanded[action.action])}
                  latest={latest[action.action]}
                  updatingId={updatingId}
                  onToggle={() => toggle(action.action)}
                  onUpdate={(entry, mode) => void update(action.action, entry, mode)}
                  onUpdateAll={setUpdatingAll}
                />
              ))}
            </div>
          )}
        </>
      )}

      {updatingAll !== null && (
        <PinActionDialog action={updatingAll} onClose={() => setUpdatingAll(null)} onApplied={() => void load()} />
      )}
    </div>
  );
}
