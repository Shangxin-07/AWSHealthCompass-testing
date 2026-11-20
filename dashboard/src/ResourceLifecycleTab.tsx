import React, { useCallback, useEffect, useState } from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Select from '@cloudscape-design/components/select';
import Table from '@cloudscape-design/components/table';
import Button from '@cloudscape-design/components/button';
import Box from '@cloudscape-design/components/box';
import Alert from '@cloudscape-design/components/alert';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import { apiFetch } from './api';

type Status = 'PENDING' | 'RESOLVED';
interface CampaignOption { campaignId: string; title: string; totalResources: number; }
interface AccountRow { groupKey: string; groupLabel: string; total: number; pending: number; resolved: number; }
interface ResourceRow { resourceArn: string; accountId: string; healthStatus: string; ticketId?: string; jiraTicketKey?: string; }

/** Delay before re-reading counts: the replayed events go through the ingestion queue. */
export const REFRESH_DELAY_MS = 8000;

export default function ResourceLifecycleTab() {
  const [campaigns, setCampaigns] = useState<CampaignOption[]>([]);
  const [campaignId, setCampaignId] = useState<string | null>(null);
  const [accounts, setAccounts] = useState<AccountRow[] | null>(null);
  const [account, setAccount] = useState<AccountRow | null>(null);
  const [resources, setResources] = useState<ResourceRow[]>([]);
  const [nextToken, setNextToken] = useState<string | null>(null);
  const [selected, setSelected] = useState<ResourceRow[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<{ type: 'success' | 'error' | 'info'; text: string } | null>(null);

  useEffect(() => {
    apiFetch('/campaigns')
      .then((list: any[]) => setCampaigns((list || []).filter(c => c.hasResources)
        .map(c => ({ campaignId: c.campaignId, title: c.title, totalResources: c.totalResources }))))
      .catch(e => setMessage({ type: 'error', text: e.message }));
  }, []);

  const loadAccounts = useCallback(async (cid: string) => {
    const data = await apiFetch(`/campaigns/${encodeURIComponent(cid)}/breakdown`);
    setAccounts(data.groups || []);
  }, []);

  const loadResources = useCallback(async (cid: string, acct: string, token?: string) => {
    const qs = new URLSearchParams({ accountId: acct, limit: '200' });
    if (token) qs.set('nextToken', token);
    const data = await apiFetch(`/campaigns/${encodeURIComponent(cid)}/resources?${qs}`);
    setResources(prev => (token ? [...prev, ...(data.resources || [])] : data.resources || []));
    setNextToken(data.nextToken || null);
  }, []);

  const refresh = useCallback(async () => {
    if (!campaignId) return;
    await loadAccounts(campaignId);
    if (account) { setSelected([]); await loadResources(campaignId, account.groupKey); }
  }, [campaignId, account, loadAccounts, loadResources]);

  const pickCampaign = async (cid: string) => {
    setCampaignId(cid); setAccount(null); setResources([]); setSelected([]); setMessage(null);
    try { await loadAccounts(cid); } catch (e: any) { setMessage({ type: 'error', text: e.message }); }
  };

  const pickAccount = async (a: AccountRow) => {
    if (!campaignId) return;
    setAccount(a); setSelected([]);
    try { await loadResources(campaignId, a.groupKey); } catch (e: any) { setMessage({ type: 'error', text: e.message }); }
  };

  const apply = async (key: string, status: Status, scope: { accountId?: string; resourceArns?: string[] }) => {
    if (!campaignId) return;
    setBusy(key); setMessage(null);
    try {
      const r = await apiFetch('/test/resource-status', {
        method: 'POST', body: JSON.stringify({ campaignId, status, ...scope }),
      });
      if (!r.changed) {
        setMessage({ type: 'info', text: `Nothing to change — ${r.selected} resource(s) already ${status.toLowerCase()}.` });
      } else {
        setMessage({ type: 'success', text: `Sent ${r.eventsSent} Health event(s) marking ${r.changed} resource(s) ${status.toLowerCase()} across ${r.accounts} account(s). Refreshing shortly…` });
        setTimeout(() => { refresh().catch(() => {}); }, REFRESH_DELAY_MS);
      }
    } catch (e: any) {
      setMessage({ type: 'error', text: e.message });
    } finally { setBusy(null); }
  };

  const healthCell = (s: string) => s === 'RESOLVED'
    ? <StatusIndicator type="success">Resolved</StatusIndicator>
    : <StatusIndicator type="pending">Pending</StatusIndicator>;

  return (
    <SpaceBetween size="l">
      <Container header={<Header variant="h2" actions={<Button iconName="refresh" onClick={() => refresh()} disabled={!campaignId}>Refresh</Button>}>Resource Lifecycle</Header>}>
        <SpaceBetween size="m">
          <Alert type="info">
            Move a campaign's resources between pending and resolved by re-sending synthetic Health events
            through the normal pipeline — the same way AWS Health reports progress. Tickets are updated by the
            usual flow; ticket status in Compass refreshes on the hourly Sync.
          </Alert>
          <Select
            placeholder="Choose a campaign"
            selectedOption={campaignId ? { value: campaignId, label: campaigns.find(c => c.campaignId === campaignId)?.title } : null}
            onChange={({ detail }) => pickCampaign(detail.selectedOption.value!)}
            options={campaigns.map(c => ({ value: c.campaignId, label: c.title, description: `${c.totalResources} resources` }))}
            empty="No resource-level campaigns"
          />
          {message && <StatusIndicator type={message.type}>{message.text}</StatusIndicator>}
        </SpaceBetween>
      </Container>

      {accounts && (
        <Table
          header={<Header variant="h3" counter={`(${accounts.length})`}>Accounts</Header>}
          items={accounts}
          trackBy="groupKey"
          columnDefinitions={[
            { id: 'acct', header: 'Account', cell: a => <Button variant="inline-link" onClick={() => pickAccount(a)}>{a.groupLabel !== a.groupKey ? `${a.groupLabel} (${a.groupKey})` : a.groupKey}</Button> },
            { id: 'total', header: 'Resources', cell: a => a.total },
            { id: 'pending', header: 'Pending', cell: a => a.pending },
            { id: 'resolved', header: 'Resolved', cell: a => a.resolved },
            { id: 'actions', header: 'Actions', cell: a => (
              <SpaceBetween direction="horizontal" size="xs">
                <Button disabled={a.pending === 0} loading={busy === `${a.groupKey}:RESOLVED`}
                  onClick={() => apply(`${a.groupKey}:RESOLVED`, 'RESOLVED', { accountId: a.groupKey })}>Resolve all</Button>
                <Button disabled={a.resolved === 0} loading={busy === `${a.groupKey}:PENDING`}
                  onClick={() => apply(`${a.groupKey}:PENDING`, 'PENDING', { accountId: a.groupKey })}>Mark all pending</Button>
              </SpaceBetween>
            ) },
          ]}
          empty={<Box>No accounts</Box>}
        />
      )}

      {account && (
        <Table
          header={
            <Header variant="h3" counter={`(${resources.length}${nextToken ? '+' : ''})`}
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button disabled={!selected.length} loading={busy === 'sel:RESOLVED'}
                    onClick={() => apply('sel:RESOLVED', 'RESOLVED', { accountId: account.groupKey, resourceArns: selected.map(r => r.resourceArn) })}>
                    Mark selected resolved</Button>
                  <Button disabled={!selected.length} loading={busy === 'sel:PENDING'}
                    onClick={() => apply('sel:PENDING', 'PENDING', { accountId: account.groupKey, resourceArns: selected.map(r => r.resourceArn) })}>
                    Mark selected pending</Button>
                </SpaceBetween>
              }>
              Resources — {account.groupLabel !== account.groupKey ? `${account.groupLabel} (${account.groupKey})` : account.groupKey}
            </Header>
          }
          items={resources}
          trackBy="resourceArn"
          selectionType="multi"
          selectedItems={selected}
          onSelectionChange={({ detail }) => setSelected(detail.selectedItems.slice(0, 200))}
          columnDefinitions={[
            { id: 'arn', header: 'Resource ARN', cell: r => <Box variant="code">{r.resourceArn}</Box> },
            { id: 'health', header: 'Health', cell: r => healthCell(r.healthStatus) },
            { id: 'ticket', header: 'Ticket', cell: r => r.ticketId || r.jiraTicketKey || '—' },
          ]}
          footer={nextToken ? <Box textAlign="center"><Button onClick={() => campaignId && loadResources(campaignId, account.groupKey, nextToken)}>Load more</Button></Box> : undefined}
          empty={<Box>No resources</Box>}
        />
      )}
    </SpaceBetween>
  );
}
