import React from "react";
import { Button } from "./ui/button";
// eslint-disable-next-line no-restricted-imports -- Legacy: TODO migrate CSS file to Tailwind CSS classes
import "../styles/projectMgmt.css";

interface DangerZoneProps {
  projectName: string;
  onDeleteProject: () => void;
  /** Why deleting is not available right now, e.g. the project is already being deleted. */
  disabledReason?: string | null;
}

const DangerZone: React.FC<DangerZoneProps> = ({ projectName, onDeleteProject, disabledReason = null }) => {
  return (
    <div className="danger-zone-page">
      {/* Warning intro */}
      <div className="danger-zone-intro">
        <h2 className="danger-zone-title">⚠️ Danger Zone</h2>
        <p className="danger-zone-description">
          Destructive actions for this project live here. These actions may be permanent and should be used with caution.
        </p>
      </div>

      {/* Delete Project section */}
      <div className="danger-zone-card">
        <div className="danger-zone-card-header">
          <h3 className="danger-zone-card-title">🗑️ Delete Project</h3>
          <p className="danger-zone-card-description">
            Permanently remove <strong>{projectName}</strong> from ActionsManager. You choose whether its files stay in GitHub or are removed by a pull request per repository and branch, and whether its secrets and variables are deleted too.
          </p>
          {disabledReason && (
            <p className="danger-zone-card-description" data-testid="danger-zone-disabled-reason">
              {disabledReason}
            </p>
          )}
        </div>
        <div className="danger-zone-card-action">
          <Button
            type="button"
            variant="destructive"
            onClick={onDeleteProject}
            disabled={disabledReason !== null}
            title={disabledReason ?? undefined}
            className="danger-zone-delete-btn"
          >
            ❌ Delete Project
          </Button>
        </div>
      </div>
    </div>
  );
};

export default DangerZone;
