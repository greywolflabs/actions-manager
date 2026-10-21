import React from "react";
import { Link, NavLink } from "react-router";
import { FileStack, FolderKanban, type LucideIcon, Package } from "lucide-react";
import type { UserDetails } from "../api/user";
import { cn } from "../lib/utils";
import BrandLogo from "./BrandLogo";
import PlanChip from "./PlanChip";
import UserAvatar from "./UserAvatar";

/** The page's container. Pages outside a project center their content in it. */
const APP_CONTAINER_CLASS = "w-full max-w-[1400px] mx-auto px-6 lg:px-8";

/**
 * The header spans the window on every page, logo and tabs at the left, the
 * account menu at the right, so it never moves between pages. It has the page
 * container's side padding, so the logo and a page heading still line up
 * until the window is wider than the container.
 */
const HEADER_ROW_CLASS = "w-full px-6 lg:px-8";

export type PrimarySection = "projects" | "managed-files" | "managed-actions";

interface PrimaryTab {
  readonly key: PrimarySection;
  readonly label: string;
  readonly icon: LucideIcon;
  readonly testId: string;
  readonly path: (user: string) => string;
  /** Projects lives at /project/:user, the parent of every other section, so it matches that URL only. */
  readonly end: boolean;
}

const PRIMARY_TABS: readonly PrimaryTab[] = [
  { key: "projects", label: "Projects", icon: FolderKanban, testId: "projects-nav-button", path: (user) => `/project/${user}`, end: true },
  { key: "managed-files", label: "Managed Files", icon: FileStack, testId: "managed-files-nav-button", path: (user) => `/project/${user}/managed-files`, end: false },
  { key: "managed-actions", label: "Managed Actions", icon: Package, testId: "actions-projects-nav-button", path: (user) => `/project/${user}/actions-projects`, end: false },
];

const FOCUS_RING = "focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring";

const tabClassName = ({ isActive }: { isActive: boolean }): string => cn(
  "inline-flex min-h-11 shrink-0 items-center gap-2 whitespace-nowrap border-b-2 px-3 text-sm transition-colors focus-visible:ring-inset",
  FOCUS_RING,
  isActive
    ? "border-primary font-semibold text-text-primary dark:border-primary-dark dark:text-text-primary-dark"
    : "border-transparent font-medium text-text-secondary hover:bg-hover-bg hover:text-text-primary dark:text-secondary-dark dark:hover:bg-hover-dark-bg dark:hover:text-text-primary-dark",
);

interface AppHeaderProps {
  /** Whose projects the tabs open: the route's user on project pages, the signed-in user elsewhere. */
  readonly user: string;
  /** The avatar menu waits for this, as it always has. */
  readonly userDetails?: UserDetails;
  readonly onLogout?: () => void;
  /**
   * The section a page belongs to when its URL matches no tab, as a project or
   * the New Project wizard does. Marked aria-current="true": the section, not the page.
   */
  readonly currentSection?: PrimarySection;
}

// The full wordmark and the avatar don't fit one row on a phone, so there the
// logo is the shield alone and the tabs get a scrolling row of their own.
export const AppHeader: React.FC<AppHeaderProps> = ({ user, userDetails, onLogout, currentSection }) => (
  <header
    className="sticky top-0 z-40 w-full shrink-0 border-b border-border bg-container dark:border-border-dark dark:bg-container-dark"
    data-testid="app-header"
  >
    <div className={cn(HEADER_ROW_CLASS, "flex flex-wrap items-center gap-x-6 sm:h-16 sm:flex-nowrap")}>
      <Link
        to={`/project/${user}`}
        aria-label="ActionsManager home"
        data-testid="app-header-home-link"
        className={cn("flex shrink-0 items-center rounded-md py-2", FOCUS_RING)}
      >
        <span className="flex sm:hidden">
          <BrandLogo variant="icon" size="sm" />
        </span>
        <span className="hidden sm:inline-flex">
          <BrandLogo variant="full" size="md" />
        </span>
      </Link>

      <nav
        aria-label="Primary"
        className="order-last -mb-px flex w-full overflow-x-auto sm:order-none sm:w-auto sm:min-w-0 sm:flex-1 sm:self-stretch"
      >
        {PRIMARY_TABS.map(({ key, label, icon: Icon, testId, path, end }) => {
          const content = (
            <>
              <Icon className="h-4 w-4" aria-hidden="true" />
              {label}
            </>
          );
          // NavLink sets aria-current only on a URL match, so a section the URL doesn't name needs a plain Link.
          return key === currentSection ? (
            <Link key={key} to={path(user)} aria-current="true" data-testid={testId} className={tabClassName({ isActive: true })}>
              {content}
            </Link>
          ) : (
            <NavLink key={key} to={path(user)} end={end} data-testid={testId} className={tabClassName}>
              {content}
            </NavLink>
          );
        })}
      </nav>

      {userDetails && (
        <div className="ml-auto flex shrink-0 items-center gap-3 py-2 sm:py-0">
          <PlanChip plan={userDetails.plan} />
          <UserAvatar
            avatarUrl={userDetails.avatar_url}
            username={userDetails.github_user}
            accountType={userDetails.account_type}
            plan={userDetails.plan}
            githubAccountType={userDetails.github_account_type}
            connectedGithubAccount={userDetails.connected_github_account}
            connectedGithubAccountType={userDetails.connected_github_account_type}
            workspaceRole={userDetails.workspace_role}
            rateLimit={userDetails.rate_limit}
            githubToken={userDetails.github_token}
            onLogout={onLogout}
          />
        </div>
      )}
    </div>
  </header>
);

interface AppPageProps {
  readonly children: React.ReactNode;
}

export const AppPage: React.FC<AppPageProps> = ({ children }) => (
  <main className={cn(APP_CONTAINER_CLASS, "pt-8 pb-12")}>{children}</main>
);

export const AppShell: React.FC<AppHeaderProps & AppPageProps> = ({ children, ...headerProps }) => (
  <div className="w-full min-h-screen bg-background dark:bg-background-dark">
    <AppHeader {...headerProps} />
    <AppPage>{children}</AppPage>
  </div>
);
