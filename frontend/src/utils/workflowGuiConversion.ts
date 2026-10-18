import * as yaml from 'js-yaml';

// TypeScript interfaces for GUI workflow structure
export interface WorkflowGUI {
  name: string;
  events: WorkflowEvent[];
  jobs: WorkflowJob[];
  env?: { [key: string]: string };
  unsupportedFields?: { [key: string]: any };
}

export interface WorkflowCallInput {
  description?: string;
  required?: boolean;
  type?: 'string' | 'number' | 'boolean' | 'choice';
  default?: string | number | boolean;
  options?: string[];
}

export interface WorkflowEvent {
  type: 'push' | 'pull_request' | 'workflow_dispatch' | 'schedule' | 'release' | 'workflow_call';
  branches?: string[];
  paths?: string[];
  /** Tag patterns for `push.tags` - GitHub Actions has no separate "tag push"
   * event; tag-triggered pushes are the `push` event filtered by tags. */
  tags?: string[];
  cron?: string;
  /** Every `on.schedule` entry as read. The GUI edits only the first entry's
   * cron, through `cron`; later entries and keys such as `timezone` are kept so
   * the round trip stays lossless. */
  schedule?: unknown[];
  types?: string[];
  inputs?: { [key: string]: WorkflowCallInput };
  /** Keys under this event that the GUI does not model - notably a reusable
   * workflow's `workflow_call.secrets` / `.outputs`, which are part of its
   * public interface. Kept verbatim so the round trip stays lossless, the way
   * jobs and steps already preserve theirs. */
  unsupportedFields?: { [key: string]: any };
}

export interface WorkflowJob {
  id: string;
  name?: string;
  runsOn: string;
  steps: WorkflowStep[];
  env?: { [key: string]: string };
  if?: string;
  /** As written: a single `needs: build` stays a string so it round-trips. */
  needs?: string | string[];
  timeoutMinutes?: number;
  /** A job-level `uses:` makes this a call to a reusable workflow. GitHub then
   * rejects `runs-on`, `steps`, `env` and `timeout-minutes` on it, so none of
   * them are written or offered for it. */
  uses?: string;
  unsupportedFields?: { [key: string]: any };
}

export interface WorkflowStep {
  /** The GUI's own key for the step. Seeded from the YAML `id:` when there is
   * one, otherwise minted (`step-1`, `x-copy`), so it is never written back. */
  id: string;
  /** The step's YAML `id:` as read, which `steps.<id>` expressions reference. */
  yamlId?: string;
  name?: string;
  uses?: string;
  run?: string;
  with?: { [key: string]: string };
  env?: { [key: string]: string };
  if?: string;
  shell?: string;
  workingDirectory?: string;
  continueOnError?: boolean;
  timeoutMinutes?: number;
  unsupportedFields?: { [key: string]: any };
}

// Default workflow template
export const DEFAULT_WORKFLOW_GUI: WorkflowGUI = {
  name: 'CI',
  events: [{ type: 'push' }],
  jobs: [
    {
      id: 'build',
      name: 'Build',
      runsOn: 'ubuntu-latest',
      steps: [
        {
          id: 'checkout',
          name: 'Checkout code',
          uses: 'actions/checkout@v5'
        },
        {
          id: 'setup-node',
          name: 'Setup Node.js',
          uses: 'actions/setup-node@v4',
          with: {
            'node-version': '20'
          }
        },
        {
          id: 'install',
          name: 'Install dependencies',
          run: 'npm install'
        }
      ]
    }
  ]
};

// Default reusable workflow template with workflow_call
export const DEFAULT_REUSABLE_WORKFLOW_GUI: WorkflowGUI = {
  name: 'Reusable Workflow',
  events: [{ 
    type: 'workflow_call',
    inputs: {
      environment: {
        description: 'Target environment for deployment',
        required: false,
        type: 'string',
        default: 'staging'
      }
    }
  }],
  jobs: [
    {
      id: 'build',
      name: 'Build',
      runsOn: 'ubuntu-latest',
      steps: [
        {
          id: 'checkout',
          name: 'Checkout code',
          uses: 'actions/checkout@v5'
        },
        {
          id: 'setup-node',
          name: 'Setup Node.js',
          uses: 'actions/setup-node@v4',
          with: {
            'node-version': '20'
          }
        },
        {
          id: 'install',
          name: 'Install dependencies',
          run: 'npm install'
        },
        {
          id: 'build',
          name: 'Build application',
          run: 'npm run build'
        }
      ]
    }
  ]
};

