/**
 * Validation for the Create Project wizard.
 *
 * These were closures inside NewProject. Nested functions count toward their
 * parent's cognitive complexity in Sonar, and the component was well over the
 * limit — so the branching lives here, where it is also directly testable.
 * Each returns the message to show, or null when the input is acceptable.
 */

export interface Repository {
  name: string;
  full_name: string;
  private: boolean;
}

export type VisibilityScope = 'public' | 'private';

export function repoMatchesVisibilityScope(repo: Repository, scope: VisibilityScope): boolean {
  return scope === 'private' ? !!repo.private : !repo.private;
}

export interface ProjectInputs {
  projectName: string;
  selectedRepos: string[];
  repos: Repository[];
  visibilityScope: VisibilityScope;
  privateAllowedByTier: boolean;
  /** The plan's reason for refusing private repositories. */
  privateRefusedMessage?: string;
  useCustomKey: boolean;
  projectKey: string;
}

/** Repositories in the selection that do not match the chosen visibility. */
export function mismatchedRepos(inputs: ProjectInputs): string[] {
  return inputs.selectedRepos.filter((name) => {
    const repo = inputs.repos.find((r) => (r.full_name || r.name) === name);
    // Unknown repo — let the backend decide rather than blocking here.
    if (!repo) return false;
    return !repoMatchesVisibilityScope(repo, inputs.visibilityScope);
  });
}

export function validateProjectInputs(inputs: ProjectInputs): string | null {
  if (!inputs.projectName.trim()) return 'Project name cannot be empty.';
  if (inputs.selectedRepos.length === 0) return 'Please select at least one repository.';

  // Mirrors the backend's tier/scope enforcement.
  const mismatched = mismatchedRepos(inputs);
  if (mismatched.length > 0) {
    const label = inputs.visibilityScope === 'private' ? 'Private' : 'Public';
    return (
      `The following repositories do not match the selected ${label} visibility: ` +
      `${mismatched.join(', ')}. Remove them or change the visibility option.`
    );
  }

  if (inputs.visibilityScope === 'private' && !inputs.privateAllowedByTier) {
    return inputs.privateRefusedMessage ?? 'Private repository projects are not available on your plan.';
  }

  if (inputs.useCustomKey && inputs.projectKey.trim()) {
    const cleanKey = inputs.projectKey.trim().toUpperCase().replaceAll(/[^A-Z0-9]/g, '');
    if (cleanKey.length < 2 || cleanKey.length > 10) {
      return 'Project key must be 2–10 characters (letters and numbers only).';
    }
  }

  return null;
}

/**
 * The plan's message when a per-type project limit is reached, or null while
 * that type is still available (or not limited).
 */
export function limitReachedMessage(
  limit: number | null | undefined,
  count: number,
  message: string | undefined,
): string | null {
  if (limit === null || limit === undefined || count < limit) return null;
  return message ?? '';
}
