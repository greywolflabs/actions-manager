import React, { useMemo } from "react";
import type { Plan } from "../api/user";
import UsageMeter from "./UsageMeter";

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

/**
 * The plan's project usage as the shared meter. The plan's name and Upgrade
 * link live in the header's PlanChip, so every page shows them.
 */
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

    // Per-type limits: show each type's usage, the bar following the fuller one.
    if (callerLimit !== null && rwxLimit !== null) {
      const caller = usedCount(callerProjectsUsed);
      const rwx = usedCount(rwxProjectsUsed);
      const fullest = Math.max(Math.min((caller / callerLimit) * 100, 100), Math.min((rwx / rwxLimit) * 100, 100));
      return {
        label: `Caller: ${caller}/${callerLimit} · Reusable: ${rwx}/${rwxLimit}`,
        value: Math.round(fullest),
        max: 100,
      };
    }

    const used = usedCount(projectsUsed);
    if (!projects) return { label: `${formatProjectCount(used)} · Unlimited`, value: used, max: null };
    return { label: `${used} of ${projects} projects`, value: used, max: projects };
  }, [plan, projectsUsed, callerProjectsUsed, rwxProjectsUsed]);

  if (!plan || !usage) return null;

  return (
    <UsageMeter
      label={usage.label}
      value={usage.value}
      max={usage.max}
      ariaLabel={`${plan.label} project usage`}
      testId="plan-usage-pill"
      className={className}
    />
  );
};

export default PlanUsagePill;
