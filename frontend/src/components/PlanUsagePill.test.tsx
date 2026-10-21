import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import PlanUsagePill from './PlanUsagePill';
import { BETA_PLAN, ENTERPRISE_PLAN, FREE_PLAN, PROFESSIONAL_PLAN } from '../test/plans';

describe('PlanUsagePill', () => {
  test('renders free tier usage with numeric limit', () => {
    render(<PlanUsagePill plan={FREE_PLAN} projectsUsed={2} />);

    expect(screen.getByTestId('plan-usage-pill')).toHaveTextContent('2 of 3 projects');

    const progress = screen.getByRole('progressbar', { name: /Free Plan project usage/i });
    expect(progress).toHaveAttribute('max', '3');
    expect(progress).toHaveAttribute('value', '2');
  });

  test('renders professional tier usage with numeric limit', () => {
    render(<PlanUsagePill plan={PROFESSIONAL_PLAN} projectsUsed={8} />);

    expect(screen.getByText('8 of 10 projects')).toBeInTheDocument();

    const progress = screen.getByRole('progressbar', { name: /Professional Plan project usage/i });
    expect(progress).toHaveAttribute('max', '10');
    expect(progress).toHaveAttribute('value', '8');
  });

  test('leaves the plan name and Upgrade link to the header chip', () => {
    render(<PlanUsagePill plan={PROFESSIONAL_PLAN} projectsUsed={8} />);

    expect(screen.queryByText(/Professional Plan/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /Upgrade/i })).not.toBeInTheDocument();
  });

  test('renders enterprise tier as unlimited, without a bar or limit math', () => {
    render(<PlanUsagePill plan={ENTERPRISE_PLAN} projectsUsed={5} />);

    expect(screen.getByText('5 projects \u00b7 Unlimited')).toBeInTheDocument();
    expect(screen.queryByText(/Infinity/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
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

    expect(screen.getByTestId('plan-usage-pill')).toHaveTextContent('Caller: 2/4');
    expect(screen.getByTestId('plan-usage-pill')).toHaveTextContent('Reusable: 1/2');
  });

  test('the per-type bar follows the fuller type', () => {
    render(
      <PlanUsagePill
        plan={BETA_PLAN}
        projectsUsed={2}
        callerProjectsUsed={2}
        rwxProjectsUsed={0}
      />
    );

    const progress = screen.getByRole('progressbar', { name: /Self-Hosted Beta project usage/i });
    expect(progress).toHaveAttribute('max', '100');
    expect(progress).toHaveAttribute('value', '50');
  });

  test('renders nothing until the plan has loaded', () => {
    render(<PlanUsagePill plan={null} projectsUsed={3} />);

    expect(screen.queryByTestId('plan-usage-pill')).not.toBeInTheDocument();
  });
});
