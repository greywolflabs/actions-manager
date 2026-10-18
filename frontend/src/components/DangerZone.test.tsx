import React from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom';
import DangerZone from './DangerZone';

describe('DangerZone', () => {
  const user = userEvent.setup();

  const defaultProps = {
    projectName: 'My Test Project',
    onDeleteProject: vi.fn(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  test('renders the Danger Zone title', () => {
    render(<DangerZone {...defaultProps} />);
    expect(screen.getByText(/Danger Zone/i)).toBeInTheDocument();
  });

  test('renders the warning description', () => {
    render(<DangerZone {...defaultProps} />);
    expect(
      screen.getByText(/Destructive actions for this project live here/i)
    ).toBeInTheDocument();
  });

  test('renders the project name in the delete card', () => {
    render(<DangerZone {...defaultProps} />);
    expect(screen.getByText('My Test Project')).toBeInTheDocument();
  });

  test('renders the Delete Project button', () => {
    render(<DangerZone {...defaultProps} />);
    expect(screen.getByRole('button', { name: /Delete Project/i })).toBeInTheDocument();
  });

  test('clicking Delete Project button calls onDeleteProject', async () => {
    const onDeleteProject = vi.fn();
    render(<DangerZone {...defaultProps} onDeleteProject={onDeleteProject} />);
    await user.click(screen.getByRole('button', { name: /Delete Project/i }));
    expect(onDeleteProject).toHaveBeenCalledTimes(1);
  });

  test('a project already being deleted cannot be deleted again, and says why', async () => {
    const onDeleteProject = vi.fn();
    render(
      <DangerZone
        {...defaultProps}
        onDeleteProject={onDeleteProject}
        disabledReason="This project is already being deleted."
      />
    );

    const button = screen.getByRole('button', { name: /Delete Project/i });
    expect(button).toBeDisabled();
    expect(screen.getByTestId('danger-zone-disabled-reason')).toHaveTextContent('already being deleted');
    await user.click(button);
    expect(onDeleteProject).not.toHaveBeenCalled();
  });
});
