import apiClient from "./apiClient";

/** How a ref reads, from its name alone: only a 40-character SHA is pinned. */
export type RefKind = "sha" | "tag" | "branch";

/** A workflow on one ref of an action, and the repositories that run it. */
export interface ActionUsageEntry {
  project_id: number;
  project_name: string;
  workflow_id: number;
  /** The name stored for the workflow, without the project prefix. */
  workflow_name: string;
  /** The file name on GitHub. */
  workflow_file: string;
  workflow_status: string;
  reusable: boolean;
  /** "rwx" for a Reusable Workflow Project, which Pin or bump leaves alone. */
  project_type: string;
  /** Whether the caller may change the project, which a pin does. */
  editable: boolean;
  repos: string[];
  /** True when these repositories run their own copy of the workflow. */
  override: boolean;
  /** Where in the action it is used: a composite action's subdirectory, or a reusable workflow's file. */
  paths: string[];
}

export interface ActionVersion {
  ref: string;
  kind: RefKind;
  pinned: boolean;
  workflow_count: number;
  repo_count: number;
  usages: ActionUsageEntry[];
}

export interface ActionUsageAction {
  /** owner/repo */
  action: string;
  /** Unpinned refs first. */
  versions: ActionVersion[];
  workflow_count: number;
  unpinned_workflow_count: number;
}

/** GET /api/action-usage: the actions the caller's managed workflows use. */
export interface ActionUsage {
  actions: ActionUsageAction[];
  /** Local and docker references, recorded and never flagged. */
  other: { local: number; docker: number };
  summary: { action_count: number; workflows_read: number; unpinned_workflows: number };
}

export const fetchActionUsage = async (): Promise<ActionUsage> =>
  (await apiClient.get<ActionUsage>("/api/action-usage")).data;

/** The lines of one workflow an update would change. */
export interface PinLine {
  line: number;
  before: string;
  after: string;
}

export interface PinWorkflowChange {
  workflow_id: number;
  workflow_name: string;
  workflow_file: string;
  reusable: boolean;
  lines: PinLine[];
  /** Why it is not updated, when it is not: it has an open pull request. */
  blocked?: string | null;
}

/** What an update does in one project. */
export interface PinProjectChange {
  project_id: number;
  project_name: string;
  project_type: string;
  editable: boolean;
  /** Repositories with their own copy of a changed workflow, which keep it. */
  skipped_repos: string[];
  workflows: PinWorkflowChange[];
}

/** GET /api/action-usage/preview: the tag resolved once, and what changes in every visible project. */
export interface PinPreview {
  action: string;
  /** The tag, branch or SHA the preview is for: the one asked for, or the latest release's tag. */
  ref: string;
  source: "latest release" | "requested";
  sha: string;
  /** The version written beside the SHA; null when a bare SHA was asked for. */
  comment: string | null;
  projects: PinProjectChange[];
}

/** Without `ref` the server picks the action's latest release (or newest version tag), which is the default worth updating to. */
export const previewPin = async (action: string, ref?: string): Promise<PinPreview> =>
  (await apiClient.get<PinPreview>("/api/action-usage/preview", { params: ref ? { action, ref } : { action } })).data;

/** The version an update goes to by default: an action's latest release, and the commit it is on. */
export interface LatestVersion {
  action: string;
  ref: string;
  sha: string;
  comment: string | null;
}

export const fetchLatestVersion = async (action: string): Promise<LatestVersion> =>
  (await apiClient.get<LatestVersion>("/api/action-usage/latest", { params: { action } })).data;

export interface ApplyPinRequest {
  project_id: number;
  action: string;
  sha: string;
  comment: string | null;
}

/** What updating every workflow of one project did. */
export interface ApplyPinResult {
  updated: number;
  workflows: string[];
  pr_state: string;
}

/** POST /api/action-usage/apply: updates the workflows of one project and commits them locally. No pull request is opened. */
export const applyPin = async (request: ApplyPinRequest): Promise<ApplyPinResult> =>
  (await apiClient.post<ApplyPinResult>("/api/action-usage/apply", request)).data;

export interface UpdateWorkflowRequest {
  workflow_id: number;
  action: string;
  sha: string;
  comment: string | null;
  /** False saves nothing and returns the updated YAML, for the editor to load as an unsaved edit. */
  commit: boolean;
}

export interface UpdatedWorkflow {
  workflow_id: number;
  workflow_name: string;
  workflow_file: string;
  project_id: number;
  project_name: string;
  reusable: boolean;
  changed: number;
  /** Only without `commit`: the updated YAML. */
  content?: string;
  /** Only with `commit`: where the project and the workflow stand now. */
  pr_state?: string;
  workflow_status?: string;
}

/** POST /api/action-usage/update-workflow: update one workflow's use of an action. */
export const updateWorkflow = async (request: UpdateWorkflowRequest): Promise<UpdatedWorkflow> =>
  (await apiClient.post<UpdatedWorkflow>("/api/action-usage/update-workflow", request)).data;
