// Styling for the plan tiers the backend reports (plan.tier). Limits and plan
// wording come from the backend; see api/user.ts Plan.
const PAID_TIERS = new Set(["professional", "enterprise"]);

export const isPaidTier = (tier: string | null | undefined): boolean => PAID_TIERS.has(tier ?? "");
