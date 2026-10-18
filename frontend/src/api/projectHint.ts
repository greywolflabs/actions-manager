/**
 * The project page API requests are made from (#2240).
 *
 * Project names are unique per owner, not across the workspace, so a name two
 * owners share can't say which project a request means. The page already knows
 * the id; requests carry it as X-Project-Id, and the backend uses it only to
 * choose among projects of that exact name the caller can already see.
 */
let currentProjectId: number | null = null;

export const setProjectHint = (projectId: number | null): void => {
  currentProjectId = projectId && projectId > 0 ? projectId : null;
};

export const projectHintHeaders = (): Record<string, string> =>
  currentProjectId ? { "X-Project-Id": String(currentProjectId) } : {};
