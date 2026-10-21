import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import UsageMeter from './UsageMeter';

describe('UsageMeter', () => {
  test('shows the words and a bar when there is a limit', () => {
    render(<UsageMeter label="3 of 10 projects" value={3} max={10} ariaLabel="Projects used" testId="meter" />);

    expect(screen.getByTestId('meter')).toHaveTextContent('3 of 10 projects');
    const bar = screen.getByRole('progressbar', { name: 'Projects used' });
    expect(bar).toHaveAttribute('value', '3');
    expect(bar).toHaveAttribute('max', '10');
  });

  test('shows no bar when there is no limit', () => {
    render(<UsageMeter label="5 projects" value={5} max={null} ariaLabel="Projects used" />);

    expect(screen.getByText('5 projects')).toBeInTheDocument();
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  });

  test('a bar never runs past its limit', () => {
    render(<UsageMeter label="4 of 3" value={4} max={3} ariaLabel="Projects used" />);

    expect(screen.getByRole('progressbar')).toHaveAttribute('value', '3');
  });
});
