import React, { useCallback, useEffect, useState } from 'react';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Container from '@cloudscape-design/components/container';
import Table from '@cloudscape-design/components/table';
import Button from '@cloudscape-design/components/button';
import Box from '@cloudscape-design/components/box';
import Alert from '@cloudscape-design/components/alert';
import Toggle from '@cloudscape-design/components/toggle';
import Modal from '@cloudscape-design/components/modal';
import TextFilter from '@cloudscape-design/components/text-filter';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import { apiFetch } from './api';
import { serverErrorMessage } from './errors';
import { formatAge } from './SystemStatus';

export interface AlertType {
  eventTypeCode: string; eventTypeCategory: string; service: string;
  campaigns: number; heldBack: number; accounts: number; resources: number;
  ticketableAccounts: number; lastSeen: string;
  approved: boolean | null;
  approval: { ruleId: string; approvedBy: string; approvedAt: string } | null;
  approvedByOtherRule: boolean;
  revoked: { revokedBy: string; revokedAt: string } | null;
}

interface Props { isAdmin: boolean; notify: (t: string, m: string) => void; }

const CATEGORY: Record<string, string> = { scheduledChange: 'Scheduled change', accountNotification: 'Account notification' };

function ageOf(iso: string): string {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? '—' : formatAge(Math.round((Date.now() - t) / 1000));
}

/** Approval state shown in the table. */
export function approvalLabel(t: AlertType, whitelist: boolean): { type: 'success' | 'pending' | 'info' | 'stopped'; text: string } {
  if (!whitelist) return { type: 'info', text: 'Sent automatically (whitelist off)' };
  if (t.approved && t.approval) return { type: 'success', text: `Approved by ${t.approval.approvedBy}` };
  if (t.approved) return { type: 'success', text: 'Approved (Dispatch Window rule)' };
  if (t.revoked) return { type: 'stopped', text: `Revoked by ${t.revoked.revokedBy}` };
  return { type: 'pending', text: 'Pending approval' };
}

