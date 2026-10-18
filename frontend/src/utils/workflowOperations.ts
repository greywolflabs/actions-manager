import { saveWorkflows, updateWorkflows, deleteWorkflowFromGitHub, deleteWorkflowFromDatabase, deleteReusableWorkflowFromGitHub, type UpdateWorkflowsResponse } from '../api/workflows';
import { saveRxWorkflows } from '../api/rxworkflows';
import { updateLinkedReusableWorkflow } from '../api/projects';
import { Workflow, RXWorkflow, UnifiedWorkflowItem } from '../types/workflow';
import type { RemovalDelivery, RemovalScope } from '../components/RemovalScopeDialog';
import { WorkflowResult } from '../types/workflowResponse';
import { RwxWorkflow } from '../api/projects';
import { normalizeWorkflowStem, setWorkflowYamlName } from './workflowFilename';
import type { WorkflowKindChange } from './workflowKindChanges';
import { toast } from './toast';
import { tour } from './tour';

// Save Draft functionality
// A save whose trigger change moved the workflow to the other section says so,
// or the workflow appears to vanish from where the user was editing it.
const draftSavedMessage = (stem: string, kindChanges: WorkflowKindChange[]): string => {
  const saved = `Workflow '${stem}.yml' saved as draft.`;
  if (kindChanges.length === 0) return saved;
  const kind = kindChanges[0].is_reusable ? 'reusable' : 'regular';
  return `${saved} Its trigger makes it a ${kind} workflow now.`;
};

export const saveDraftWorkflow = async (
  index: number | null,
  type: 'regular' | 'reusable',
  workflows: Workflow[],
  rxworkflows: RXWorkflow[],
  user: string,
  projectName: string,
  accountType?: string,
  markWorkflowAsSaved?: (index: number, type: 'regular' | 'reusable', workflowStatus?: string) => void,
  fetchWorkflowsCount?: () => Promise<void>,
  refreshProjectsList?: () => Promise<void>,
  onProjectStateChange?: (state: string) => void,
  applyKindChanges?: (changes: WorkflowKindChange[]) => void
): Promise<void> => {
  if (index === null) return;
  
  const workflow = type === 'regular' ? workflows[index] : rxworkflows[index];
  
  if (!workflow?.name || !workflow?.content) {
    toast.error("Please provide both a workflow name and content before saving as draft.");
    return;
  }

  // Normalize to stem: trim whitespace and strip any .yml/.yaml extension.
  // The backend stores workflow_name as a stem and appends .yml itself via
  // format_workflow_name(), so we must send the stem to avoid duplicate rows
  // and double-extension issues in prefix mode (AM_CODE_name.yml.yml).
  const stem = normalizeWorkflowStem(workflow.name);
  if (!stem) {
    toast.error("Please provide a valid workflow name before saving as draft.");
    return;
  }

  try {
    console.log("Saving workflow as draft:", stem);
    
    // Determine new status: first-ever save → "new"; subsequent saves → "committed_locally"
    const newWorkflowStatus =
      (workflow.workflowStatus != null && workflow.workflowStatus !== '')
        ? 'committed_locally'
        : 'new';

    let kindChanges: WorkflowKindChange[] = [];
    if (type === 'regular') {
      const singleWorkflow: Workflow[] = [{ ...workflows[index], name: stem }];
      const saveResponse = await saveWorkflows(user, projectName, singleWorkflow);
      if (!saveResponse) {
        throw new Error("Invalid response from save API");
      }
      kindChanges = saveResponse.kind_changes ?? [];
      // Notify parent of project state change if it changed (e.g. new → draft)
      if (onProjectStateChange && saveResponse.pr_state) {
        onProjectStateChange(saveResponse.pr_state);
      }
    } else {
      const singleWorkflow: RXWorkflow[] = [{ ...rxworkflows[index], name: stem }];
      const saveResponse = await saveRxWorkflows(user, projectName, singleWorkflow);
      if (!saveResponse) {
        throw new Error("Invalid response from save API");
      }
      kindChanges = saveResponse.kind_changes ?? [];
      // Notify parent of project state change if it changed (e.g. new → draft)
      // so the Create Pull Requests button enables without a page refresh
      if (onProjectStateChange && saveResponse.pr_state) {
        onProjectStateChange(saveResponse.pr_state);
      }
    }
    
    if (markWorkflowAsSaved) {
      markWorkflowAsSaved(index, type, newWorkflowStatus);
    }
    // After the "saved" marks: the move is keyed by name and applies on top.
    applyKindChanges?.(kindChanges);
    // This is the "Commit Locally" the tour points at — the editor's primary
    // action routes here, not through ProjectMgmt's toolbar save.
    tour.completed('commit-workflow');
    toast.success(draftSavedMessage(stem, kindChanges));
    
    // Update count for free accounts
    if (accountType === "free" && fetchWorkflowsCount) {
      await fetchWorkflowsCount();
    }
    
    // Refresh projects list to update status
    if (refreshProjectsList) {
      await refreshProjectsList();
    }
  } catch (error: any) {
    console.error("❌ Error saving workflow draft:", error);
    toast.error(`Failed to save workflow '${stem}.yml' as draft: ${error.message}`);
  }
};