/**
 * Convert YAML to the GUI structure, reporting failure instead of hiding it.
 *
 * `yamlToGui` below silently substitutes the default template when conversion
 * fails, which is fine for rendering but dangerous for the YAML→GUI mode switch:
 * the first GUI edit serialises whatever model is loaded straight back over the
 * document, so a swallowed error turns into lost work. Callers that need to act
 * on the failure use this and check `error`.
 *
 * Empty content is a success with the default model — that is a new workflow,
 * not a broken one.
 */
export function yamlToGuiResult(yamlContent: string): { gui: WorkflowGUI; error: string | null } {
  if (!yamlContent?.trim()) {
    return { gui: DEFAULT_WORKFLOW_GUI, error: null };
  }

  try {
    const parsed = yaml.load(yamlContent) as any;
    if (!parsed || typeof parsed !== 'object') {
      return { gui: DEFAULT_WORKFLOW_GUI, error: 'Workflow YAML is not a mapping.' };
    }

    const result: WorkflowGUI = {
      name: parsed.name ?? '',
      events: parseEvents(parsed.on),
      jobs: parseJobs(parsed.jobs),
      env: parsed.env,
      unsupportedFields: {}
    };

    // Store unsupported top-level fields
    const supportedTopLevel = ['name', 'on', 'jobs', 'env'];
    Object.keys(parsed).forEach(key => {
      if (!supportedTopLevel.includes(key)) {
        result.unsupportedFields![key] = parsed[key];
      }
    });

    return { gui: result, error: null };
  } catch (error) {
    return {
      gui: DEFAULT_WORKFLOW_GUI,
      error: error instanceof Error ? error.message : 'Failed to parse workflow YAML.'
    };
  }
}

// Convert YAML string to GUI structure
export function yamlToGui(yamlContent: string): WorkflowGUI {
  const { gui, error } = yamlToGuiResult(yamlContent);
  if (error) {
    console.warn('Failed to parse YAML, using default template:', error);
  }
  return gui;
}

// Convert GUI structure to YAML string
export function guiToYaml(gui: WorkflowGUI): string {
  // `name:` is optional in GitHub Actions; an empty label means the YAML had
  // none, and writing one would change what the Actions tab shows.
  const workflow: any = {
    ...(gui.name ? { name: gui.name } : {}),
    trigger: serializeEvents(gui.events), // Use 'trigger' temporarily to avoid YAML quoting 'on'
    jobs: serializeJobs(gui.jobs)
  };

  // Add env if present
  if (gui.env && Object.keys(gui.env).length > 0) {
    workflow.env = gui.env;
  }

  // Add unsupported fields back
  if (gui.unsupportedFields) {
    Object.assign(workflow, gui.unsupportedFields);
  }

  const yamlStr = yaml.dump(workflow, {
    indent: 2,
    lineWidth: -1,
    noRefs: true,
    sortKeys: false
  });

  // Replace 'trigger:' with 'on:' to avoid YAML quoting the reserved word 'on'
  return yamlStr.replace(/^trigger:/gm, 'on:');
}

const WHOLE_WORKFLOW = 'entire workflow';
const INPUT_EVENTS = new Set(['workflow_dispatch', 'workflow_call']);

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return Object.prototype.toString.call(value) === '[object Object]';
}

function mapValues(
  record: Record<string, unknown>,
  map: (key: string, value: unknown) => unknown
): Record<string, unknown> {
  return Object.fromEntries(Object.entries(record).map(([key, value]) => [key, map(key, value)]));
}

// GitHub defaults an input to `required: false` and `type: string`, and
// guiToYaml writes both out even when the original omitted them.
function normalizeInput(input: unknown): unknown {
  if (!isPlainObject(input)) {
    return input;
  }
  const normalized = { ...input };
  if (normalized.required === false) delete normalized.required;
  if (normalized.type === 'string') delete normalized.type;
  return normalized;
}

