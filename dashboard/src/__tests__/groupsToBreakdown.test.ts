import { describe, it, expect } from 'vitest';
import { groupsToBreakdown } from '../Dashboard';

describe('groupsToBreakdown', () => {
  it('maps /breakdown groups to the Account Breakdown shape, labelled "name (id)"', () => {
    const out = groupsToBreakdown({ groups: [
      { groupKey: '610000000001', groupLabel: 'lt-finance-prod-001', total: 3, resolved: 1, pending: 2 },
      { groupKey: '610000000002', groupLabel: '610000000002', total: 2, resolved: 2, pending: 0 },
    ] });
    expect(out).toEqual({
      'lt-finance-prod-001 (610000000001)': { total: 3, resolved: 1, pending: 2 },
      '610000000002': { total: 2, resolved: 2, pending: 0 },
    });
  });

  it('falls back to a legacy breakdown map, or empty', () => {
    expect(groupsToBreakdown({ breakdown: { a: { total: 1, resolved: 0, pending: 1 } } }))
      .toEqual({ a: { total: 1, resolved: 0, pending: 1 } });
    expect(groupsToBreakdown(null)).toEqual({});
  });
});