// The server pushes only to the project's own repositories (#2316). One ticked
// but not saved yet is skipped, and a plain success would imply it got the push.
const warnSkippedRepos = (skipped?: string[]): void => {
  if (!skipped?.length) return;
  const pronoun = skipped.length === 1 ? 'it' : 'them';
  toast.warning(`Not pushed to ${skipped.join(', ')}: not part of this project yet. Save the project to add ${pronoun}.`);
};

// The repositories of the "owner/repo on branch" targets a pull request now carries the push to.
const reposWithPullRequest = (results: Record<string, unknown>): string[] => [
  ...new Set(
    Object.entries(results)
      .filter(([, r]) => {
        const status = (r as WorkflowResult | null)?.status;
        return status === 'pr_created' || status === 'pr_updated';
      })
      .map(([target]) => target.split(' on ')[0]),
  ),
];

// Every Commit and Update PR reports a push the same way: the repositories whose
// pull request now carries it, even when another failed, then the failure.
export const assertPushed = (
  updateResponse: UpdateWorkflowsResponse,
  onReposDelivered?: (repos: string[]) => void
): void => {
  if (updateResponse.error) {
    throw new Error(updateResponse.error);
  }
  const results = updateResponse.results || {};
  onReposDelivered?.(reposWithPullRequest(results));
  const hasErrors = Object.values(results).some(
    (r): r is WorkflowResult => typeof r === 'object' && r !== null && r.status === 'error'
  );
  if (hasErrors) {
    throw new Error("One or more repositories failed to update. Check console for details.");
  }
  warnSkippedRepos(updateResponse.skipped_repos);
};

/** Who pushes where for a Commit and Update PR, and what the page refreshes after it. */
export interface PullRequestPush {
  user: string;
  projectName: string;
  selectedRepos: string[];
  fetchWorkflowsCount?: () => Promise<void>;
  refreshProjectsList?: () => Promise<void>;
  onProjectStateChange?: (state: string) => void;
  onReposDelivered?: (repos: string[]) => void;
}

const refreshAfterPush = async (push: PullRequestPush): Promise<void> => {
  await push.fetchWorkflowsCount?.();
  await push.refreshProjectsList?.();
};

// The save a workflow's push then delivers from (as stem — backend handles .yml suffix).
const saveBeforePush = async (
  type: 'regular' | 'reusable',
  index: number,
  workflows: Workflow[],
  rxworkflows: RXWorkflow[],
  stem: string,
  push: PullRequestPush
): Promise<void> => {
  const saveResponse = type === 'regular'
    ? await saveWorkflows(push.user, push.projectName, [{ ...workflows[index], name: stem }])
    : await saveRxWorkflows(push.user, push.projectName, [{ ...rxworkflows[index], name: stem }]);
  if (!saveResponse) throw new Error("Invalid response from save API");
  if (saveResponse.pr_state) push.onProjectStateChange?.(saveResponse.pr_state);
};

