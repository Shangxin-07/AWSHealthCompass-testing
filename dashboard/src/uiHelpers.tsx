import React from 'react';
import Link from '@cloudscape-design/components/link';

/** "name (id)" when the account has a mapped name, else the id. */
export function accountLabel(accountId?: string, name?: string): string {
  if (!accountId) return '—';
  return name ? `${name} (${accountId})` : accountId;
}

/** Ticket id, linked to the ITSM when a safe https URL is stored. */
export function TicketLink({ id, url }: { id?: string; url?: string }) {
  if (!id) return <>—</>;
  if (url && /^https:\/\//i.test(url)) {
    return <Link external href={url} ariaLabel={`Open ticket ${id}`}>{id}</Link>;
  }
  return <>{id}</>;
}

/** "closed/created closed", or an em dash when there are no tickets. */
export function ticketsSummary(closed?: number, created?: number): string {
  if (!created) return '—';
  return `${closed || 0}/${created} closed`;
}
