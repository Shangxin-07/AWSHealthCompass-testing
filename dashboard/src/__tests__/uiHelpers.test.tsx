import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';
import { accountLabel, TicketLink, ticketsSummary } from '../uiHelpers';

describe('uiHelpers', () => {
  it('labels accounts with their name when known', () => {
    expect(accountLabel('600000000001', 'lt-finance-prod')).toBe('lt-finance-prod (600000000001)');
    expect(accountLabel('600000000001', '')).toBe('600000000001');
    expect(accountLabel(undefined)).toBe('—');
  });

  it('links tickets only to https URLs', () => {
    const { rerender } = render(<TicketLink id="KAN-24" url="https://example.atlassian.net/browse/KAN-24" />);
    expect(screen.getByRole('link').getAttribute('href')).toBe('https://example.atlassian.net/browse/KAN-24');
    rerender(<TicketLink id="KAN-24" url="javascript:alert(1)" />);
    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.getByText('KAN-24')).toBeTruthy();
    rerender(<TicketLink />);
    expect(screen.getByText('—')).toBeTruthy();
  });

  it('summarises tickets', () => {
    expect(ticketsSummary(3, 10)).toBe('3/10 closed');
    expect(ticketsSummary(undefined, 4)).toBe('0/4 closed');
    expect(ticketsSummary(0, 0)).toBe('—');
  });
});

import { serverErrorMessage } from '../errors';

describe('serverErrorMessage', () => {
  it("returns the server's message from an API error body", () => {
    const e = new Error('API 403: {"error": {"code": "CFG_ORGS_ACCESS_DENIED", "message": "Use Bulk Import (CSV) instead."}}');
    expect(serverErrorMessage(e)).toBe('Use Bulk Import (CSV) instead.');
  });
  it('falls back to the generic mapping', () => {
    expect(serverErrorMessage(new Error('API 403: not json'))).toBe("You don't have permission to modify this configuration.");
    expect(serverErrorMessage('x')).toBe('An unexpected error occurred.');
  });
});