// Commit workflow changes and update the existing open pull request
export const commitAndUpdatePRWorkflow = async (
  index: number | null,
  type: 'regular' | 'reusable',
  workflows: Workflow[],
  rxworkflows: RXWorkflow[],
  push: PullRequestPush,
  markWorkflowAsSaved?: (index: number, type: 'regular' | 'reusable', workflowStatus?: string) => void
): Promise<boolean> => {
  if (index === null) return false;

  const workflow = type === 'regular' ? workflows[index] : rxworkflows[index];
  if (!workflow?.name || !workflow?.content) {
    toast.error("Please provide both a workflow name and content before updating.");
    return false;
  }

  // Normalize to stem: trim whitespace and strip any .yml/.yaml extension.
  // Store the stem in DB (backend appends .yml via format_workflow_name).
  // The GitHub update API also strips extensions server-side before formatting.
  const stem = normalizeWorkflowStem(workflow.name);
  if (!stem) {
    toast.error("Please provide a valid workflow name before updating.");
    return false;
  }

  try {
    await saveBeforePush(type, index, workflows, rxworkflows, stem, push);

    // Push to GitHub to update the existing PR branch (stem sent; backend adds prefix+.yml)
    const files = type === 'regular'
      ? { workflows: [{ ...workflows[index], name: stem }] }
      : { rxworkflows: [{ name: stem, content: workflow.content, isReusable: true }] };
    assertPushed(await updateWorkflows(push.projectName, push.selectedRepos, files), push.onReposDelivered);

    // Mark as saved in the frontend state – keep workflowStatus as 'under_review'
    markWorkflowAsSaved?.(index, type, 'under_review');
    toast.success(`Workflow '${stem}.yml' committed and pull request updated.`);
    await refreshAfterPush(push);
    return true;
  } catch (error: any) {
    console.error("❌ Error committing and updating PR:", error);
    toast.error(`Failed to commit and update PR for '${stem}.yml': ${error.message}`);
    return false;
  }
};

// Commit and Update PR for linked reusable workflows.
// Linked workflows are stored in the RWX project's repo/DB, but their association to
// an open PR is tracked via the standard project's ProjectPullRequest.  The save step
// must target the RWX project so the correct DB record is updated, while the push step
// uses the standard project name (push.projectName) so the backend can resolve the
// right RWX repo via LinkedReusableWorkflow.
export const commitAndUpdatePRLinkedWorkflow = async (
  index: number,
  linkedWorkflows: Array<{ workflow_id?: number; workflow_name: string; workflow_yaml: string; rwx_project_name: string }>,
  push: PullRequestPush
): Promise<boolean> => {
  const linkedWf = linkedWorkflows[index];
  if (!linkedWf?.workflow_name || !linkedWf?.workflow_yaml) {
    toast.error("The selected linked workflow data is incomplete. Please reload the project and try again.");
    return false;
  }

  try {
    // Step 1: Save updated content to the canonical reusable workflow row
    // via the dedicated linked-workflow endpoint.  Resolution is by
    // workflow_id joined through LinkedReusableWorkflow to the standard
    // project — using the generic save-workflows endpoint here would create
    // a duplicate workflow row in the RWX project because the display
    // workflow_name (e.g. "AM_RWW1_testrwx.yml") does not match the
    // canonical stored stem ("testrwx").
    if (typeof linkedWf.workflow_id !== 'number') {
      throw new TypeError("Linked workflow is missing workflow_id; please reload the project.");
    }
    await updateLinkedReusableWorkflow(
      push.user,
      push.projectName,
      linkedWf.workflow_id,
      linkedWf.workflow_yaml
    );

    // Step 2: Push to GitHub using the standard project context.
    // The backend resolves the target RWX repo via LinkedReusableWorkflow for the
    // standard project, so passing project_name=standardProjectName is correct.
    const rxworkflows: Workflow[] = [
      { name: linkedWf.workflow_name, content: linkedWf.workflow_yaml, isReusable: true }
    ];
    assertPushed(await updateWorkflows(push.projectName, push.selectedRepos, { rxworkflows }));

    push.onProjectStateChange?.('open');
    toast.success(`Linked workflow '${linkedWf.workflow_name}' committed and pull request updated.`);
    await refreshAfterPush(push);
    return true;
  } catch (error: any) {
    console.error("❌ Error committing and updating linked workflow PR:", error);
    toast.error(`Failed to commit and update PR for '${linkedWf.workflow_name}': ${error.message}`);
    return false;
  }
};