function normalizeEventConfig(event: string, config: unknown): unknown {
  if (config === null) {
    return {};
  }
  if (!INPUT_EVENTS.has(event) || !isPlainObject(config) || !isPlainObject(config.inputs)) {
    return config;
  }
  return { ...config, inputs: mapValues(config.inputs, (_name, input) => normalizeInput(input)) };
}

// `on: push`, `on: [push]` and `on: {push: null}` are the same trigger to GitHub,
// and guiToYaml picks whichever form fits the model, not the one the user wrote.
function normalizeOn(on: unknown): unknown {
  if (typeof on === 'string') {
    return { [on]: {} };
  }
  if (Array.isArray(on) && on.every(event => typeof event === 'string')) {
    return Object.fromEntries(on.map(event => [event, {}]));
  }
  return isPlainObject(on) ? mapValues(on, normalizeEventConfig) : on;
}

function normalizeWorkflow(doc: unknown): unknown {
  return isPlainObject(doc) && Object.hasOwn(doc, 'on') ? { ...doc, on: normalizeOn(doc.on) } : doc;
}

function ownValue(record: Record<string, unknown>, key: string): unknown {
  return Object.hasOwn(record, key) ? record[key] : undefined;
}

function collectDifferences(before: unknown, after: unknown, path: string, differences: string[]): void {
  if (Array.isArray(before) && Array.isArray(after)) {
    const length = Math.max(before.length, after.length);
    for (let index = 0; index < length; index++) {
      collectDifferences(before[index], after[index], `${path}[${index}]`, differences);
    }
    return;
  }
  if (isPlainObject(before) && isPlainObject(after)) {
    new Set([...Object.keys(before), ...Object.keys(after)]).forEach(key => {
      const childPath = path ? `${path}.${key}` : key;
      collectDifferences(ownValue(before, key), ownValue(after, key), childPath, differences);
    });
    return;
  }
  if (!Object.is(before, after)) {
    differences.push(path || WHOLE_WORKFLOW);
  }
}

/**
 * The YAML paths GUI mode would change if it edited `gui` and wrote it back
 * over `originalYaml`; empty means the round trip is lossless.
 *
 * Every GUI edit replaces the whole document with guiToYaml's output, and
 * delivery is byte-exact, so anything the model can't represent would ship to
 * every repository without the user having touched it. Callers refuse GUI mode
 * on a non-empty result. Anything that goes wrong here is reported as a
 * difference, never as safe.
 */
export function findGuiRoundTripLosses(originalYaml: string, gui: WorkflowGUI): string[] {
  try {
    const differences: string[] = [];
    collectDifferences(
      normalizeWorkflow(yaml.load(originalYaml)),
      normalizeWorkflow(yaml.load(guiToYaml(gui))),
      '',
      differences
    );
    return differences;
  } catch {
    return [WHOLE_WORKFLOW];
  }
}

const SUPPORTED_EVENT_FIELDS = new Set(['branches', 'paths', 'tags', 'types', 'inputs']);

// GitHub rejects a workflow that sets both a filter and its `-ignore`
// counterpart on the same event.
const FILTER_FOR_IGNORE_KEY: { [key: string]: string } = {
  'branches-ignore': 'branches',
  'paths-ignore': 'paths',
  'tags-ignore': 'tags'
};

function collectUnsupportedEventFields(eventConfig: any): { [key: string]: any } | undefined {
  const unsupported: { [key: string]: any } = {};
  Object.keys(eventConfig).forEach(key => {
    if (!SUPPORTED_EVENT_FIELDS.has(key)) {
      unsupported[key] = eventConfig[key];
    }
  });
  return Object.keys(unsupported).length > 0 ? unsupported : undefined;
}

// A filter the user set in the GUI wins over a preserved `-ignore` counterpart,
// which would otherwise make the workflow invalid rather than merely lossy.
function applyUnsupportedEventFields(config: any, unsupportedFields: { [key: string]: any }): void {
  Object.keys(unsupportedFields).forEach(key => {
    if (config[FILTER_FOR_IGNORE_KEY[key]]) {
      return;
    }
    config[key] = unsupportedFields[key];
  });
}

