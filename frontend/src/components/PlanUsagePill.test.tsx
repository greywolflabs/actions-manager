import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import PlanUsagePill from './PlanUsagePill';
import { BETA_PLAN, ENTERPRISE_PLAN, FREE_PLAN, PROFESSIONAL_PLAN, UPGRADE_URL } from '../test/plans';

describe('PlanUsagePill', () => {
  test('renders free tier usage with numeric limit', () => {
    render(<PlanUsagePill plan={FREE_PLAN} projectsUsed={2} />);

    expect(screen.getByTestId('plan-usage-pill')).toBeInTheDocument();
    expect(screen.getByText(/Free Plan/i)).toBeInTheDocument();
    expect(screen.getByText('2 / 3 projects')).toBeInTheDocument();

    const progress = screen.getByRole('progressbar', { name: /Free Plan project usage/i });
    expect(progress).toHaveAttribute('aria-valuemax', '3');
    expect(progress).toHaveAttribute('aria-valuenow', '2');
  });

  test('renders professional tier usage with numeric limit and upgrade link', () => {
    render(<PlanUsagePill plan={PROFESSIONAL_PLAN} projectsUsed={8} />);

    expect(screen.getByText(/Professional Plan/i)).toBeInTheDocument();
    expect(screen.getByText('8 / 10 projects')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Upgrade/i })).toHaveAttribute('href', UPGRADE_URL);

    const progress = screen.getByRole('progressbar', { name: /Professional Plan project usage/i });
    expect(progress).toHaveAttribute('aria-valuemax', '10');
    expect(progress).toHaveAttribute('aria-valuenow', '8');
  });

  test('renders enterprise tier as unlimited without numeric limit math', () => {
    render(<PlanUsagePill plan={ENTERPRISE_PLAN} projectsUsed={5} />);

    expect(screen.getByText(/Enterprise Plan/i)).toBeInTheDocument();
    expect(screen.getByText('5 projects · Unlimited')).toBeInTheDocument();
    expect(screen.queryByText(/Infinity/i)).not.toBeInTheDocument();

    const progress = screen.getByRole('progressbar', { name: /Enterprise Plan project usage/i });
    expect(progress).toHaveAttribute('aria-valuemax', '100');
    expect(progress).toHaveAttribute('aria-valuenow', '100');
  });

  test('renders the per-type usage when the plan limits each project type', () => {
    render(
      <PlanUsagePill
        plan={BETA_PLAN}
        projectsUsed={3}
        callerProjectsUsed={2}
        rwxProjectsUsed={1}
      />
    );

    expect(screen.getByTestId('plan-usage-pill')).toBeInTheDocument();
    expect(screen.getByText(/Self-Hosted Beta/i)).toBeInTheDocument();
    expect(screen.getByText(/Caller: 2\/4/i)).toBeInTheDocument();
    expect(screen.getByText(/Reusable: 1\/2/i)).toBeInTheDocument();
  });

  test('does not show an Upgrade button when the plan offers none', () => {
    render(
      <PlanUsagePill
        plan={BETA_PLAN}
        projectsUsed={1}
        callerProjectsUsed={1}
        rwxProjectsUsed={0}
      />
    );

    expect(screen.queryByRole('link', { name: /Upgrade/i })).not.toBeInTheDocument();
  });

  test('renders nothing until the plan has loaded', () => {
    render(<PlanUsagePill plan={null} projectsUsed={3} />);

    expect(screen.queryByTestId('plan-usage-pill')).not.toBeInTheDocument();
  });

  test('self-hosted beta renders progress bar', () => {
    render(
      <PlanUsagePill
        plan={BETA_PLAN}
        projectsUsed={2}
        callerProjectsUsed={2}
        rwxProjectsUsed={0}
      />
    );

    const progress = screen.getByRole('progressbar', { name: /Self-Hosted Beta project usage/i });
    expect(progress).toBeInTheDocument();
  });
});