// Save draft for linked reusable workflows.
// Updates the canonical reusable workflow row in the source RWX project via
// the dedicated /api/projects/{name}/linked-reusable-workflows/{id} endpoint.
// The backend resolves the workflow by ID joined through LinkedReusableWorkflow
// (NOT by workflow_name) so the display-formatted name returned for linked
// workflows can no longer cause a duplicate workflow row to be created in the
// source RWX project.
export const saveDraftLinkedWorkflow = async (
  index: number,
  linkedWorkflows: RwxWorkflow[],
  user: string,
  standardProjectName: string,
  setLinkedWorkflows?: (updater: (prev: RwxWorkflow[]) => RwxWorkflow[]) => void,
  refreshProjectsList?: () => Promise<void>
): Promise<void> => {
  const linkedWf = linkedWorkflows[index];
  if (!linkedWf?.workflow_name || !linkedWf?.workflow_yaml) {
    toast.error("The selected linked workflow data is incomplete. Please reload the project and try again.");
    return;
  }
  if (typeof linkedWf.workflow_id !== 'number') {
    toast.error("Linked workflow source could not be resolved. Please reload the project and try again.");
    return;
  }

  try {
    const result = await updateLinkedReusableWorkflow(
      user,
      standardProjectName,
      linkedWf.workflow_id,
      linkedWf.workflow_yaml
    );

    // Update isModified and workflowStatus from the API response so the
    // editor badge, sidebar card, and PR campaign modal all reflect the new
    // status immediately — without requiring a page refresh.
    if (setLinkedWorkflows) {
      setLinkedWorkflows((prev: RwxWorkflow[]) => {
        const updated = [...prev];
        if (updated[index]) {
          updated[index] = {
            ...updated[index],
            isModified: false,
            workflowStatus: result.workflow_status ?? 'committed_locally',
          };
        }
        return updated;
      });
    }

    toast.success(`Reusable workflow '${linkedWf.workflow_name}' saved in ${linkedWf.rwx_project_name}.`);

    if (refreshProjectsList) {
      await refreshProjectsList();
    }
  } catch (error: any) {
    console.error("❌ Error saving linked workflow draft:", error);
    const detail = error?.response?.data?.detail;
    const baseMessage = detail || error.message || "Unknown error";
    if (error?.response?.status === 404) {
      toast.error(`Could not find the source reusable workflow for '${linkedWf.workflow_name}'. Please reload the project and try again.`);
    } else {
      toast.error(`Failed to update '${linkedWf.workflow_name}' in ${linkedWf.rwx_project_name}: ${baseMessage}`);
    }
  }
};

// Helper function to handle authentication errors
const handleAuthenticationError = (): void => {
  toast.error("Authentication required. Please log in again.");
  localStorage.removeItem("github_user");
  globalThis.setTimeout(() => {
    globalThis.location.reload();
  }, 1500);
};

// Helper function to handle workflow deletion errors
const handleDeleteError = (error: any): void => {
  console.error("❌ Error deleting workflow:", error);
  
  // Handle specific error cases - Type guard for axios error
  if (error && typeof error === 'object' && 'response' in error) {
    if (error.response?.status === 401) {
      handleAuthenticationError();
    } else if (error.response?.status === 404) {
      toast.error("Workflow or project not found. The workflow may have already been deleted.");
    } else {
      const errorMessage = error.response?.data?.detail ?? (error as Error).message ?? "Unknown error";
      toast.error(`Error deleting workflow: ${errorMessage}`);
    }
  } else {
    const errorMessage = error instanceof Error ? error.message : "Unknown error";
    toast.error(`Error deleting workflow: ${errorMessage}`);
  }
};

// Deletes a workflow through the delete-workflow route: every regular workflow,
// a reusable one a caller project owns (#2309), and any workflow removed by a
// PR Campaign, a Reusable Workflow Project's included.
const deleteRegularWorkflow = async (
  params: {
    user: string;
    selectedRepos: string[];
    workflowName: string;
    regexPattern: string;
    projectName: string;
    scope: RemovalScope;
    delivery: RemovalDelivery;
  },
  removeFromState: () => void
): Promise<boolean> => {
  if (params.scope === 'project_and_github') {
    await deleteWorkflowFromGitHub(
      params.user,
      params.selectedRepos,
      params.workflowName,
      params.regexPattern,
      params.projectName,
      params.delivery
    );
    if (params.delivery === 'campaign') {
      // The campaign carries the removal; the row is marked pending_delete and
      // dropped only when that campaign merges. Deleting it here would leave
      // the campaign delivering a file nothing in ActionsManager still tracks,
      // and Restore would have nothing to restore.
      return false;
    }
  }
  await deleteWorkflowFromDatabase(params.user, params.projectName, params.workflowName);
  removeFromState();
  return true;
};