function hasEventConfig(event: WorkflowEvent): boolean {
  return Boolean(
    event.branches || event.paths || event.tags || event.types || event.inputs ||
    event.cron !== undefined || event.schedule ||
    (event.unsupportedFields && Object.keys(event.unsupportedFields).length > 0)
  );
}

// `on.schedule` is a list of entries, not a mapping like the other events.
// Before #2017 the GUI wrote the list nested under a second `schedule:` key.
// Reading that too shows its cron, and the round-trip check then refuses GUI
// mode for it, since GUI mode would rewrite it as a list.
function parseSchedule(config: unknown): Pick<WorkflowEvent, 'cron' | 'schedule'> {
  const entries = isPlainObject(config) ? config.schedule : config;
  if (!Array.isArray(entries)) {
    return {};
  }
  const first = entries[0];
  if (isPlainObject(first) && typeof first.cron === 'string') {
    return { cron: first.cron, schedule: entries };
  }
  return { schedule: entries };
}

function isScheduleList(event: WorkflowEvent): boolean {
  return event.type === 'schedule' && (event.schedule !== undefined || event.cron !== undefined);
}

// Whatever the cron box holds is written, even empty, so the YAML never keeps a
// cron the GUI no longer shows.
function serializeSchedule(event: WorkflowEvent): unknown[] {
  const entries = [...(event.schedule ?? [])];
  if (event.cron !== undefined) {
    const first = entries[0];
    entries[0] = isPlainObject(first) ? { ...first, cron: event.cron } : { cron: event.cron };
  }
  return entries;
}

// Parse events from YAML 'on' field
function parseEvents(onField: any): WorkflowEvent[] {
  if (!onField) {
    return [{ type: 'push' }];
  }

  if (typeof onField === 'string') {
    return [{ type: onField as any }];
  }

  if (Array.isArray(onField)) {
    return onField.map(event => ({ type: event }));
  }

  const events: WorkflowEvent[] = [];
  Object.keys(onField).forEach(eventType => {
    const eventConfig = onField[eventType];
    const event: WorkflowEvent = { type: eventType as any };

    if (eventType === 'schedule') {
      Object.assign(event, parseSchedule(eventConfig));
    }

    if (eventConfig && typeof eventConfig === 'object' && !Array.isArray(eventConfig)) {
      if (eventConfig.branches) {
        event.branches = Array.isArray(eventConfig.branches) 
          ? eventConfig.branches 
          : [eventConfig.branches];
      }
      if (eventConfig.paths) {
        event.paths = Array.isArray(eventConfig.paths)
          ? eventConfig.paths
          : [eventConfig.paths];
      }
      if (eventConfig.tags) {
        event.tags = Array.isArray(eventConfig.tags)
          ? eventConfig.tags
          : [eventConfig.tags];
      }
      if (eventConfig.types) {
        event.types = Array.isArray(eventConfig.types) 
          ? eventConfig.types 
          : [eventConfig.types];
      }
      // Handle workflow_call / workflow_dispatch inputs (same YAML shape for
      // both - a manual-trigger workflow's `workflow_dispatch.inputs` was
      // previously silently dropped here since only workflow_call was
      // handled, losing data on every YAML -> GUI -> YAML round trip).
      if ((eventType === 'workflow_call' || eventType === 'workflow_dispatch') && eventConfig.inputs) {
        event.inputs = {};
        Object.keys(eventConfig.inputs).forEach(inputName => {
          const inputConfig = eventConfig.inputs[inputName];
          if (inputConfig && typeof inputConfig === 'object') {
            event.inputs![inputName] = {
              description: inputConfig.description,
              required: inputConfig.required,
              type: inputConfig.type || 'string',
              default: inputConfig.default
            };
            if (inputConfig.type === 'choice' && inputConfig.options) {
              event.inputs![inputName].options = inputConfig.options;
            }
          }
        });
      }

      const unsupported = collectUnsupportedEventFields(eventConfig);
      if (unsupported) {
        event.unsupportedFields = unsupported;
      }
    }

    events.push(event);
  });

  return events.length > 0 ? events : [{ type: 'push' }];
}

