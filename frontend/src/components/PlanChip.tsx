import React from "react";
import { Crown } from "lucide-react";
import type { Plan } from "../api/user";
import { isPaidTier } from "../utils/accountTier";

interface PlanChipProps {
  readonly plan?: Plan | null;
}

/**
 * The account's plan beside the avatar, on every page: the crown (paid tiers)
 * and the plan's name, with the Upgrade link when the plan has one. Usage
 * stays on the pages it is about. The avatar menu has the plan's summary.
 */
const PlanChip: React.FC<PlanChipProps> = ({ plan }) => {
  if (!plan) return null;

  const name = plan.label.replace(/ Plan$/, "");
  const paid = isPaidTier(plan.tier);

  return (
    <div
      className="flex min-w-0 items-center gap-2 rounded-full border border-border bg-hover-bg px-2.5 py-1 text-xs dark:border-border-dark dark:bg-hover-dark-bg"
      data-testid="plan-chip"
      title={plan.summary}
    >
      <span className="flex shrink-0 items-center gap-1.5 font-semibold text-text-primary dark:text-text-primary-dark">
        {paid ? (
          <Crown className="h-3.5 w-3.5 text-amber-600 dark:text-amber-300" aria-hidden="true" />
        ) : (
          <span className="h-2 w-2 rounded-full bg-slate-400 dark:bg-slate-500" aria-hidden="true" />
        )}
        <span className="sr-only sm:not-sr-only">{name}</span>
        <span className="sr-only"> plan</span>
      </span>
      {plan.upgrade && (
        <a
          href={plan.upgrade.url}
          target="_blank"
          rel="noreferrer"
          className="hidden shrink-0 border-l border-border pl-2 font-semibold text-primary hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring dark:border-border-dark dark:text-primary-dark sm:inline"
          title={plan.upgrade.message}
        >
          {plan.upgrade.label}
        </a>
      )}
    </div>
  );
};

export default PlanChip;
