import {
  yamlToGui,
  yamlToGuiResult,
  guiToYaml,
  findGuiRoundTripLosses,
  validateWorkflow,
  WorkflowGUI,
  DEFAULT_WORKFLOW_GUI,
  DEFAULT_REUSABLE_WORKFLOW_GUI
} from './workflowGuiConversion';

describe('workflowGuiConversion', () => {
  describe('yamlToGui', () => {
    test('should convert simple YAML to GUI structure', () => {
      const yaml = `name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5`;

      const result = yamlToGui(yaml);

      expect(result.name).toBe('CI');
      expect(result.events).toHaveLength(1);
      expect(result.events[0].type).toBe('push');
      expect(result.jobs).toHaveLength(1);
      expect(result.jobs[0].id).toBe('build');
      expect(result.jobs[0].runsOn).toBe('ubuntu-latest');
    });

    test('should handle workflow with multiple events', () => {
      const yaml = `name: Multi Event
on:
  push:
    branches:
      - main
  pull_request:
    branches:
      - main
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5`;

      const result = yamlToGui(yaml);

      expect(result.events).toHaveLength(2);
      expect(result.events[0].type).toBe('push');
      expect(result.events[0].branches).toEqual(['main']);
      expect(result.events[1].type).toBe('pull_request');
    });

    test('should handle workflow_call event with inputs', () => {
      const yaml = `name: Reusable
on:
  workflow_call:
    inputs:
      environment:
        description: "Environment to deploy to"
        required: true
        type: string
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - run: echo "Deploying"`;

      const result = yamlToGui(yaml);

      expect(result.events).toHaveLength(1);
      expect(result.events[0].type).toBe('workflow_call');
      expect(result.events[0].inputs).toBeDefined();
      expect(result.events[0].inputs?.environment).toBeDefined();
      expect(result.events[0].inputs?.environment.required).toBe(true);
    });

    test('should handle workflow_dispatch event with inputs', () => {
      const yaml = `name: Manual Deploy
on:
  workflow_dispatch:
    inputs:
      environment:
        description: "Environment to deploy to"
        required: true
        type: choice
        options:
          - staging
          - production
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - run: echo "Deploying"`;

      const result = yamlToGui(yaml);

      expect(result.events).toHaveLength(1);
      expect(result.events[0].type).toBe('workflow_dispatch');
      expect(result.events[0].inputs).toBeDefined();
      expect(result.events[0].inputs?.environment).toBeDefined();
      expect(result.events[0].inputs?.environment.required).toBe(true);
      expect(result.events[0].inputs?.environment.type).toBe('choice');
      expect(result.events[0].inputs?.environment.options).toEqual(['staging', 'production']);
    });

    test('should handle push event with tags', () => {
      const yaml = `name: Release
on:
  push:
    tags:
      - "v*"
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5`;

      const result = yamlToGui(yaml);

      expect(result.events).toHaveLength(1);
      expect(result.events[0].type).toBe('push');
      expect(result.events[0].tags).toEqual(['v*']);
    });

    test('should handle schedule event', () => {
      const yaml = `name: Scheduled
on:
  schedule:
    - cron: "0 0 * * *"
jobs:
  nightly:
    runs-on: ubuntu-latest
    steps:
      - run: echo "Running scheduled job"`;

      const result = yamlToGui(yaml);

      expect(result.events).toHaveLength(1);
      expect(result.events[0].type).toBe('schedule');
      expect(result.events[0].cron).toBe('0 0 * * *');
    });

    test('should handle jobs with environment variables', () => {
      const yaml = `name: With Env
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      NODE_ENV: production
    steps:
      - run: npm build`;

      const result = yamlToGui(yaml);

      expect(result.jobs[0].env).toBeDefined();
      expect(result.jobs[0].env?.NODE_ENV).toBe('production');
    });

    test('should handle steps with various properties', () => {
      const yaml = `name: Complex Steps
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v5
        with:
          fetch-depth: 0
      - name: Run tests
        run: npm test
        env:
          TEST_VAR: value
      - name: Build
        run: npm build
        if: success()`;

      const result = yamlToGui(yaml);

      expect(result.jobs[0].steps).toHaveLength(3);
      
      // Check first step
      expect(result.jobs[0].steps[0].name).toBe('Checkout');
      expect(result.jobs[0].steps[0].uses).toBe('actions/checkout@v5');
      expect(result.jobs[0].steps[0].with).toBeDefined();
      
      // Check second step
      expect(result.jobs[0].steps[1].name).toBe('Run tests');
      expect(result.jobs[0].steps[1].run).toBe('npm test');
      expect(result.jobs[0].steps[1].env).toBeDefined();
      
      // Check third step
      expect(result.jobs[0].steps[2].if).toBe('success()');
    });

    test('should handle invalid YAML and return default template', () => {
      const invalidYaml = 'this is not valid yaml: [';

      const result = yamlToGui(invalidYaml);

      expect(result).toBeDefined();
      expect(result.name).toBe(DEFAULT_WORKFLOW_GUI.name);
    });

    test('should handle empty YAML and return default template', () => {
      const result = yamlToGui('');

      expect(result).toBeDefined();
      expect(result.name).toBe(DEFAULT_WORKFLOW_GUI.name);
    });

    test('should preserve unsupported top-level fields', () => {
      const yaml = `name: With Unsupported
on: push
permissions:
  contents: read
concurrency:
  group: ci-group
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo test`;

      const result = yamlToGui(yaml);

      expect(result.unsupportedFields).toBeDefined();
      expect(result.unsupportedFields?.permissions).toBeDefined();
      expect(result.unsupportedFields?.concurrency).toBeDefined();
    });

    test('should handle workflow with global env variables', () => {
      const yaml = `name: Global Env
on: push
env:
  GLOBAL_VAR: value
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo test`;

      const result = yamlToGui(yaml);

      expect(result.env).toBeDefined();
      expect(result.env?.GLOBAL_VAR).toBe('value');
    });
  });

  describe('guiToYaml', () => {
    test('should convert simple GUI structure to YAML', () => {
      const gui: WorkflowGUI = {
        name: 'Test Workflow',
        events: [{ type: 'push' }],
        jobs: [
          {
            id: 'build',
            name: 'Build',
            runsOn: 'ubuntu-latest',
            steps: [
              {
                id: 'checkout',
                uses: 'actions/checkout@v5'
              }
            ]
          }
        ]
      };

      const result = guiToYaml(gui);

      expect(result).toContain('name: Test Workflow');
      expect(result).toContain('on: push');
      expect(result).toContain('build:');
      expect(result).toContain('runs-on: ubuntu-latest');
      expect(result).toContain('uses: actions/checkout@v5');
    });

    test('should convert GUI with multiple events to YAML', () => {
      const gui: WorkflowGUI = {
        name: 'Multi Event',
        events: [
          { type: 'push', branches: ['main'] },
          { type: 'pull_request', branches: ['main'] }
        ],
        jobs: [
          {
            id: 'test',
            runsOn: 'ubuntu-latest',
            steps: [{ id: 'test', run: 'npm test' }]
          }
        ]
      };

      const result = guiToYaml(gui);

      expect(result).toContain('push:');
      expect(result).toContain('pull_request:');
      expect(result).toContain('branches:');
      expect(result).toContain('- main');
    });

    test('should convert workflow_call event with inputs to YAML', () => {
      const gui: WorkflowGUI = {
        name: 'Reusable',
        events: [
          {
            type: 'workflow_call',
            inputs: {
              environment: {
                description: 'Environment',
                required: true,
                type: 'string',
                default: 'staging'
              }
            }
          }
        ],
        jobs: [
          {
            id: 'deploy',
            runsOn: 'ubuntu-latest',
            steps: [{ id: 'deploy', run: 'echo deploying' }]
          }
        ]
      };

      const result = guiToYaml(gui);

      expect(result).toContain('workflow_call:');
      expect(result).toContain('inputs:');
      expect(result).toContain('environment:');
      expect(result).toContain('required: true');
    });

    test('should convert workflow_dispatch event with inputs to YAML', () => {
      const gui: WorkflowGUI = {
        name: 'Manual Deploy',
        events: [
          {
            type: 'workflow_dispatch',
            inputs: {
              environment: {
                description: 'Environment',
                required: true,
                type: 'string',
                default: 'staging'
              }
            }
          }
        ],
        jobs: [
          {
            id: 'deploy',
            runsOn: 'ubuntu-latest',
            steps: [{ id: 'deploy', run: 'echo deploying' }]
          }
        ]
      };

      const result = guiToYaml(gui);

      expect(result).toContain('workflow_dispatch:');
      expect(result).toContain('inputs:');
      expect(result).toContain('environment:');
      expect(result).toContain('required: true');
    });

    test('should convert push event with tags to YAML', () => {
      const gui: WorkflowGUI = {
        name: 'Release',
        events: [{ type: 'push', tags: ['v*'] }],
        jobs: [
          {
            id: 'release',
            runsOn: 'ubuntu-latest',
            steps: [{ id: 'checkout', uses: 'actions/checkout@v5' }]
          }
        ]
      };

      const result = guiToYaml(gui);

      expect(result).toContain('push:');
      expect(result).toContain('tags:');
      expect(result).toContain('v*');
      expect(result).not.toContain('tag_push');
    });

    test('should preserve environment variables in YAML', () => {
      const gui: WorkflowGUI = {
        name: 'With Env',
        events: [{ type: 'push' }],
        env: {
          NODE_ENV: 'production',
          API_URL: 'https://api.example.com'
        },
        jobs: [
          {
            id: 'build',
            runsOn: 'ubuntu-latest',
            steps: [{ id: 'build', run: 'npm build' }]
          }
        ]
      };

      const result = guiToYaml(gui);

      expect(result).toContain('env:');
      expect(result).toContain('NODE_ENV: production');
      expect(result).toContain('API_URL: https://api.example.com');
    });

    test('should preserve unsupported fields in YAML', () => {
      const gui: WorkflowGUI = {
        name: 'With Unsupported',
        events: [{ type: 'push' }],
        jobs: [
          {
            id: 'build',
            runsOn: 'ubuntu-latest',
            steps: [{ id: 'build', run: 'npm build' }]
          }
        ],
        unsupportedFields: {
          permissions: { contents: 'read' },
          concurrency: { group: 'ci-group' }
        }
      };

      const result = guiToYaml(gui);

      expect(result).toContain('permissions:');
      expect(result).toContain('concurrency:');
    });

    test('should handle steps with complex properties', () => {
      const gui: WorkflowGUI = {
        name: 'Complex Steps',
        events: [{ type: 'push' }],
        jobs: [
          {
            id: 'test',
            runsOn: 'ubuntu-latest',
            steps: [
              {
                id: 'checkout',
                name: 'Checkout code',
                uses: 'actions/checkout@v5',
                with: {
                  'fetch-depth': '0'
                }
              },
              {
                id: 'test',
                name: 'Run tests',
                run: 'npm test',
                env: {
                  TEST_VAR: 'value'
                },
                if: 'success()'
              }
            ]
          }
        ]
      };

      const result = guiToYaml(gui);

      expect(result).toContain('name: Checkout code');
      expect(result).toContain('uses: actions/checkout@v5');
      expect(result).toContain('with:');
      expect(result).toContain('fetch-depth:');
      expect(result).toContain('name: Run tests');
      expect(result).toContain('run: npm test');
      expect(result).toContain('if: success()');
    });

    test('should not include empty env objects', () => {
      const gui: WorkflowGUI = {
        name: 'No Env',
        events: [{ type: 'push' }],
        env: {},
        jobs: [
          {
            id: 'build',
            runsOn: 'ubuntu-latest',
            steps: [{ id: 'build', run: 'npm build' }]
          }
        ]
      };

      const result = guiToYaml(gui);

      // The env field should not appear in YAML if empty
      const lines = result.split('\n');
      const hasEnvLine = lines.some(line => line.trim() === 'env:');
      expect(hasEnvLine).toBe(false);
    });
  });

  describe('round-trip conversion', () => {
    test('should maintain data integrity through YAML -> GUI -> YAML conversion', () => {
      const originalYaml = `name: CI Pipeline
on:
  push:
    branches:
      - main
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v5
      - name: Build
        run: npm build`;

      const gui = yamlToGui(originalYaml);
      const convertedYaml = guiToYaml(gui);
      const guiAgain = yamlToGui(convertedYaml);

      expect(guiAgain.name).toBe(gui.name);
      expect(guiAgain.events).toHaveLength(gui.events.length);
      expect(guiAgain.jobs).toHaveLength(gui.jobs.length);
      expect(guiAgain.jobs[0].steps).toHaveLength(gui.jobs[0].steps.length);
    });

    test('should maintain reusable workflow structure through conversion', () => {
      const originalYaml = `name: Reusable Workflow
on:
  workflow_call:
    inputs:
      environment:
        required: true
        type: string
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - run: echo test`;

      const gui = yamlToGui(originalYaml);
      const convertedYaml = guiToYaml(gui);

      expect(convertedYaml).toContain('workflow_call:');
      expect(convertedYaml).toContain('inputs:');
      expect(convertedYaml).toContain('environment:');
    });

    test('should not drop workflow_dispatch inputs through YAML -> GUI -> YAML conversion', () => {
      // Regression test: workflow_dispatch.inputs was previously silently
      // dropped by parseEvents/serializeEvents, which only handled
      // workflow_call.inputs - a manually-triggered workflow's inputs would
      // vanish the moment a user switched from YAML mode to GUI mode and back.
      const originalYaml = `name: Manual Deploy
on:
  workflow_dispatch:
    inputs:
      environment:
        description: "Target environment"
        required: true
        type: choice
        options:
          - staging
          - production
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - run: echo deploying`;

      const gui = yamlToGui(originalYaml);
      const convertedYaml = guiToYaml(gui);
      const guiAgain = yamlToGui(convertedYaml);

      expect(convertedYaml).toContain('workflow_dispatch:');
      expect(convertedYaml).toContain('inputs:');
      expect(convertedYaml).toContain('environment:');
      expect(guiAgain.events[0].inputs?.environment).toBeDefined();
      expect(guiAgain.events[0].inputs?.environment.required).toBe(true);
      expect(guiAgain.events[0].inputs?.environment.options).toEqual(['staging', 'production']);
    });

    test('should not corrupt tag-triggered push workflows through conversion', () => {
      // Regression test: the editor previously exposed a "Tag Push" trigger
      // that serialized to an invalid `tag_push:` YAML key (not a real
      // GitHub Actions event - GitHub silently ignores it, so the workflow
      // never actually triggers). Tag-triggered pushes are really the `push`
      // event filtered by `tags:`.
      const originalYaml = `name: Release
on:
  push:
    tags:
      - "v*"
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5`;

      const gui = yamlToGui(originalYaml);
      const convertedYaml = guiToYaml(gui);
      const guiAgain = yamlToGui(convertedYaml);

      expect(convertedYaml).not.toContain('tag_push');
      expect(convertedYaml).toContain('push:');
      expect(convertedYaml).toContain('tags:');
      expect(guiAgain.events[0].type).toBe('push');
      expect(guiAgain.events[0].tags).toEqual(['v*']);
    });

    // Regression tests: parseEvents modelled only a fixed set of keys under
    // `on:` and, unlike parseJobs/parseSteps, kept nothing else. A reusable
    // workflow's `workflow_call.secrets` and `.outputs` - its public interface
    // - were silently dropped the moment it was opened in GUI mode and saved,
    // breaking every caller and drifting every repo already holding the file.
    const REUSABLE_WITH_SECRETS = `name: Reusable Deploy
on:
  workflow_call:
    secrets:
      NPM_TOKEN:
        required: true
      SLACK_WEBHOOK:
        required: false
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - run: npm publish
`;

    const REUSABLE_WITH_OUTPUTS = `name: Reusable Build
on:
  workflow_call:
    outputs:
      image-tag:
        description: Published image tag
        value: \${{ jobs.build.outputs.tag }}
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: npm run build
`;

    const REUSABLE_WITH_BOTH = `name: Reusable Release
on:
  workflow_call:
    inputs:
      environment:
        description: Target environment
        required: true
        type: string
    outputs:
      image-tag:
        description: Published image tag
        value: \${{ jobs.release.outputs.tag }}
    secrets:
      NPM_TOKEN:
        required: true
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - run: npm publish
`;

    test('should round-trip workflow_call secrets byte-identically', () => {
      expect(guiToYaml(yamlToGui(REUSABLE_WITH_SECRETS))).toBe(REUSABLE_WITH_SECRETS);
    });

    test('should round-trip workflow_call outputs byte-identically', () => {
      expect(guiToYaml(yamlToGui(REUSABLE_WITH_OUTPUTS))).toBe(REUSABLE_WITH_OUTPUTS);
    });

    test('should round-trip workflow_call secrets and outputs together byte-identically', () => {
      expect(guiToYaml(yamlToGui(REUSABLE_WITH_BOTH))).toBe(REUSABLE_WITH_BOTH);
    });

    test('should keep workflow_call secrets and outputs when a step is edited', () => {
      const gui = yamlToGui(REUSABLE_WITH_BOTH);
      gui.jobs[0].steps[0].run = 'npm publish --provenance';

      const editedYaml = guiToYaml(gui);

      expect(editedYaml).toBe(REUSABLE_WITH_BOTH.replace('npm publish', 'npm publish --provenance'));
      expect(yamlToGui(editedYaml).events[0].unsupportedFields).toEqual({
        outputs: {
          'image-tag': {
            description: 'Published image tag',
            value: '${{ jobs.release.outputs.tag }}'
          }
        },
        secrets: { NPM_TOKEN: { required: true } }
      });
    });

    test('should leave a workflow without workflow_call unaffected', () => {
      const originalYaml = `name: CI
on:
  push:
    branches:
      - main
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: npm ci
`;

      const gui = yamlToGui(originalYaml);

      expect(gui.events[0].unsupportedFields).toBeUndefined();
      expect(guiToYaml(gui)).toBe(originalYaml);
    });

    test('should keep unmodelled push filters such as paths-ignore', () => {
      const originalYaml = `name: Docs
on:
  push:
    branches:
      - main
    paths-ignore:
      - docs/**
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: npm ci
`;

      expect(guiToYaml(yamlToGui(originalYaml))).toBe(originalYaml);
    });

    test('should drop a preserved -ignore filter when the GUI sets its counterpart', () => {
      // GitHub rejects an event carrying both `branches` and `branches-ignore`,
      // so preserving the ignore variant must not turn a lossy save into an
      // invalid workflow: the filter the user just set in the GUI wins.
      const originalYaml = `name: CI
on:
  push:
    branches-ignore:
      - gh-pages
    paths-ignore:
      - docs/**
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: npm ci
`;

      const gui = yamlToGui(originalYaml);
      const edited = {
        ...gui,
        events: [{ ...gui.events[0], branches: ['main'], paths: ['src/**'] }]
      };

      const editedYaml = guiToYaml(edited);

      expect(editedYaml).toContain('branches:');
      expect(editedYaml).toContain('paths:');
      expect(editedYaml).not.toContain('branches-ignore');
      expect(editedYaml).not.toContain('paths-ignore');
    });

    test('should keep unmodelled keys that appear before the modelled ones', () => {
      // Preserved keys are re-emitted after the modelled ones, so a workflow
      // that lists them first comes back reordered - lossless, but not
      // byte-identical. Pinning that so the difference is a decision, not a
      // surprise.
      const originalYaml = `name: Reusable
on:
  workflow_call:
    secrets:
      NPM_TOKEN:
        required: true
    inputs:
      environment:
        description: Target environment
        required: true
        type: string
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - run: npm publish
`;

      const convertedYaml = guiToYaml(yamlToGui(originalYaml));

      expect(convertedYaml).toContain('NPM_TOKEN');
      expect(convertedYaml).toContain('environment');
      expect(convertedYaml.indexOf('inputs:')).toBeLessThan(convertedYaml.indexOf('secrets:'));
    });
  });

  describe('on.schedule (#2017)', () => {
    const BUILD = 'jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: npm ci\n';
    const NIGHTLY = `name: Nightly\non:\n  schedule:\n    - cron: 0 0 * * *\n${BUILD}`;
    const TWO_SCHEDULES =
      'name: Twice\non:\n  schedule:\n' +
      '    - cron: 0 9 * * 1-5\n      timezone: America/New_York\n' +
      `    - cron: 0 0 * * 0\n${BUILD}`;

    test('round-trips a single cron byte-identically', () => {
      expect(guiToYaml(yamlToGui(NIGHTLY))).toBe(NIGHTLY);
    });

    test('keeps every entry and its timezone byte-identically', () => {
      expect(guiToYaml(yamlToGui(TWO_SCHEDULES))).toBe(TWO_SCHEDULES);
    });

    test('a GUI cron edit changes only the first entry', () => {
      const gui = yamlToGui(TWO_SCHEDULES);
      gui.events[0].cron = '30 6 * * 1-5';

      expect(guiToYaml(gui)).toBe(TWO_SCHEDULES.replace('0 9 * * 1-5', '30 6 * * 1-5'));
    });

    // The old GUI's nested form is invalid on GitHub; GUI mode would rewrite it.
    test('reads the old nested schedule form and refuses to rewrite it', () => {
      const nested = `name: Nightly\non:\n  schedule:\n    schedule:\n      - cron: 0 0 * * *\n${BUILD}`;
      const gui = yamlToGui(nested);

      expect(gui.events[0].cron).toBe('0 0 * * *');
      expect(findGuiRoundTripLosses(nested, gui)).toEqual(['on.schedule']);
    });

    test('writes a schedule added in the GUI as an on.schedule list', () => {
      const gui = yamlToGui(`name: CI\non: push\n${BUILD}`);
      gui.events = [...gui.events, { type: 'schedule', cron: '0 0 * * *' }];

      expect(guiToYaml(gui)).toBe(`name: CI\non:\n  push: null\n  schedule:\n    - cron: 0 0 * * *\n${BUILD}`);
    });

    test('writes a schedule that is the only trigger as an on.schedule list', () => {
      const gui = yamlToGui(`name: CI\non: push\n${BUILD}`);
      gui.events = [{ type: 'schedule', cron: '0 0 * * *' }];

      expect(guiToYaml(gui)).toBe(`name: CI\non:\n  schedule:\n    - cron: 0 0 * * *\n${BUILD}`);
    });

    test('writes what the cron box holds, even once it is cleared', () => {
      const gui = yamlToGui(NIGHTLY);
      gui.events[0].cron = '';

      expect(guiToYaml(gui)).toBe(NIGHTLY.replace('- cron: 0 0 * * *', "- cron: ''"));
    });
  });

  describe('step ids', () => {
    const STEP_IDS =
      'name: Ids\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n' +
      '      - name: Read version\n        id: meta\n        run: echo "version=1" >> "$GITHUB_OUTPUT"\n' +
      '      - run: echo ${{ steps.meta.outputs.version }}\n';

    test('round-trips a step id and the reference to it byte-identically', () => {
      expect(guiToYaml(yamlToGui(STEP_IDS))).toBe(STEP_IDS);
    });

    test('writes no id for a step that had none', () => {
      const gui = yamlToGui(STEP_IDS);

      expect(gui.jobs[0].steps[1].id).toBe('step-2');
      expect(gui.jobs[0].steps[1].yamlId).toBeUndefined();
    });

    test('writes no id for a step added in the GUI', () => {
      const gui = yamlToGui(STEP_IDS);
      gui.jobs[0].steps.push({ id: 'step-3', name: 'Step 3', run: 'make' });

      expect(guiToYaml(gui)).toBe(`${STEP_IDS}      - name: Step 3\n        run: make\n`);
    });
  });

  describe('jobs that call a reusable workflow', () => {
    const CALLER =
      'name: Deploy\non: push\njobs:\n' +
      '  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make\n' +
      '  deploy:\n    name: Deploy to staging\n    needs:\n      - build\n' +
      '    uses: org/repo/.github/workflows/deploy.yml@main\n' +
      '    with:\n      environment: staging\n    secrets: inherit\n';

    test('round-trips a caller job byte-identically', () => {
      expect(guiToYaml(yamlToGui(CALLER))).toBe(CALLER);
    });

    test('models uses and keeps with and secrets as written', () => {
      const deploy = yamlToGui(CALLER).jobs[1];

      expect(deploy.uses).toBe('org/repo/.github/workflows/deploy.yml@main');
      expect(deploy.unsupportedFields).toEqual({ with: { environment: 'staging' }, secrets: 'inherit' });
    });

    test('writes no runs-on or steps for a caller job after a GUI edit', () => {
      const gui = yamlToGui(CALLER);
      gui.jobs[1].name = 'Deploy to production';

      expect(guiToYaml(gui)).toBe(CALLER.replace('Deploy to staging', 'Deploy to production'));
    });

    test('does not warn that a caller job has no steps', () => {
      const errors = validateWorkflow(yamlToGui(CALLER));

      expect(errors.filter(error => error.field.startsWith('jobs[1]'))).toEqual([]);
    });
  });

  // Kept as read, so GUI mode must cope with the other shapes GitHub allows.
  describe('job fields kept as written', () => {
    test('round-trips a single needs: string', () => {
      const content = `name: CI\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make\n  test:\n    runs-on: ubuntu-latest\n    needs: build\n    steps:\n      - run: make test\n`;
      const gui = yamlToGui(content);

      expect(gui.jobs[1].needs).toBe('build');
      expect(findGuiRoundTripLosses(content, gui)).toEqual([]);
    });

    test('validates a runs-on label list without throwing', () => {
      const content = `name: CI\non: push\njobs:\n  build:\n    runs-on:\n      - self-hosted\n      - linux\n    steps:\n      - run: make\n`;
      const gui = yamlToGui(content);

      expect(findGuiRoundTripLosses(content, gui)).toEqual([]);
      expect(validateWorkflow(gui).filter(error => error.field === 'jobs[0].runsOn')).toEqual([]);
    });
  });

  describe('yamlToGuiResult', () => {
    test('reports no error and a populated model for valid YAML', () => {
      const { gui, error } = yamlToGuiResult('name: CI\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: npm ci\n');

      expect(error).toBeNull();
      expect(gui.name).toBe('CI');
      expect(gui.jobs).toHaveLength(1);
      expect(gui.jobs[0].steps[0].run).toBe('npm ci');
    });

    test('reports an error for YAML that does not parse', () => {
      const { error } = yamlToGuiResult('name: [unclosed\non: push');

      expect(error).toBeTruthy();
      expect(typeof error).toBe('string');
    });

    test('reports an error when the document is not a mapping', () => {
      expect(yamlToGuiResult('just a bare string').error).toBeTruthy();
    });

    test('treats empty content as a new workflow, not a failure', () => {
      expect(yamlToGuiResult('').error).toBeNull();
      expect(yamlToGuiResult('   \n  ').error).toBeNull();
      expect(yamlToGuiResult('').gui).toEqual(DEFAULT_WORKFLOW_GUI);
    });

    test('yamlToGui keeps swallowing failures for existing callers', () => {
      const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});

      expect(yamlToGui('name: [unclosed')).toEqual(DEFAULT_WORKFLOW_GUI);
      expect(() => yamlToGui('name: [unclosed')).not.toThrow();

      warn.mockRestore();
    });

    test('yamlToGui still converts valid YAML identically', () => {
      const content = 'name: CI\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: npm ci\n';
      expect(yamlToGui(content)).toEqual(yamlToGuiResult(content).gui);
    });
  });

  describe('findGuiRoundTripLosses', () => {
    const doc = (...lines: string[]) => `${lines.join('\n')}\n`;
    const losses = (content: string) => findGuiRoundTripLosses(content, yamlToGuiResult(content).gui);

    const BUILD_JOB = ['jobs:', '  build:', '    runs-on: ubuntu-latest', '    steps:', '      - run: make'];

    test.each([
      ['push/pull_request with branch, path and tag filters and types', doc(
        'name: CI',
        'on:',
        '  push:',
        "    branches: [main, 'release/**']",
        "    paths: ['src/**']",
        "    tags: ['v*']",
        '  pull_request:',
        '    types: [opened, synchronize]',
        '    branches: [main]',
        "    paths-ignore: ['docs/**']",
        ...BUILD_JOB
      )],
      ['a job with matrix, needs, if, services, container, environment and outputs', doc(
        'name: Matrix',
        'on: push',
        'jobs:',
        '  setup:',
        '    runs-on: ubuntu-latest',
        '    outputs:',
        '      sha: ${{ github.sha }}',
        '    steps:',
        '      - run: echo setup',
        '  test:',
        '    needs: [setup]',
        "    if: github.event_name == 'push'",
        '    runs-on: ${{ matrix.os }}',
        '    environment: staging',
        '    container: node:20',
        '    services:',
        '      redis:',
        '        image: redis',
        "        ports: ['6379:6379']",
        '    strategy:',
        '      fail-fast: false',
        '      matrix:',
        '        os: [ubuntu-latest, windows-latest]',
        '        node: [18, 20]',
        '    timeout-minutes: 30',
        '    env:',
        '      CI: true',
        '    steps:',
        '      - uses: actions/setup-node@v4',
        '        with:',
        '          node-version: ${{ matrix.node }}',
        '      - name: Test',
        '        run: npm test',
        '        shell: bash',
        '        working-directory: app',
        '        continue-on-error: false',
        '        timeout-minutes: 10',
        '        if: always()',
        '        env:',
        '          NODE: ${{ matrix.node }}'
      )],
      ['top-level permissions, concurrency, defaults and env', doc(
        'name: Release',
        'on: workflow_dispatch',
        'permissions:',
        '  contents: read',
        '  id-token: write',
        'concurrency:',
        '  group: release-${{ github.ref }}',
        '  cancel-in-progress: true',
        'defaults:',
        '  run:',
        '    shell: bash',
        'env:',
        '  NODE_ENV: production',
        ...BUILD_JOB
      )],
      ['workflow_call with inputs, secrets and outputs', doc(
        'name: Reusable build',
        'on:',
        '  workflow_call:',
        '    inputs:',
        '      environment:',
        '        description: Target environment',
        '        required: true',
        '        type: string',
        '      debug:',
        '        type: boolean',
        '        default: false',
        '    secrets:',
        '      token:',
        '        required: true',
        '    outputs:',
        '      artifact:',
        '        value: ${{ jobs.build.outputs.artifact }}',
        'jobs:',
        '  build:',
        '    runs-on: ubuntu-latest',
        '    outputs:',
        '      artifact: dist.zip',
        '    steps:',
        '      - run: make'
      )],
      ['workflow_dispatch inputs that omit required and type', doc(
        'name: Manual',
        'on:',
        '  workflow_dispatch:',
        '    inputs:',
        '      level:',
        '        type: choice',
        '        options: [info, debug]',
        '        default: info',
        '      note:',
        '        description: Free text',
        ...BUILD_JOB
      )],
      ['`on` as a string', doc('name: CI', 'on: push', ...BUILD_JOB)],
      ['`on` as a list', doc('name: CI', 'on: [push, pull_request]', ...BUILD_JOB)],
      ['`on` as a map with empty events', doc('name: CI', 'on:', '  push:', '  workflow_dispatch:', ...BUILD_JOB)],
      ['no name: label', doc('on: push', ...BUILD_JOB)],
      ['step ids and references to them', doc(
        'name: Ids',
        'on: push',
        'jobs:',
        '  build:',
        '    runs-on: ubuntu-latest',
        '    steps:',
        '      - id: meta',
        '        run: echo "x=1" >> "$GITHUB_OUTPUT"',
        '      - run: echo ${{ steps.meta.outputs.x }}'
      )],
      ['a job that calls a reusable workflow', doc(
        'name: Caller',
        'on: push',
        'jobs:',
        '  call:',
        '    uses: org/repo/.github/workflows/ci.yml@main',
        '    secrets: inherit'
      )],
      ['schedule entries with a timezone (#2017)', doc(
        'name: Nightly',
        'on:',
        '  schedule:',
        "    - cron: '0 3 * * *'",
        '      timezone: Europe/London',
        "    - cron: '0 4 * * 0'",
        ...BUILD_JOB
      )],
    ])('finds no losses for %s', (_label, content) => {
      expect(losses(content)).toEqual([]);
    });

    test('still reports a change the normalisation does not cover', () => {
      const content = doc(
        'name: Manual',
        'on:',
        '  workflow_dispatch:',
        '    inputs:',
        '      note:',
        '        required: true',
        ...BUILD_JOB
      );
      const gui = yamlToGuiResult(content).gui;
      gui.events[0].inputs!.note.required = false;

      expect(findGuiRoundTripLosses(content, gui)).toEqual(['on.workflow_dispatch.inputs.note.required']);
    });

    test('fails closed when the check itself throws', () => {
      const content = doc('name: CI', 'on: push', ...BUILD_JOB);
      const broken = { ...yamlToGuiResult(content).gui, jobs: null } as unknown as WorkflowGUI;

      expect(findGuiRoundTripLosses(content, broken)).toEqual(['entire workflow']);
    });

  });

  describe('default templates', () => {
    test('DEFAULT_WORKFLOW_GUI should have valid structure', () => {
      expect(DEFAULT_WORKFLOW_GUI.name).toBeDefined();
      expect(DEFAULT_WORKFLOW_GUI.events).toHaveLength(1);
      expect(DEFAULT_WORKFLOW_GUI.jobs).toHaveLength(1);
      expect(DEFAULT_WORKFLOW_GUI.jobs[0].steps.length).toBeGreaterThan(0);
    });

    test('DEFAULT_REUSABLE_WORKFLOW_GUI should have workflow_call event', () => {
      expect(DEFAULT_REUSABLE_WORKFLOW_GUI.name).toBeDefined();
      expect(DEFAULT_REUSABLE_WORKFLOW_GUI.events).toHaveLength(1);
      expect(DEFAULT_REUSABLE_WORKFLOW_GUI.events[0].type).toBe('workflow_call');
      expect(DEFAULT_REUSABLE_WORKFLOW_GUI.events[0].inputs).toBeDefined();
    });
  });
});