// Serialize events to YAML 'on' field
function serializeEvents(events: WorkflowEvent[]): any {
  if (events.length === 0) {
    return ['push'];
  }

  if (events.length === 1) {
    const event = events[0];
    if (!hasEventConfig(event)) {
      return event.type;
    }
  }

  const result: any = {};
  events.forEach(event => {
    if (!hasEventConfig(event)) {
      result[event.type] = null;
    } else if (isScheduleList(event)) {
      result[event.type] = serializeSchedule(event);
    } else {
      const config: any = {};
      if (event.branches && event.branches.length > 0) {
        config.branches = event.branches;
      }
      if (event.paths && event.paths.length > 0) {
        config.paths = event.paths;
      }
      if (event.tags && event.tags.length > 0) {
        config.tags = event.tags;
      }
      if (event.types && event.types.length > 0) {
        config.types = event.types;
      }
      // Handle workflow_call / workflow_dispatch inputs (see matching note
      // in parseEvents above).
      if ((event.type === 'workflow_call' || event.type === 'workflow_dispatch') && event.inputs) {
        config.inputs = {};
        Object.keys(event.inputs).forEach(inputName => {
          const input = event.inputs![inputName];
          config.inputs[inputName] = {
            description: input.description,
            required: input.required || false,
            type: input.type || 'string'
          };
          if (input.default !== undefined) {
            config.inputs[inputName].default = input.default;
          }
          if (input.type === 'choice' && input.options) {
            config.inputs[inputName].options = input.options;
          }
        });
      }

      if (event.unsupportedFields) {
        applyUnsupportedEventFields(config, event.unsupportedFields);
      }

      result[event.type] = config;
    }
  });

  return result;
}

// Parse jobs from YAML jobs field
function parseJobs(jobsField: any): WorkflowJob[] {
  if (!jobsField || typeof jobsField !== 'object') {
    return [];
  }

  return Object.keys(jobsField).map(jobId => {
    const job = jobsField[jobId];
    const result: WorkflowJob = {
      id: jobId,
      name: job.name,
      runsOn: job['runs-on'] || 'ubuntu-latest',
      steps: parseSteps(job.steps || []),
      env: job.env,
      if: job.if,
      needs: job.needs,
      timeoutMinutes: job['timeout-minutes'],
      uses: job.uses,
      unsupportedFields: {}
    };

    // Store unsupported job fields
    const supportedJobFields = new Set(['name', 'runs-on', 'steps', 'env', 'if', 'needs', 'timeout-minutes', 'uses']);
    Object.keys(job).forEach(key => {
      if (!supportedJobFields.has(key)) {
        result.unsupportedFields![key] = job[key];
      }
    });

    return result;
  });
}

// Serialize jobs to YAML jobs field
function serializeJobs(jobs: WorkflowJob[]): any {
  const result: any = {};
  
  jobs.forEach(job => {
    const jobConfig: any = job.uses === undefined
      ? { 'runs-on': job.runsOn, steps: serializeSteps(job.steps) }
      : {};

    if (job.name) jobConfig.name = job.name;
    if (job.env && Object.keys(job.env).length > 0) jobConfig.env = job.env;
    if (job.if) jobConfig.if = job.if;
    if (job.needs && job.needs.length > 0) jobConfig.needs = job.needs;
    if (job.timeoutMinutes) jobConfig['timeout-minutes'] = job.timeoutMinutes;
    if (job.uses !== undefined) jobConfig.uses = job.uses;

    // Add unsupported fields back
    if (job.unsupportedFields) {
      Object.assign(jobConfig, job.unsupportedFields);
    }

    result[job.id] = jobConfig;
  });

  return result;
}

