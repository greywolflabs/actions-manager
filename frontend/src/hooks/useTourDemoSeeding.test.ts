import { renderHook, waitFor } from '@testing-library/react';
import type { Mock } from 'vitest';
import { fetchProjects } from '../api/projects';
import { pickDemoProjectName, useTourDemoSeeding, DEMO_PROJECT_NAME } from './useTourDemoSeeding';

vi.mock('../api/projects', () => ({
  fetchProjects: vi.fn(),
}));

describe('pickDemoProjectName', () => {
  test('uses the plain name when nothing is taken', () => {
    expect(pickDemoProjectName([])).toBe(DEMO_PROJECT_NAME);
  });

  test('skips past names already in use, so the tour cannot dead-end', () => {
    // A duplicate name is rejected on create, which would strand the user on
    // the step that promised the form was filled in for them.
    expect(pickDemoProjectName(['Demo-Project'])).toBe('Demo-Project-2');
    expect(pickDemoProjectName(['Demo-Project', 'Demo-Project-2'])).toBe('Demo-Project-3');
  });

  test('matches case-insensitively, as the backend does', () => {
    expect(pickDemoProjectName(['demo-project'])).toBe('Demo-Project-2');
  });

  test('tolerates missing names in the project list', () => {
    expect(pickDemoProjectName([undefined, ''])).toBe(DEMO_PROJECT_NAME);
  });
});

describe('useTourDemoSeeding', () => {
  test("avoids every name the owner has, a Managed Files project's included", async () => {
    // A name is unique among all of an owner's projects, so a Managed Files
    // project called Demo-Project would make the create step fail.
    (fetchProjects as Mock).mockResolvedValue([{ project_name: 'Demo-Project', project_type: 'managed_files' }]);
    const setProjectName = vi.fn();

    renderHook(() => useTourDemoSeeding({
      user: 'alice',
      tourStep: 'create-project',
      setProjectName,
      setProjectColor: vi.fn(),
      setUsePrefix: vi.fn(),
    }));

    await waitFor(() => expect(setProjectName).toHaveBeenCalled());
    expect(fetchProjects).toHaveBeenCalledWith('alice', 'all');
    expect(setProjectName.mock.calls[0][0]('')).toBe('Demo-Project-2');
  });
});
