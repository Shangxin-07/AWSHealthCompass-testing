import React, { useState, useEffect } from 'react';
import Table from '@cloudscape-design/components/table';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import Container from '@cloudscape-design/components/container';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import ProgressBar from '@cloudscape-design/components/progress-bar';
import SplitPanel from '@cloudscape-design/components/split-panel';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import SegmentedControl from '@cloudscape-design/components/segmented-control';
import Button from '@cloudscape-design/components/button';
import type { Campaign, OnboardingConfig } from './types';
import { formatTitle, formatDays, daysLeft } from './types';
import { apiFetch } from './api';
import { usePlatformLabels } from './PlatformContext';
import { accountLabel, TicketLink, ticketsSummary } from './uiHelpers';

interface Props { campaigns: Campaign[]; config: OnboardingConfig | null; onRefresh: () => void; notify: (t: string, m: string) => void; onSync?: () => void; onSplitPanelChange: (panel: React.ReactNode) => void; }

export default function Campaigns({ campaigns, config, onRefresh, notify, onSync, onSplitPanelChange }: Props) {
  const labels = usePlatformLabels();
  const [view, setView] = useState('campaigns');
  const [selected, setSelected] = useState<Campaign | null>(null);
  const [detail, setDetail] = useState<Campaign | null>(null);

  const loadDetail = async (c: Campaign) => {
    setSelected(c);
    try { setDetail(await apiFetch(`/campaigns/${encodeURIComponent(c.campaignId)}`)); }
    catch { setDetail(c); }
  };

  useEffect(() => {
    if (selected && detail) {
      onSplitPanelChange(
        <SplitPanel header={formatTitle(detail)} closeBehavior="hide">
          <SpaceBetween size="m">
            <ColumnLayout columns={4}>
              <div><Box variant="awsui-key-label">Service</Box>{detail.service}</div>
              <div><Box variant="awsui-key-label">Deadline</Box>{detail.deadline || 'None'}</div>
              <div><Box variant="awsui-key-label">Resources resolved</Box>{detail.totalResources > 0 ? `${detail.resolvedResources}/${detail.totalResources} (${Math.round((detail.resolvedResources / detail.totalResources) * 100)}%)` : '—'}</div>
              <div><Box variant="awsui-key-label">Tickets</Box>{ticketsSummary(detail.ticketsClosed ?? detail.ticketsClosedResources, detail.ticketsCreated ?? detail.ticketedResources)}</div>
            </ColumnLayout>
            {detail.resources && detail.resources.length > 0 && (
              <Table items={detail.resources} columnDefinitions={[
                { id: 'arn', header: 'Resource', cell: (r: any) => r.resourceArn },
                { id: 'account', header: 'Account', cell: (r: any) => accountLabel(r.accountId, r.accountName) },
                { id: 'health', header: 'Health', cell: (r: any) =>
                  r.healthStatus === 'RESOLVED' ? <StatusIndicator type="success">Resolved</StatusIndicator>
                  : <StatusIndicator type="pending">Pending</StatusIndicator> },
                { id: 'ticket', header: labels.ticketColumn, cell: (r: any) => <TicketLink id={r.ticketId || r.jiraTicketKey} url={r.ticketUrl} /> },
                { id: 'status', header: labels.statusColumn, cell: (r: any) => r.jiraStatusName || r.ticketStatus || '—' },
              ]} />
            )}
            {detail.resourcesTruncated && (
              <Box variant="small">Showing the first {detail.resources?.length ?? 0} of {detail.totalResources} resources.</Box>
            )}
          </SpaceBetween>
        </SplitPanel>
      );
    } else {
      onSplitPanelChange(null);
    }
  }, [selected, detail]);

  const totalTicketed = campaigns.reduce((s, c) => s + c.ticketedResources, 0);
  const totalClosed = campaigns.reduce((s, c) => s + (c.ticketsClosedResources || 0), 0);
  const atRisk = campaigns.filter(c => daysLeft(c.deadline) <= 30 && daysLeft(c.deadline) >= 0).length;

  // Per-account totals across active campaigns, from the per-account stats
  // (the campaign list has no per-account data; using affectedAccount
  // credited a whole org-wide campaign to its first account).
  const [accounts, setAccounts] = useState<any[] | null>(null);
  const [accountsError, setAccountsError] = useState<string | null>(null);
  useEffect(() => {
    if (view !== 'account') return;
    apiFetch('/accounts?scope=active')
      .then((d: any) => { setAccounts(d.accounts || []); setAccountsError(null); })
      .catch((e: any) => setAccountsError(e.message));
  }, [view, campaigns]);

  return (
    <SpaceBetween size="l">
      <Header variant="h1" actions={onSync && <Button onClick={onSync} iconName="refresh">Sync</Button>}>Campaigns</Header>

      <ColumnLayout columns={4} variant="text-grid">
        <Container><Box variant="awsui-key-label">Active Campaigns</Box><Box variant="awsui-value-large">{campaigns.length}</Box></Container>
        <Container><Box variant="awsui-key-label">Tickets</Box><Box variant="awsui-value-large">{totalTicketed}</Box></Container>
        <Container><Box variant="awsui-key-label">Tickets Closed</Box><Box variant="awsui-value-large">{totalClosed}</Box></Container>
        <Container><Box variant="awsui-key-label">At Risk (&lt;30d)</Box><Box variant="awsui-value-large">{atRisk}</Box></Container>
      </ColumnLayout>

      <SegmentedControl
        selectedId={view}
        onChange={({ detail: d }) => { setView(d.selectedId); setSelected(null); setDetail(null); }}
        options={[
          { text: 'Campaigns', id: 'campaigns' },
          { text: 'By Account', id: 'account' },
        ]}
      />

      {view === 'campaigns' && (
        <Table
          items={campaigns}
          onSelectionChange={({ detail: d }) => d.selectedItems[0] && loadDetail(d.selectedItems[0])}
          selectionType="single" selectedItems={selected ? [selected] : []}
          columnDefinitions={[
            { id: 'title', header: 'Campaign', cell: c => formatTitle(c), sortingField: 'title' },
            { id: 'service', header: 'Service', cell: c => c.service },
            { id: 'progress', header: 'Resources resolved', cell: c => c.hasResources
              ? <ProgressBar value={c.totalResources > 0 ? (c.resolvedResources / c.totalResources) * 100 : 0}
                  additionalInfo={`${c.resolvedResources}/${c.totalResources}`} />
              : <StatusIndicator type={c.ticketedResources > 0 ? 'success' : 'pending'}>{c.ticketedResources > 0 ? 'Ticketed' : 'Pending'}</StatusIndicator>
            },
            { id: 'tickets', header: 'Tickets', cell: c => ticketsSummary(c.ticketsClosed ?? c.ticketsClosedResources, c.ticketsCreated ?? c.ticketedResources) },
            { id: 'deadline', header: 'Deadline', cell: c => formatDays(c.deadline), sortingField: 'deadline' },
          ]}
          empty={<Box textAlign="center"><b>No campaigns</b><Box>Create tickets from the Health Events dashboard</Box></Box>}
        />
      )}

      {view === 'account' && (
        <Table
          loading={accounts === null && !accountsError}
          loadingText="Loading accounts"
          items={accounts || []}
          header={accountsError ? <StatusIndicator type="error">{accountsError}</StatusIndicator> : undefined}
          columnDefinitions={[
            { id: 'acct', header: 'Account', cell: (r: any) => r.name ? `${r.name} (${r.accountId})` : r.accountId },
            { id: 'pending', header: 'Pending', cell: (r: any) => r.pending },
            { id: 'progress', header: 'Resolved', cell: (r: any) =>
              <ProgressBar value={r.total > 0 ? (r.resolved / r.total) * 100 : 0} additionalInfo={`${r.resolved}/${r.total}`} /> },
            { id: 'campaigns', header: 'Campaigns', cell: (r: any) => r.campaignsWithPending === r.campaigns
              ? r.campaigns : `${r.campaignsWithPending} of ${r.campaigns} with pending` },
            { id: 'deadline', header: 'Nearest Deadline', cell: (r: any) => r.nearestDeadline ? formatDays(r.nearestDeadline) : '—' },
          ]}
          empty={<Box textAlign="center"><b>No account data</b></Box>}
        />
      )}

    </SpaceBetween>
  );
}