// Parse steps from YAML steps array
function parseSteps(stepsField: any[]): WorkflowStep[] {
  if (!Array.isArray(stepsField)) {
    return [];
  }

  return stepsField.map((step, index) => {
    const result: WorkflowStep = {
      id: step.id || `step-${index + 1}`,
      yamlId: step.id,
      name: step.name,
      uses: step.uses,
      run: step.run,
      with: step.with,
      env: step.env,
      if: step.if,
      shell: step.shell,
      workingDirectory: step['working-directory'],
      continueOnError: step['continue-on-error'],
      timeoutMinutes: step['timeout-minutes'],
      unsupportedFields: {}
    };

    // Store unsupported step fields
    const supportedStepFields = [
      'id', 'name', 'uses', 'run', 'with', 'env', 'if', 'shell', 
      'working-directory', 'continue-on-error', 'timeout-minutes'
    ];
    Object.keys(step).forEach(key => {
      if (!supportedStepFields.includes(key)) {
        result.unsupportedFields![key] = step[key];
      }
    });

    return result;
  });
}

// Serialize steps to YAML steps array
function serializeSteps(steps: WorkflowStep[]): any[] {
  return steps.map(step => {
    const stepConfig: any = {};

    if (step.name) stepConfig.name = step.name;
    if (step.yamlId !== undefined) stepConfig.id = step.yamlId;
    if (step.uses) stepConfig.uses = step.uses;
    if (step.run) stepConfig.run = step.run;
    if (step.with && Object.keys(step.with).length > 0) stepConfig.with = step.with;
    if (step.env && Object.keys(step.env).length > 0) stepConfig.env = step.env;
    if (step.if) stepConfig.if = step.if;
    if (step.shell) stepConfig.shell = step.shell;
    if (step.workingDirectory) stepConfig['working-directory'] = step.workingDirectory;
    if (step.continueOnError !== undefined) stepConfig['continue-on-error'] = step.continueOnError;
    if (step.timeoutMinutes) stepConfig['timeout-minutes'] = step.timeoutMinutes;

    // Add unsupported fields back
    if (step.unsupportedFields) {
      Object.assign(stepConfig, step.unsupportedFields);
    }

    return stepConfig;
  });
}

// Validation functions
export interface ValidationError {
  field: string;
  message: string;
  severity: 'error' | 'warning';
}

export function validateWorkflow(gui: WorkflowGUI): ValidationError[] {
  const errors: ValidationError[] = [];

  // No check on `name`: it is optional in GitHub Actions, which shows the
  // file path when a workflow has none.

  // Validate events
  if (!gui.events || gui.events.length === 0) {
    errors.push({
      field: 'events',
      message: 'At least one trigger event is required',
      severity: 'error'
    });
  }

  // Validate jobs
  if (!gui.jobs || gui.jobs.length === 0) {
    errors.push({
      field: 'jobs',
      message: 'At least one job is required',
      severity: 'error'
    });
  } else {
    gui.jobs.forEach((job, jobIndex) => {
      // Validate job ID
      if (!job.id || job.id.trim() === '') {
        errors.push({
          field: `jobs[${jobIndex}].id`,
          message: 'Job ID is required',
          severity: 'error'
        });
      }

      // Validate runner. runs-on may also be a label list or a group map, kept as read.
      if (!job.runsOn || (typeof job.runsOn === 'string' && job.runsOn.trim() === '')) {
        errors.push({
          field: `jobs[${jobIndex}].runsOn`,
          message: 'Runner is required',
          severity: 'error'
        });
      }

      validateJobSteps(job, jobIndex, errors);
    });
  }

  return errors;
}

// A job calling a reusable workflow has no steps of its own.
function validateJobSteps(job: WorkflowJob, jobIndex: number, errors: ValidationError[]): void {
  if (job.uses !== undefined) {
    return;
  }

  if (!job.steps || job.steps.length === 0) {
    errors.push({
      field: `jobs[${jobIndex}].steps`,
      message: 'At least one step is required',
      severity: 'warning'
    });
    return;
  }

  job.steps.forEach((step, stepIndex) => {
    // Validate step has either uses or run
    if (!step.uses && !step.run) {
      errors.push({
        field: `jobs[${jobIndex}].steps[${stepIndex}]`,
        message: 'Step must have either "uses" (action) or "run" (script)',
        severity: 'error'
      });
    }

    // Validate action version if uses is present
    if (step.uses && !step.uses.includes('@')) {
      errors.push({
        field: `jobs[${jobIndex}].steps[${stepIndex}].uses`,
        message: 'Action must include a version (e.g., @v4)',
        severity: 'warning'
      });
    }
  });
}