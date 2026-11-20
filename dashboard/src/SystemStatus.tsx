import React, { useCallback, useEffect, useState } from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import StatusIndicator, { StatusIndicatorProps } from '@cloudscape-design/components/status-indicator';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import { apiFetch } from './api';

type State = 'ok' | 'warning' | 'error';
const TYPE: Record<State, StatusIndicatorProps.Type> = { ok: 'success', warning: 'warning', error: 'error' };

export function formatAge(seconds: number | null | undefined): string {
  if (seconds == null) return 'never';
  if (seconds < 90) return 'just now';
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}

/** Background jobs, queues and alarms (GET /api/system/status). */
export default function SystemStatus() {
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await apiFetch('/system/status');
      // Never let an unexpected shape break the page around this panel.
      if (!d || typeof d !== 'object' || !d.sync || !d.reconciliation || !d.overall) {
        throw new Error('unexpected response');
      }
      setData(d); setError(null);
    }
    catch (e: any) { setError(e.message); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const header = (
    <Header variant="h2" actions={<Button iconName="refresh" variant="icon" ariaLabel="Refresh status" loading={loading} onClick={load} />}
      description={data ? `Checked ${new Date(data.checkedAt).toLocaleTimeString()}` : undefined}>
      System Status{' '}
      {data && <StatusIndicator type={TYPE[data.overall as State]}>
        {data.overall === 'ok' ? 'All systems normal' : data.problems?.[0]}</StatusIndicator>}
    </Header>
  );
  if (error) return <Container header={header}><StatusIndicator type="error">Status unavailable: {error}</StatusIndicator></Container>;
  if (!data) return <Container header={header}><StatusIndicator type="loading">Checking…</StatusIndicator></Container>;

  const q = data.queues || {};
  const dlqs = Object.values(q).filter((x: any) => x.dlq) as any[];
  const work = Object.values(q).filter((x: any) => !x.dlq) as any[];
  const s = data.sync, r = data.reconciliation;

  return (
    <Container header={header}>
      <ColumnLayout columns={4} variant="text-grid">
        <div>
          <Box variant="awsui-key-label">Ticket Sync (hourly)</Box>
          <StatusIndicator type={TYPE[s.state as State]}>{s.message}</StatusIndicator>
          <Box variant="small">Last run {formatAge(s.ageSeconds)}{s.result ? ` · ${s.result}` : ''}{s.ticketsSynced != null ? ` · ${s.ticketsSynced} tickets` : ''}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Reconciliation (daily)</Box>
          <StatusIndicator type={TYPE[r.state as State]}>{r.message}</StatusIndicator>
          <Box variant="small">Last run {formatAge(r.ageSeconds)}{r.result ? ` · ${r.result}` : ''}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Dead-letter queues</Box>
          <StatusIndicator type={data.deadLetterMessages ? 'error' : 'success'}>
            {data.deadLetterMessages ? `${data.deadLetterMessages} failed message(s)` : 'Empty'}</StatusIndicator>
          <Box variant="small">{dlqs.map(d => d.name).join(', ')}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Alarms</Box>
          {data.alarmsFiring == null
            ? <StatusIndicator type="info">Unavailable</StatusIndicator>
            : <StatusIndicator type={data.alarmsFiring.length ? 'error' : 'success'}>
                {data.alarmsFiring.length ? `${data.alarmsFiring.length} firing` : 'None firing'}</StatusIndicator>}
          <Box variant="small">{(data.alarmsFiring || []).map((a: any) => a.name).join(', ')}</Box>
        </div>
      </ColumnLayout>
      {(data.problems?.length > 1 || work.some(w => w.visible + w.inFlight > 0)) && (
        <ExpandableSection headerText="Details">
          {data.problems?.map((p: string) => <div key={p}><StatusIndicator type="warning">{p}</StatusIndicator></div>)}
          {work.map(w => <Box key={w.name} variant="small">{w.name}: {w.visible} waiting, {w.inFlight} in progress</Box>)}
          {r.errorCodes?.length > 0 && <Box variant="small">Reconciliation errors: {r.errorCodes.join(', ')}</Box>}
        </ExpandableSection>
      )}
    </Container>
  );
}