// Helper function to delete reusable workflow
const deleteReusableWorkflow = async (
  user: string,
  workflowName: string,
  projectName: string,
  scope: RemovalScope,
  index: number,
  setRXWorkflows: (workflows: RXWorkflow[] | ((prev: RXWorkflow[]) => RXWorkflow[])) => void
): Promise<void> => {
  if (scope === 'project_and_github') {
    await deleteReusableWorkflowFromGitHub(user, workflowName, projectName);
  }
  await deleteWorkflowFromDatabase(user, projectName, workflowName);
  setRXWorkflows(prev => {
    const newWorkflows = Array.isArray(prev) ? [...prev] : [];
    return newWorkflows.filter((_, i) => i !== index);
  });
};

// Delete workflow functionality
export interface DeleteWorkflowParams {
  index: number;
  type: 'regular' | 'reusable';
  workflows: Workflow[];
  rxworkflows: RXWorkflow[];
  user: string;
  projectName: string;
  selectedRepos: string[];
  regexPattern: string;
  /** Required, not defaulted: an omitted scope must never silently mean "delete from GitHub too". */
  scope: RemovalScope;
  /** How a GitHub-scoped removal reaches the repositories. Ignored for scope 'project'. */
  delivery: RemovalDelivery;
  /**
   * A caller project's own reusable workflows live in its repositories, so they
   * are removed from there like a regular workflow. Omitted, a reusable workflow
   * is removed from the Reusable Workflow Project's repository: by a PR Campaign
   * through the same route as any other, directly through its own.
   */
  projectType?: 'standard' | 'rwx';
  setWorkflows: (workflows: Workflow[]) => void;
  setRXWorkflows: (workflows: RXWorkflow[] | ((prev: RXWorkflow[]) => RXWorkflow[])) => void;
  setSelectedWorkflowId: (id: string | null) => void;
  fetchWorkflowsCount?: () => Promise<void>;
  /**
   * Reloads the project itself, not just the projects list. The server drops the
   * removed workflow's drift rows, so anything seeded from the project-load
   * endpoint — `drifted_workflow_names` above all — is stale until this runs.
   */
  refreshProjectData?: () => Promise<void>;
}

/** What the removal did, as a sentence for a toast. */
export function describeWorkflowRemoval(
  name: string,
  scope: RemovalScope,
  delivery: RemovalDelivery,
  removed: boolean
): string {
  if (scope !== 'project_and_github') {
    return `Workflow "${name}" removed from ActionsManager. Nothing was deleted from GitHub.`;
  }
  if (!removed) {
    return `PR Campaign opened to remove "${name}" from GitHub. Track and merge it under PR Campaigns.`;
  }
  return `Workflow "${name}" deleted from ActionsManager and GitHub.`;
}

export const deleteWorkflow = async ({
  index,
  type,
  workflows,
  rxworkflows,
  user,
  projectName,
  selectedRepos,
  regexPattern,
  scope,
  delivery,
  projectType,
  setWorkflows,
  setRXWorkflows,
  setSelectedWorkflowId,
  fetchWorkflowsCount,
  refreshProjectData,
}: DeleteWorkflowParams): Promise<void> => {
  const workflow = type === 'regular' ? workflows[index] : rxworkflows[index];

  if (!workflow) return;

  try {
    let removed = true;
    if (type === 'regular' || projectType === 'standard' || delivery === 'campaign') {
      removed = await deleteRegularWorkflow(
        {
          user,
          selectedRepos,
          workflowName: workflow.name,
          regexPattern,
          projectName,
          scope,
          delivery,
        },
        type === 'regular'
          ? () => setWorkflows(workflows.filter((_, i) => i !== index))
          : () => setRXWorkflows(prev => (Array.isArray(prev) ? prev : []).filter((_, i) => i !== index))
      );
    } else {
      await deleteReusableWorkflow(user, workflow.name, projectName, scope, index, setRXWorkflows);
    }

    if (removed) setSelectedWorkflowId(null);
    toast.success(describeWorkflowRemoval(workflow.name, scope, delivery, removed));
  } catch (error: any) {
    handleDeleteError(error);
    return;
  }

  // After the toast: the workflow is already gone, so these only correct what the
  // removal invalidated. Both swallow their own errors, and neither depends on
  // the other, so they run together.
  await Promise.all([fetchWorkflowsCount?.(), refreshProjectData?.()]);
};

