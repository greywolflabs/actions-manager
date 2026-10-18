import { planWorkflowKindMoves } from './workflowKindChanges';

const wf = (name: string, extra: Record<string, unknown> = {}) => ({
  name,
  content: `name: ${name}`,
  isModified: true,
  workflowStatus: 'new',
  ...extra,
});

describe('planWorkflowKindMoves (#2318)', () => {
  const regular = [wf('build'), wf('deploy'), wf('lint')];
  const reusable = [wf('shared', { isReusable: true })];

  test('moves a workflow its trigger made reusable, marked saved with the server status', () => {
    const plan = planWorkflowKindMoves(
      regular, reusable,
      [{ name: 'deploy', is_reusable: true, workflow_status: 'committed_locally' }],
      null,
    )!;

    expect(plan.nextRegular(regular).map(w => w.name)).toEqual(['build', 'lint']);
    const moved = plan.nextReusable(reusable);
    expect(moved.map(w => w.name)).toEqual(['shared', 'deploy']);
    expect(moved[1]).toMatchObject({
      isReusable: true, isModified: false, savedName: 'deploy', workflowStatus: 'committed_locally',
    });
  });

  test('the editor stays on the moved workflow', () => {
    const plan = planWorkflowKindMoves(regular, reusable, [{ name: 'deploy', is_reusable: true }], 'regular-1')!;
    expect(plan.selectedId).toBe('reusable-1');
  });

  test('and on a workflow whose position the move shifted', () => {
    const plan = planWorkflowKindMoves(regular, reusable, [{ name: 'deploy', is_reusable: true }], 'regular-2')!;
    expect(plan.selectedId).toBe('regular-1');
  });

  test('a linked workflow selection is left alone', () => {
    const plan = planWorkflowKindMoves(regular, reusable, [{ name: 'deploy', is_reusable: true }], 'linked-7')!;
    expect(plan.selectedId).toBe('linked-7');
  });

  test('names match as case-insensitive stems, as the server stores them', () => {
    const plan = planWorkflowKindMoves(
      regular, reusable, [{ name: 'SHARED.yml', is_reusable: false }], 'reusable-0',
    )!;
    expect(plan.nextRegular(regular).map(w => w.name)).toEqual(['build', 'deploy', 'lint', 'shared']);
    expect(plan.nextReusable(reusable)).toEqual([]);
    expect(plan.selectedId).toBe('regular-3');
  });

  test('nothing to move when the change names a workflow already in its section', () => {
    expect(planWorkflowKindMoves(regular, reusable, [{ name: 'shared', is_reusable: true }], null)).toBeNull();
    expect(planWorkflowKindMoves(regular, reusable, [], null)).toBeNull();
  });

  test('the updaters apply on top of a newer list, not the one they were planned from', () => {
    const plan = planWorkflowKindMoves(regular, reusable, [{ name: 'deploy', is_reusable: true }], null)!;
    const savedMeanwhile = regular.map(w => ({ ...w, isModified: false }));
    expect(plan.nextRegular(savedMeanwhile).every(w => w.isModified === false)).toBe(true);
  });
});
