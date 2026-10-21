import React from "react";
import { cn } from "../lib/utils";

interface UsageMeterProps {
  /** What is used, in words: "3 of 10 projects". */
  readonly label: string;
  readonly value: number;
  /** The most there is; null for no limit, which shows no bar. */
  readonly max: number | null;
  readonly ariaLabel: string;
  readonly testId?: string;
  readonly className?: string;
}

/**
 * One slim usage meter: the words, and a bar when there is a limit. The
 * Projects and Managed Files pages both show their plan usage with it.
 */
const UsageMeter: React.FC<UsageMeterProps> = ({ label, value, max, ariaLabel, testId, className }) => (
  <div className={cn("flex flex-col items-start gap-1 sm:items-end", className)} data-testid={testId}>
    <span className="text-sm text-slate-600 dark:text-slate-300" title={label}>{label}</span>
    {max !== null && (
      <progress
        value={Math.min(value, max)}
        max={max}
        aria-label={ariaLabel}
        className="h-1.5 w-32 appearance-none overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700 [&::-moz-progress-bar]:bg-slate-500 [&::-webkit-progress-bar]:bg-slate-200 [&::-webkit-progress-value]:bg-slate-500 dark:[&::-moz-progress-bar]:bg-slate-300 dark:[&::-webkit-progress-bar]:bg-slate-700 dark:[&::-webkit-progress-value]:bg-slate-300"
      />
    )}
  </div>
);

export default UsageMeter;