// Workflow creation utilities
export const createBlankWorkflow = (
  type: 'regular' | 'reusable',
  workflowName: string,
  workflows: Workflow[],
  setWorkflows: (workflows: Workflow[]) => void,
  setRXWorkflows: (workflows: RXWorkflow[] | ((prev: RXWorkflow[]) => RXWorkflow[])) => void,
  setSelectedWorkflowId: (id: string) => void
): void => {
  const stem = normalizeWorkflowStem(workflowName);
  const template = setWorkflowYamlName('', stem);
  // A reusable workflow is only callable through workflow_call, and GUI mode
  // refuses one without it, since it would rewrite the trigger.
  const content = type === 'reusable' ? template.replace('workflow_dispatch:', 'workflow_call:') : template;

  if (type === 'regular') {
    const newWorkflow: Workflow = {
      name: stem,
      content,
      isReusable: false,
      isModified: true
    };
    
    const newWorkflows = [...workflows, newWorkflow];
    setWorkflows(newWorkflows);
    setSelectedWorkflowId(`regular-${newWorkflows.length - 1}`);
  } else {
    const newWorkflow: RXWorkflow = {
      name: stem,
      content,
      isReusable: true,
      isModified: true
    };
    
    setRXWorkflows(prev => {
      const newWorkflows = Array.isArray(prev) ? [...prev, newWorkflow] : [newWorkflow];
      setSelectedWorkflowId(`reusable-${newWorkflows.length - 1}`);
      return newWorkflows;
    });
  }
};

export interface WorkflowChangeParams {
  field: string;
  value: string;
  selectedWorkflow: UnifiedWorkflowItem | undefined;
  workflows: Workflow[];
  setWorkflows: (workflows: Workflow[] | ((prev: Workflow[]) => Workflow[])) => void;
  setRXWorkflows: (workflows: RXWorkflow[] | ((prev: RXWorkflow[]) => RXWorkflow[])) => void;
  markWorkflowAsModified: (index: number, type: 'regular' | 'reusable') => void;
  setLinkedWorkflows?: (updater: (prev: any[]) => any[]) => void;
}

export const handleWorkflowChange = ({
  field,
  value,
  selectedWorkflow,
  workflows,
  setWorkflows,
  setRXWorkflows,
  markWorkflowAsModified,
  setLinkedWorkflows,
}: WorkflowChangeParams) => {
  if (!selectedWorkflow) return;

  if (selectedWorkflow.type === 'regular') {
    setWorkflows(prev => {
      const newWorkflows = [...prev];
      newWorkflows[selectedWorkflow.originalIndex] = {
        ...newWorkflows[selectedWorkflow.originalIndex],
        [field]: value,
        isModified: true
      };
      return newWorkflows;
    });
    markWorkflowAsModified(selectedWorkflow.originalIndex, 'regular');
  } else if (selectedWorkflow.type === 'reusable') {
    setRXWorkflows(prev => {
      const newWorkflows = Array.isArray(prev) ? [...prev] : [];
      const workflow = newWorkflows[selectedWorkflow.originalIndex];
      if (workflow) {
        newWorkflows[selectedWorkflow.originalIndex] = {
          ...workflow,
          [field]: value,
          isModified: true
        };
      }
      return newWorkflows;
    });
    markWorkflowAsModified(selectedWorkflow.originalIndex, 'reusable');
  } else if (selectedWorkflow.type === 'linked') {
    if (setLinkedWorkflows && field === 'content') {
      setLinkedWorkflows((prev: any[]) => {
        const updated = [...prev];
        if (updated[selectedWorkflow.originalIndex]) {
          updated[selectedWorkflow.originalIndex] = {
            ...updated[selectedWorkflow.originalIndex],
            workflow_yaml: value,
            isModified: true,
          };
        }
        return updated;
      });
    }
  }
};
