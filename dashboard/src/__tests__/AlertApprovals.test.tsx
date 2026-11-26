import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import React from 'react';

const mockApiFetch = vi.fn();
vi.mock('../api', () => ({ apiFetch: (...a: any[]) => mockApiFetch(...a) }));
import AlertApprovals, { approvalLabel, AlertType } from '../AlertApprovals';

const base: AlertType = {
  eventTypeCode: 'AWS_LAMBDA_PLANNED_LIFECYCLE_EVENT', eventTypeCategory: 'scheduledChange', service: 'LAMBDA',
  campaigns: 2, heldBack: 1, accounts: 75, resources: 300, ticketableAccounts: 75, lastSeen: '2026-11-25T00:00:00Z',
  approved: false, approval: null, approvedByOtherRule: false, revoked: null,
};
const approved: AlertType = { ...base, approved: true, approval: { ruleId: 'x', approvedBy: 'a@b', approvedAt: '' } };
const list = (types: AlertType[], whitelistEnabled = true) =>
  ({ whitelistEnabled, mode: whitelistEnabled ? 'custom' : 'all', rulesUsed: 0, ruleLimit: 100, types });

describe('AlertApprovals', () => {
  beforeEach(() => mockApiFetch.mockReset());

  it('labels approval state', () => {
    expect(approvalLabel(base, false).text).toMatch(/whitelist off/);
    expect(approvalLabel(base, true).text).toBe('Pending approval');
    expect(approvalLabel(approved, true).text).toBe('Approved by a@b');
    expect(approvalLabel({ ...base, approved: true, approvedByOtherRule: true }, true).text).toMatch(/Dispatch Window/);
    expect(approvalLabel({ ...base, revoked: { revokedBy: 'a@b', revokedAt: '' } }, true).type).toBe('stopped');
  });

  it('approves a pending type', async () => {
    mockApiFetch.mockResolvedValueOnce(list([base])).mockResolvedValueOnce({ approved: true })
      .mockResolvedValueOnce(list([approved]));
    const notify = vi.fn();
    render(<AlertApprovals isAdmin notify={notify} />);
    fireEvent.click(await screen.findByText('Approve'));
    await waitFor(() => expect(notify).toHaveBeenCalledWith('success', expect.stringMatching(/Approved/)));
    expect(mockApiFetch.mock.calls[1][0]).toBe('/alerts/approve');
    expect(JSON.parse(mockApiFetch.mock.calls[1][1].body))
      .toEqual({ eventTypeCode: base.eventTypeCode, eventTypeCategory: 'scheduledChange' });
    expect(await screen.findByText('Approved by a@b')).toBeTruthy();
  });

  it('previews before sending held-back campaigns', async () => {
    mockApiFetch.mockResolvedValueOnce(list([approved]))
      .mockResolvedValueOnce({ dryRun: true, campaigns: 2, tickets: 75 })
      .mockResolvedValueOnce({ sent: 2, failed: [] })
      .mockResolvedValueOnce(list([{ ...approved, ticketableAccounts: 0 }]));
    const notify = vi.fn();
    render(<AlertApprovals isAdmin notify={notify} />);
    fireEvent.click((await screen.findAllByText('Send now'))[0]);
    await screen.findByText('Send held-back campaigns?');
    expect(JSON.parse(mockApiFetch.mock.calls[1][1].body).dryRun).toBe(true);
    const buttons = screen.getAllByText('Send now');
    fireEvent.click(buttons[buttons.length - 1]);
    await waitFor(() => expect(notify).toHaveBeenCalledWith('success', expect.stringMatching(/Sent 2 campaign/)));
    expect(JSON.parse(mockApiFetch.mock.calls[2][1].body).dryRun).toBe(false);
  });

  it('is read-only for viewers', async () => {
    mockApiFetch.mockResolvedValueOnce(list([base]));
    render(<AlertApprovals isAdmin={false} notify={vi.fn()} />);
    expect(await screen.findByText('Pending approval')).toBeTruthy();
    expect(screen.queryByText('Approve')).toBeNull();
    expect(screen.getByText(/Only administrators/)).toBeTruthy();
  });
});
