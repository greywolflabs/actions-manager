import React from 'react';
import { Label } from './ui/label';
import { optionCardClass } from './ProjectBasicsStep';
import type { VisibilityScope } from '../utils/newProjectValidation';

/** What a scope is for; the private one also says what the plan requires. */
function visibilityHelperText(isPrivate: boolean, privateRepositoriesHelp: string | undefined): string {
  if (!isPrivate) return 'Use this project for public GitHub repositories only.';
  return privateRepositoriesHelp ?? 'Use this project for private GitHub repositories only.';
}

export interface VisibilityScopeOptionsProps {
  readonly visibilityScope: VisibilityScope;
  readonly onChange: (scope: VisibilityScope) => void;
  readonly privateAllowedByTier: boolean;
  /** The plan's description of the private scope. */
  readonly privateRepositoriesHelp?: string;
  /** The plan's reason for refusing private repositories, when it does. */
  readonly privateRefusedMessage?: string;
}

/**
 * The public/private choice a project's repositories are picked from, shared
 * by the new-project wizard and the Managed Files create dialog.
 */
const VisibilityScopeOptions: React.FC<VisibilityScopeOptionsProps> = ({
  visibilityScope,
  onChange,
  privateAllowedByTier,
  privateRepositoriesHelp,
  privateRefusedMessage,
}) => (
  <div>
    <Label className="mb-3 block text-sm font-semibold text-gray-900 dark:text-white">Repository Visibility:</Label>
    <div className="grid gap-3 md:grid-cols-2">
      {(["public", "private"] as VisibilityScope[]).map((scope) => {
        const isSelected = visibilityScope === scope;
        const isPrivate = scope === "private";
        const isDisabled = isPrivate && !privateAllowedByTier;
        const labelText = isPrivate ? "Private repositories" : "Public repositories";
        const helperText = visibilityHelperText(isPrivate, privateRepositoriesHelp);
        return (
          <label
            key={scope}
            data-testid={`visibility-option-${scope}`}
            className={`flex gap-3 rounded-lg border p-4 transition focus-within:ring-2 focus-within:ring-blue-400 ${optionCardClass(
              isDisabled,
              isSelected,
              "cursor-not-allowed border-gray-200 dark:border-slate-700 bg-gray-100 dark:bg-slate-800/60 opacity-60",
              "cursor-pointer border-blue-500 bg-blue-500/10",
              "cursor-pointer border-gray-200 dark:border-slate-700 bg-gray-100 dark:bg-slate-800/60 hover:border-blue-400/60",
            )}`}
          >
            <input
              type="radio"
              name="repositoryVisibilityScope"
              value={scope}
              checked={isSelected}
              disabled={isDisabled}
              onChange={() => onChange(scope)}
              className="mt-1"
            />
            <span className="flex-1">
              <span className="flex items-center gap-2 font-semibold text-gray-900 dark:text-white">
                {labelText}
                {isSelected && <span className="ml-auto text-blue-600 dark:text-blue-400">✓</span>}
              </span>
              <span className="mt-1 block text-xs leading-5 text-gray-600 dark:text-slate-300">
                {helperText}
              </span>
              {isDisabled && privateRefusedMessage && (
                <span className="mt-2 block text-xs font-medium text-red-600 dark:text-red-400">
                  ⚠️ {privateRefusedMessage}
                </span>
              )}
            </span>
          </label>
        );
      })}
    </div>
  </div>
);

export default VisibilityScopeOptions;