export default function AlertApprovals({ isAdmin, notify }: Props) {
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [filter, setFilter] = useState('');
  const [confirmWhitelist, setConfirmWhitelist] = useState<boolean | null>(null);
  const [sendPreview, setSendPreview] = useState<{ type: AlertType; campaigns: number; tickets: number } | null>(null);

  const load = useCallback(async () => {
    try { setData(await apiFetch('/alerts')); setError(null); }
    catch (e: any) { setError(serverErrorMessage(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const post = async (key: string, path: string, body: any) => {
    setBusy(key);
    try { return await apiFetch(path, { method: 'POST', body: JSON.stringify(body) }); }
    catch (e: any) { notify('error', serverErrorMessage(e)); return null; }
    finally { setBusy(null); }
  };

  const setWhitelist = async (enabled: boolean) => {
    setConfirmWhitelist(null);
    if (await post('whitelist', '/alerts/whitelist', { enabled })) {
      notify('success', enabled ? 'Whitelist on: only approved alert types will be sent.' : 'Whitelist off: all actionable alerts will be sent.');
      load();
    }
  };

  const typeBody = (t: AlertType) => ({ eventTypeCode: t.eventTypeCode, eventTypeCategory: t.eventTypeCategory });

  const approve = async (t: AlertType) => {
    if (await post(`a:${t.eventTypeCode}:${t.eventTypeCategory}`, '/alerts/approve', typeBody(t))) {
      notify('success', `Approved ${t.eventTypeCode}. New events of this type will be sent (within about 5 minutes).`);
      load();
    }
  };
  const revoke = async (t: AlertType) => {
    if (await post(`r:${t.eventTypeCode}:${t.eventTypeCategory}`, '/alerts/revoke', typeBody(t))) {
      notify('success', `Revoked ${t.eventTypeCode}. New events of this type will be held back.`);
      load();
    }
  };
  const previewSend = async (t: AlertType) => {
    const r = await post(`s:${t.eventTypeCode}:${t.eventTypeCategory}`, '/alerts/send', { ...typeBody(t), dryRun: true });
    if (r) setSendPreview({ type: t, campaigns: r.campaigns, tickets: r.tickets });
  };
  const send = async () => {
    if (!sendPreview) return;
    const t = sendPreview.type;
    const r = await post('send', '/alerts/send', { ...typeBody(t), dryRun: false });
    setSendPreview(null);
    if (r) {
      notify(r.failed?.length ? 'warning' : 'success',
        `Sent ${r.sent} campaign(s) of ${t.eventTypeCode}${r.failed?.length ? `; ${r.failed.length} failed: ${r.failed[0].error}` : ''}.`);
      load();
    }
  };

  if (error) return <Alert type="error" header="Couldn't load alert types">{error}</Alert>;
  if (!data) return <StatusIndicator type="loading">Loading alert types…</StatusIndicator>;

  const whitelist: boolean = data.whitelistEnabled;
  const q = filter.toLowerCase();
  const items: AlertType[] = (data.types || []).filter((t: AlertType) =>
    !q || t.eventTypeCode.toLowerCase().includes(q) || t.service.toLowerCase().includes(q));
  const pending = (data.types || []).filter((t: AlertType) => whitelist && !t.approved).length;

  return (
    <SpaceBetween size="l">
      <Header variant="h1" description="Choose which AWS Health alert types are sent to your ITSM automatically.">
        Alert Approvals
      </Header>

      <Container header={<Header variant="h2">Whitelist</Header>}>
        <SpaceBetween size="s">
          <Toggle checked={whitelist} disabled={!isAdmin || busy === 'whitelist'}
            onChange={({ detail }) => setConfirmWhitelist(detail.checked)}>
            Only send approved alert types
          </Toggle>
          <Box variant="p">
            {whitelist
              ? `On. Alert types are held back until approved: ${pending} pending, ${data.rulesUsed} of ${data.ruleLimit} approval rules used. Held-back alerts are still recorded and visible in Compass.`
              : 'Off. Every actionable alert type is sent to the ITSM as it arrives.'}
          </Box>
          {!isAdmin && <Box variant="small">Only administrators can change approvals.</Box>}
        </SpaceBetween>
      </Container>

      <Table
        header={<Header variant="h2" counter={`(${items.length})`}
          actions={<Button iconName="refresh" onClick={load}>Refresh</Button>}
          description="Alert types seen so far. Approving affects new events; use Send now for campaigns already recorded.">
          Alert types</Header>}
        filter={<TextFilter filteringText={filter} filteringPlaceholder="Filter by service or event type"
          onChange={({ detail }) => setFilter(detail.filteringText)} />}
        items={items}
        trackBy={t => `${t.eventTypeCode}|${t.eventTypeCategory}`}
        columnDefinitions={[
          { id: 'type', header: 'Alert type', cell: t => <><Box fontWeight="bold">{t.eventTypeCode}</Box><Box variant="small">{t.service} · {CATEGORY[t.eventTypeCategory] || t.eventTypeCategory}</Box></> },
          { id: 'status', header: 'Status', cell: t => { const s = approvalLabel(t, whitelist); return <StatusIndicator type={s.type}>{s.text}</StatusIndicator>; } },
          { id: 'campaigns', header: 'Campaigns', cell: t => t.heldBack ? `${t.campaigns} (${t.heldBack} held back)` : t.campaigns },
          { id: 'accounts', header: 'Accounts', cell: t => t.accounts },
          { id: 'resources', header: 'Resources', cell: t => t.resources },
          { id: 'unticketed', header: 'Without ticket', cell: t => t.ticketableAccounts ? `${t.ticketableAccounts} account(s)` : '—' },
          { id: 'seen', header: 'Last seen', cell: t => ageOf(t.lastSeen) },
          { id: 'actions', header: 'Actions', cell: t => !isAdmin ? null : (
            <SpaceBetween direction="horizontal" size="xs">
              {whitelist && !t.approved && (
                <Button loading={busy === `a:${t.eventTypeCode}:${t.eventTypeCategory}`} onClick={() => approve(t)}>Approve</Button>)}
              {whitelist && t.approved && t.approval && (
                <Button loading={busy === `r:${t.eventTypeCode}:${t.eventTypeCategory}`} onClick={() => revoke(t)}>Revoke</Button>)}
              {(!whitelist || t.approved) && t.ticketableAccounts > 0 && (
                <Button variant="primary" loading={busy === `s:${t.eventTypeCode}:${t.eventTypeCategory}`} onClick={() => previewSend(t)}>Send now</Button>)}
            </SpaceBetween>) },
        ]}
        empty={<Box textAlign="center">No alerts recorded yet.</Box>}
      />

      <Modal visible={confirmWhitelist !== null} onDismiss={() => setConfirmWhitelist(null)}
        header={confirmWhitelist ? 'Turn the whitelist on?' : 'Turn the whitelist off?'}
        footer={<Box float="right"><SpaceBetween direction="horizontal" size="xs">
          <Button onClick={() => setConfirmWhitelist(null)}>Cancel</Button>
          <Button variant="primary" onClick={() => setWhitelist(!!confirmWhitelist)}>{confirmWhitelist ? 'Turn on' : 'Turn off'}</Button>
        </SpaceBetween></Box>}>
        {confirmWhitelist
          ? 'Only approved alert types will be sent to the ITSM. Everything else is recorded and held back until approved.'
          : 'Every actionable alert type will be sent to the ITSM as it arrives, including types nobody has approved.'}
      </Modal>

      <Modal visible={sendPreview !== null} onDismiss={() => setSendPreview(null)}
        header="Send held-back campaigns?"
        footer={<Box float="right"><SpaceBetween direction="horizontal" size="xs">
          <Button onClick={() => setSendPreview(null)}>Cancel</Button>
          <Button variant="primary" loading={busy === 'send'} disabled={!sendPreview?.tickets} onClick={send}>Send now</Button>
        </SpaceBetween></Box>}>
        {sendPreview && (
          <SpaceBetween size="s">
            <Box>This sends <b>{sendPreview.campaigns}</b> campaign(s) of <b>{sendPreview.type.eventTypeCode}</b> to the ITSM,
              creating about <b>{sendPreview.tickets}</b> ticket(s) (one per account without a ticket).</Box>
            <Box variant="small">Accounts that already have a ticket are skipped.</Box>
          </SpaceBetween>
        )}
      </Modal>
    </SpaceBetween>
  );
}
