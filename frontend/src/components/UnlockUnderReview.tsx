import React from "react";

interface UnlockOverlayProps {
  title: string;
  onRequestUnlock: () => void;
  overlayTestId?: string;
  buttonTestId?: string;
}

/** The lock over a file whose pull request is still open, with its way in. */
export const UnlockOverlay: React.FC<UnlockOverlayProps> = ({ title, onRequestUnlock, overlayTestId, buttonTestId }) => (
  <div className="workflow-lock-overlay" data-testid={overlayTestId}>
    <button type="button" className="workflow-lock-btn" onClick={onRequestUnlock} title={title} data-testid={buttonTestId}>
      <span className="workflow-lock-icon">🔒</span>
      <span className="workflow-lock-label">Unlock to Edit</span>
    </button>
  </div>
);

interface UnlockUnderReviewDialogProps {
  /** What is being unlocked, as the body names it: "workflow", "file". */
  subject: string;
  onCancel: () => void;
  onUnlock: () => void;
}

/**
 * Confirms editing a file whose pull request is still open. Shared by every
 * file editor: once unlocked, Commit and Update PR adds the edit to that PR.
 */
export const UnlockUnderReviewDialog: React.FC<UnlockUnderReviewDialogProps> = ({ subject, onCancel, onUnlock }) => (
  <div className="workflow-lock-modal-backdrop">
    <button
      type="button"
      aria-label="Dismiss unlock dialog"
      onClick={onCancel}
      className="absolute inset-0 cursor-default border-0 bg-transparent p-0"
    />
    <div className="workflow-lock-modal relative">
      <div className="workflow-lock-modal-header">
        <span className="workflow-lock-modal-icon">🔒</span>
        <h3 className="workflow-lock-modal-title">Open Pull Request Detected</h3>
      </div>
      <p className="workflow-lock-modal-body">
        This {subject} already has an open pull request. If you continue editing, your changes will be
        added to the existing pull request instead of creating a separate one.
      </p>
      <div className="workflow-lock-modal-actions">
        <button type="button" className="btn btn-secondary" onClick={onCancel}>
          Cancel
        </button>
        <button type="button" className="btn btn-primary" onClick={onUnlock}>
          Unlock and Edit
        </button>
      </div>
    </div>
  </div>
);
