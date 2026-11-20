import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';

const mockApiFetch = vi.fn();
vi.mock('../api', () => ({ apiFetch: (...a: any[]) => mockApiFetch(...a) }));
import SystemStatus, { formatAge } from '../SystemStatus';

const ok = {
  overall: 'ok', problems: [], checkedAt: '2026-11-20T06:00:00Z', deadLetterMessages: 0, alarmsFiring: [],
  sync: { state: 'ok', message: 'Sync is running.', ageSeconds: 600, result: 'success', ticketsSynced: 411 },
  reconciliation: { state: 'ok', message: 'Reconciliation is running daily.', ageSeconds: 3600, result: 'completed' },
  queues: { ingestionDlq: { name: 'compass-ingestion-dlq', dlq: true, visible: 0, inFlight: 0 } },
};

describe('SystemStatus', () => {
  it('shows each job and the overall verdict', async () => {
    mockApiFetch.mockResolvedValueOnce(ok);
    render(<SystemStatus />);
    expect(await screen.findByText('All systems normal')).toBeTruthy();
    expect(screen.getByText('Sync is running.')).toBeTruthy();
    expect(screen.getByText(/411 tickets/)).toBeTruthy();
  });

  it('surfaces the first problem when something is wrong', async () => {
    mockApiFetch.mockResolvedValueOnce({ ...ok, overall: 'error', problems: ['Last Sync run failed.'],
      sync: { ...ok.sync, state: 'error', message: 'Last Sync run failed.' } });
    render(<SystemStatus />);
    expect((await screen.findAllByText('Last Sync run failed.')).length).toBeGreaterThan(0);
  });

  it('degrades to "unavailable" on an unexpected response instead of crashing', async () => {
    mockApiFetch.mockResolvedValueOnce({});
    render(<SystemStatus />);
    expect(await screen.findByText(/Status unavailable/)).toBeTruthy();
  });

  it('formats ages', () => {
    expect(formatAge(null)).toBe('never');
    expect(formatAge(30)).toBe('just now');
    expect(formatAge(600)).toBe('10 min ago');
    expect(formatAge(7200)).toBe('2 h ago');
    expect(formatAge(3 * 86400)).toBe('3 days ago');
  });
});
