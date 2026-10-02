import {
  validateProjectInputs,
  limitReachedMessage,
  mismatchedRepos,
  repoMatchesVisibilityScope,
  type ProjectInputs,
} from './newProjectValidation';
import { BETA_PLAN, FREE_PLAN } from '../test/plans';

const repo = (full_name: string, isPrivate = false) => ({
  name: full_name.split('/').pop() as string,
  full_name,
  private: isPrivate,
});

const inputs = (over: Partial<ProjectInputs> = {}): ProjectInputs => ({
  projectName: 'My Project',
  selectedRepos: ['acme-corp/api'],
  repos: [repo('acme-corp/api')],
  visibilityScope: 'public',
  privateAllowedByTier: true,
  useCustomKey: false,
  projectKey: '',
  ...over,
});

describe('repoMatchesVisibilityScope', () => {
  test('matches public repos to the public scope', () => {
    expect(repoMatchesVisibilityScope(repo('acme-corp/api'), 'public')).toBe(true);
    expect(repoMatchesVisibilityScope(repo('acme-corp/api'), 'private')).toBe(false);
  });

  test('matches private repos to the private scope', () => {
    expect(repoMatchesVisibilityScope(repo('acme-corp/api', true), 'private')).toBe(true);
    expect(repoMatchesVisibilityScope(repo('acme-corp/api', true), 'public')).toBe(false);
  });
});

describe('mismatchedRepos', () => {
  test('reports a selected repo whose visibility does not match', () => {
    const result = mismatchedRepos(
      inputs({
        selectedRepos: ['acme-corp/api', 'acme-corp/secret'],
        repos: [repo('acme-corp/api'), repo('acme-corp/secret', true)],
      }),
    );
    expect(result).toEqual(['acme-corp/secret']);
  });

  test('leaves unknown repos to the backend rather than blocking', () => {
    expect(mismatchedRepos(inputs({ selectedRepos: ['acme-corp/not-loaded'], repos: [] }))).toEqual([]);
  });
});

describe('validateProjectInputs', () => {
  test('accepts a well-formed project', () => {
    expect(validateProjectInputs(inputs())).toBeNull();
  });

  test('requires a name', () => {
    expect(validateProjectInputs(inputs({ projectName: '   ' }))).toMatch(/cannot be empty/i);
  });

  test('requires at least one repository', () => {
    expect(validateProjectInputs(inputs({ selectedRepos: [] }))).toMatch(/at least one repository/i);
  });

  test('names the repositories that do not match the visibility scope', () => {
    const error = validateProjectInputs(
      inputs({
        selectedRepos: ['acme-corp/secret'],
        repos: [repo('acme-corp/secret', true)],
      }),
    );
    expect(error).toMatch(/acme-corp\/secret/);
    expect(error).toMatch(/Public visibility/);
  });

  test('blocks private projects the plan does not allow, with the plan\'s reason', () => {
    const error = validateProjectInputs(
      inputs({
        visibilityScope: 'private',
        privateAllowedByTier: false,
        privateRefusedMessage: FREE_PLAN.limit_messages.private_repositories,
        repos: [repo('acme-corp/api', true)],
      }),
    );
    expect(error).toBe(FREE_PLAN.limit_messages.private_repositories);
  });

  test('blocks private projects before the plan has loaded', () => {
    const error = validateProjectInputs(
      inputs({
        visibilityScope: 'private',
        privateAllowedByTier: false,
        repos: [repo('acme-corp/api', true)],
      }),
    );
    expect(error).toBe('Private repository projects are not available on your plan.');
  });

  test.each([['A'], ['ABCDEFGHIJK'], ['!!']])('rejects the custom key %s', (key) => {
    expect(validateProjectInputs(inputs({ useCustomKey: true, projectKey: key }))).toMatch(
      /2–10 characters/,
    );
  });

  test('accepts a custom key once punctuation is stripped', () => {
    expect(validateProjectInputs(inputs({ useCustomKey: true, projectKey: 'ap-i' }))).toBeNull();
  });

  test('ignores the key field when custom keys are off', () => {
    expect(validateProjectInputs(inputs({ useCustomKey: false, projectKey: 'x' }))).toBeNull();
  });
});

describe('limitReachedMessage', () => {
  const { caller_projects: limit } = BETA_PLAN.limits;
  const message = BETA_PLAN.limit_messages.caller_projects;

  test('is null while the type is under its limit', () => {
    expect(limitReachedMessage(limit, limit! - 1, message)).toBeNull();
  });

  test("is the plan's message once the limit is reached", () => {
    expect(limitReachedMessage(limit, limit!, message)).toBe(message);
  });

  test('is null when the plan does not limit the type, or has not loaded', () => {
    expect(limitReachedMessage(null, 100, message)).toBeNull();
    expect(limitReachedMessage(undefined, 100, message)).toBeNull();
  });
});
