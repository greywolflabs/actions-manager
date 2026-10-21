import React from "react";
import { MoreHorizontal } from "lucide-react";
import type { Project } from "../api/projects";
import { PROJECT_STATE, normalizeProjectName } from "../utils/projectCardState";
import { Button } from "./ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "./ui/dropdown-menu";

interface ProjectCardMenuProps {
  readonly project: Project;
  /** Opens the project; every item but Open PR does. */
  readonly onOpen: () => void;
}

/** A project card's more-actions menu, shared by the Projects page and the Managed Files section. */
const ProjectCardMenu: React.FC<ProjectCardMenuProps> = ({ project, onOpen }) => {
  const projectName = normalizeProjectName(project);
  const projectState = project.pr_state ?? PROJECT_STATE.NEW;
  const showContinueEditing = projectState === PROJECT_STATE.DRAFT;
  const showConfigure = projectState === PROJECT_STATE.NEW;
  const showView = projectState === PROJECT_STATE.SYNCED || projectState === PROJECT_STATE.OPEN
    || projectState === PROJECT_STATE.DELETING;
  const showOpenPrPlaceholder = projectState === PROJECT_STATE.OPEN;
  const prUrl = [project.pr_url, project.prUrl].find((url): url is string => typeof url === "string") ?? null;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          size="icon"
          variant="ghost"
          onClick={(e) => e.stopPropagation()}
          aria-label="More actions"
          data-testid={`project-more-${projectName || project.project_code}`}
          className="h-8 w-8 border border-transparent text-slate-500 hover:border-slate-300 hover:bg-slate-100 hover:text-slate-700 dark:text-slate-300 dark:hover:border-slate-600 dark:hover:bg-slate-800 dark:hover:text-slate-100"
        >
          <MoreHorizontal className="h-4 w-4" aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" onClick={(e) => e.stopPropagation()}>
        {showContinueEditing && (
          <DropdownMenuItem
            onSelect={onOpen}
            data-testid="project-action-continue-editing"
          >
            Continue Editing
          </DropdownMenuItem>
        )}
        {showConfigure && (
          <DropdownMenuItem
            onSelect={onOpen}
            data-testid="project-action-configure"
          >
            Configure
          </DropdownMenuItem>
        )}
        {showView && (
          <DropdownMenuItem
            onSelect={onOpen}
            data-testid="project-action-view"
          >
            View
          </DropdownMenuItem>
        )}
        {showOpenPrPlaceholder && prUrl && (
          <DropdownMenuItem
            onSelect={() => globalThis.open(prUrl, "_blank", "noopener,noreferrer")}
            data-testid="project-action-open-pr"
          >
            Open PR
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
};

export default ProjectCardMenu;
