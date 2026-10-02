/* eslint-disable no-restricted-syntax -- Need inline style to set progress bar width from runtime usage percent */
import React, { useMemo } from "react";
import { Crown } from "lucide-react";
import type { Plan } from "../api/user";
import { isPaidTier } from "../utils/accountTier";

const formatProjectCount = (count: number): string => (
  `${count} project${count === 1 ? "" : "s"}`
);

const usedCount = (value: number | undefined): number => (
  Number.isFinite(value) ? Math.max(0, value!) : 0
);

export interface PlanUsagePillProps {
  plan?: Plan | null;
  projectsUsed: number;
  /** Caller Workflow Projects used; shown when the plan limits each project type. */
  callerProjectsUsed?: number;
  /** Reusable Workflow Projects used; shown when the plan limits each project type. */
  rwxProjectsUsed?: number;
  className?: string;
}

const PlanUsagePill: React.FC<PlanUsagePillProps> = ({
  plan,
  projectsUsed,
  callerProjectsUsed,
  rwxProjectsUsed,
  className,
}) => {
  const usage = useMemo(() => {
    if (!plan) return null;
    const { projects, caller_projects: callerLimit, reusable_projects: rwxLimit } = plan.limits;

    // Per-type limits: show each type's usage.
    if (callerLimit !== null && rwxLimit !== null) {
      const caller = usedCount(callerProjectsUsed);
      const rwx = usedCount(rwxProjectsUsed);
      const callerPct = Math.min((caller / callerLimit) * 100, 100);
      const rwxPct = Math.min((rwx / rwxLimit) * 100, 100);
      // Use the higher utilisation for the progress bar
      const progressPercent = Math.max(callerPct, rwxPct);
      return {
        label: `Caller: ${caller}/${callerLimit} · Reusable: ${rwx}/${rwxLimit}`,
        progressPercent,
        aria: {
          min: 0,
          max: 100,
          now: Math.round(progressPercent),
          label: `${plan.label} project usage`,
        },
      };
    }

    const used = usedCount(projectsUsed);

    if (!projects) {
      return {
        label: `${formatProjectCount(used)} · Unlimited`,
        progressPercent: 100,
        aria: {
          min: 0,
          max: 100,
          now: 100,
          label: `${plan.label} project usage (unlimited)`,
        },
      };
    }

    const progressPercent = Math.min((used / projects) * 100, 100);
    return {
      label: `${used} / ${projects} projects`,
      progressPercent,
      aria: {
        min: 0,
        max: projects,
        now: Math.min(used, projects),
        label: `${plan.label} project usage`,
      },
    };
  }, [plan, projectsUsed, callerProjectsUsed, rwxProjectsUsed]);

  if (!plan || !usage) return null;

  const showCrown = isPaidTier(plan.tier);
  const barClassName = showCrown
    ? "bg-amber-400 dark:bg-amber-300"
    : "bg-slate-500/70 dark:bg-slate-300/70";

  return (
    <div
      className={[
        "flex h-14 items-center gap-3 rounded-md border border-border bg-hover-bg px-3 py-2 text-left shadow-sm dark:border-border-dark dark:bg-hover-dark-bg",
        "w-[168px] sm:w-[260px] md:w-[320px] lg:w-[400px] max-w-[420px] min-w-0",
        className ?? "",
      ].join(" ").trim()}
      aria-label="Plan usage"
      data-testid="plan-usage-pill"
    >
      <div className="flex h-9 w-9 items-center justify-center rounded-full bg-slate-100 dark:bg-slate-800/60">
        {showCrown ? (
          <Crown className="h-4 w-4 text-amber-600 dark:text-amber-300" aria-hidden="true" />
        ) : (
          <span className="text-xs font-semibold text-slate-500 dark:text-slate-300" aria-hidden="true">P</span>
        )}
      </div>

      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center justify-between gap-2">
          <div className="min-w-0">
            <div
              className="truncate text-sm font-semibold text-text-primary dark:text-text-primary-dark"
              title={plan.label}
            >
              <span className="hidden md:inline">{plan.label}</span>
              <span className="md:hidden">{plan.label.replace(" Plan", "").replace(" Beta", " β")}</span>
            </div>
            <div
              className="truncate text-xs text-text-secondary dark:text-text-secondary-dark"
              title={usage.label}
            >
              {usage.label}
            </div>
          </div>

          {plan.upgrade && (
            <a
              href={plan.upgrade.url}
              target="_blank"
              rel="noreferrer"
              className="hidden sm:inline-flex shrink-0 items-center rounded-md border border-slate-300 bg-white/70 px-2 py-1 text-[11px] font-semibold text-slate-700 hover:bg-white dark:border-slate-600 dark:bg-slate-950/30 dark:text-slate-200 dark:hover:bg-slate-950/50"
              aria-label="Upgrade plan"
              title={plan.upgrade.message}
            >
              {plan.upgrade.label}
            </a>
          )}
        </div>

        <div
          className="mt-2 h-1 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700"
          role="progressbar"
          aria-label={usage.aria.label}
          aria-valuemin={usage.aria.min}
          aria-valuemax={usage.aria.max}
          aria-valuenow={usage.aria.now}
        >
          <div
            className={`h-full rounded-full transition-all ${barClassName}`}
            style={{ width: `${usage.progressPercent}%` }}
          />
        </div>
      </div>
    </div>
  );
};

export default PlanUsagePill;
