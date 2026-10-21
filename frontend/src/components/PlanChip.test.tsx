import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import PlanChip from './PlanChip';
import { BETA_PLAN, FREE_PLAN, PROFESSIONAL_PLAN, UPGRADE_URL } from '../test/plans';

describe('PlanChip', () => {
  test('shows the plan name without the word Plan', () => {
    render(<PlanChip plan={PROFESSIONAL_PLAN} />);

    expect(screen.getByTestId('plan-chip')).toHaveTextContent('Professional');
    expect(screen.getByTestId('plan-chip')).not.toHaveTextContent('Professional Plan');
  });

  test('links to Upgrade when the plan has one', () => {
    render(<PlanChip plan={FREE_PLAN} />);

    const upgrade = screen.getByRole('link', { name: 'Upgrade' });
    expect(upgrade).toHaveAttribute('href', UPGRADE_URL);
    expect(upgrade).toHaveAttribute('rel', 'noreferrer');
  });

  test('has no Upgrade link when the plan offers none', () => {
    render(<PlanChip plan={BETA_PLAN} />);

    expect(screen.getByTestId('plan-chip')).toHaveTextContent('Self-Hosted Beta');
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  test('renders nothing until the plan has loaded', () => {
    render(<PlanChip plan={null} />);

    expect(screen.queryByTestId('plan-chip')).not.toBeInTheDocument();
  });
});
