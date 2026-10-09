import { useCallback, useEffect, useMemo, useState } from 'react'
import VoiceLab from './VoiceLab'

type Section = 'Overview' | 'Contacts' | 'Campaigns' | 'Voice lab' | 'Live calls' | 'Review' | 'Settings'
type Overview = { contacts: number; suppressed: number; campaigns: number; attempts: number; active_calls: number; spend_inr: number; attempt_status: Record<string, number>; outcomes: Record<string, number> }
type Contact = { id: string; external_id: string; name: string; phone: string; reminder_label: string; reminder_at: string; language: string; consent_source: string; suppressed: boolean; pilot_self_test: boolean }
type Campaign = { id: string; name: string; status: string; language: string; voice: string; start_hour: number; end_hour: number; max_concurrent: number; retry_limit: number; budget_inr: number; created_at: string }
type Call = { id: string; direction: string; provider?: string; status: string; outcome: string | null; language: string; created_at: string; events?: { kind: string; text: string; latency_ms: number | null; created_at: string }[] }
type Audit = { action: string; target_id: string; detail: string; created_at: string }
type CampaignMetrics = { campaign_id: string; attempts: number; connected: number; outcomes: Record<string, number>; interested_parent_rate: number | null; qualified_subscription_intent_rate: number | null; opt_out_rate: number | null; completed_subscriptions: number | null; callback_conversion_rate: number | null }

const sections: { label: Section; glyph: string; group: string }[] = [
  { label: 'Overview', glyph: '◫', group: 'WORKSPACE' },
  { label: 'Contacts', glyph: '♙', group: 'WORKSPACE' },
  { label: 'Campaigns', glyph: '▣', group: 'WORKSPACE' },
  { label: 'Voice lab', glyph: '◉', group: 'BUILD & TEST' },
  { label: 'Live calls', glyph: '◌', group: 'BUILD & TEST' },
  { label: 'Review', glyph: '▤', group: 'BUILD & TEST' },
  { label: 'Settings', glyph: '⚙', group: 'ADMIN' },
]

const API = import.meta.env.VITE_API_URL || ''
async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, { headers: { ...(options?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }) }, ...options })
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try { detail = (await response.json()).detail || detail } catch { /* no JSON body */ }
    throw new Error(detail)
  }
  return response.json() as Promise<T>
}

const shortDate = (value: string) => {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' })
}
const titleCase = (value: string | null) => value ? value.replaceAll('_', ' ').replace(/\b\w/g, c => c.toUpperCase()) : 'Pending'

function App() {
  const [section, setSection] = useState<Section>('Overview')
  const [overview, setOverview] = useState<Overview | null>(null)
  const [contacts, setContacts] = useState<Contact[]>([])
  const [campaigns, setCampaigns] = useState<Campaign[]>([])
  const [campaignMetrics, setCampaignMetrics] = useState<CampaignMetrics | null>(null)
  const [calls, setCalls] = useState<Call[]>([])
  const [audit, setAudit] = useState<Audit[]>([])
  const [selectedCall, setSelectedCall] = useState<Call | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [health, setHealth] = useState<{ mode: string; call_mode: string } | null>(null)
  const [voiceLabUsed, setVoiceLabUsed] = useState(false)
  const [pilotScenario, setPilotScenario] = useState<'saved_reminder' | 'festival_sale_demo' | 'actnoww_subscription'>('saved_reminder')
  const [campaignForm, setCampaignForm] = useState({ name: '', language: 'hi-IN', voice: 'priya', start_hour: 9, end_hour: 18, max_concurrent: 5, retry_limit: 0, budget_inr: 100 })

  const refresh = useCallback(async () => {
    try {
      const [o, c, m, v, a, h] = await Promise.all([
        request<Overview>('/v1/overview'), request<{ items: Contact[] }>('/v1/contacts'),
        request<{ items: Campaign[] }>('/v1/campaigns'), request<{ items: Call[] }>('/v1/calls'),
        request<{ items: Audit[] }>('/v1/audit'), request<{ mode: string; call_mode: string }>('/health')
      ])
      setOverview(o); setContacts(c.items); setCampaigns(m.items); setCalls(v.items); setAudit(a.items); setHealth(h)
      setError('')
    } catch (exc) { setError(exc instanceof Error ? exc.message : 'Could not load dashboard') }
    finally { setLoading(false) }
  }, [])
  useEffect(() => { void refresh(); const timer = window.setInterval(() => { void refresh() }, 15000); return () => clearInterval(timer) }, [refresh])

  const action = async (fn: () => Promise<unknown>, success: string): Promise<boolean> => {
    setBusy(true); setError(''); setNotice('')
    try { await fn(); setNotice(success); await refresh(); return true }
    catch (exc) { setError(exc instanceof Error ? exc.message : 'Operation failed'); return false }
    finally { setBusy(false) }
  }

  const importFile = async (file: File) => {
    const form = new FormData(); form.set('file', file)
    await action(async () => {
      const result = await request<{ accepted: number; updated: number; error_count: number }>('/v1/imports', { method: 'POST', body: form })
      return result
    }, 'Import complete; open the contact directory to review accepted rows.')
  }

  const startPilotCall = async (contact: Contact) => {
    const context = pilotScenario === 'festival_sale_demo' ? 'synthetic Vijay Sales festival sale demo' : pilotScenario === 'actnoww_subscription' ? 'real Actnoww subscription campaign' : 'saved service reminder'
    if (pilotScenario === 'actnoww_subscription' && !campaigns.some(c => c.name === 'Actnoww Kids Learning Subscription' && c.status === 'draft')) {
      setError('Create the Actnoww campaign draft in Campaigns first.'); setSection('Campaigns'); return
    }
    if (pilotScenario === 'actnoww_subscription' && !contact.pilot_self_test) {
      setError('The Actnoww pilot is limited to your configured self-test number.'); return
    }
    if (!window.confirm(`Place one live AI call to ${contact.name} (${contact.phone}) using the ${context}? This uses Exotel and Sarvam credits.`)) return
    const promotionalConsent = pilotScenario === 'actnoww_subscription'
      ? window.confirm('Confirm this is your own number and you consent to receive this real Actnoww promotional test call. Select Cancel to stop.')
      : false
    if (pilotScenario === 'actnoww_subscription' && !promotionalConsent) return
    const queued = await action(() => request(`/v1/contacts/${contact.id}/pilot-call`, {
      method: 'POST', body: JSON.stringify({ scenario: pilotScenario, promotional_consent_confirmed: promotionalConsent }),
    }),
      'One live call queued. Check Live calls for status and Review for the transcript.')
    if (queued) setSection('Live calls')
  }

  const restoreSelfTest = async (contact: Contact) => {
    if (!window.confirm(`Restore ${contact.name} (${contact.phone}) for your own synthetic test calls? Only continue if you still consent to receive them.`)) return
    await action(() => request(`/v1/contacts/${contact.id}/restore-self-test`, { method: 'POST' }),
      'Self-test contact restored. Review the call context before clicking Call once.')
  }

  const downloadTemplate = () => {
    const csv = 'external_id,name,phone,reminder_at,reminder_label,language,consent_source,consent_at\nEX-001,Ananya Sharma,+919876543210,2026-10-08T10:00:00+05:30,service appointment,hi-IN,booking form,2026-10-01T12:00:00+05:30\n'
    const link = document.createElement('a'); link.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' })); link.download = 'swarvaah-contacts-template.csv'; link.click(); URL.revokeObjectURL(link.href)
  }


  const completed = overview?.attempt_status.completed || 0
  const answerRate = overview?.attempts ? Math.round(completed / overview.attempts * 100) : 0
  const chart = useMemo(() => [42, 53, 38, 62, 50, 67, 57, 73, 61, 82, 69, 88], [])

  return <div className="app theme-light">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><span>〰</span></div><div><strong>Swarvaah <em>AI</em></strong><small>VOICE OPERATIONS</small></div></div>
      <div className="workspace-switch"><div className="workspace-avatar">S</div><div><strong>Swarvaah Workspace</strong><small>Primary organization</small></div><span className="chevron">⌄</span></div>
      <nav aria-label="Main navigation">{sections.map((item, index) => <div key={item.label}>{(index === 0 || sections[index - 1].group !== item.group) && <div className="nav-group">{item.group}</div>}<button type="button" className={`nav-item ${section === item.label ? 'active' : ''}`} onClick={() => setSection(item.label)}><span className="nav-glyph">{item.glyph}</span>{item.label}{item.label === 'Live calls' && <span className="nav-count">{overview?.active_calls || 0}</span>}</button></div>)}</nav>
      <div className="sidebar-bottom"><div className="system-state"><span className="pulse-dot"/><div><strong>All systems operational</strong><small>{health?.call_mode === 'simulation' ? 'Simulation mode' : 'Live dial mode'}</small></div></div><div className="user-row"><div className="user-avatar">MS</div><div><strong>Manmeet Singh</strong><small>Workspace admin</small></div><span>⋯</span></div></div>
    </aside>
    <main className="main">
      <header className="topbar"><div className="breadcrumb">Workspace <span>/</span> <strong>{section}</strong></div><div className="top-actions"><span className="env-pill"><span className="tiny-dot"/>{health?.mode === 'demo' ? 'DEMO ENVIRONMENT' : health?.mode === 'pilot' ? 'LOCAL PHONE PILOT' : 'PRODUCTION'}</span><div className="top-avatar">MS</div></div></header>
      <div className="content">
        {notice && <div className="toast success" role="status"><span>✓</span>{notice}<button onClick={() => setNotice('')}>×</button></div>}
        {error && <div className="toast error" role="alert"><span>!</span>{error}<button onClick={() => setError('')}>×</button></div>}
        {loading ? <div className="loading"><div className="spinner"/> Loading workspace…</div> : <>
          {section === 'Overview' && <><div className="page-head"><div><div className="eyebrow">{new Date().toLocaleDateString('en-IN', { weekday: 'long', day: '2-digit', month: 'long', year: 'numeric' }).toUpperCase()}</div><h1>Good evening, Manmeet <span className="head-spark">✳</span></h1><p>Here is what is happening across your voice operations.</p></div><button className="primary-button" onClick={() => setSection('Campaigns')}>＋ New campaign</button></div>
            <div className="hero-panel"><div className="hero-copy"><span className="hero-kicker"><span className="tiny-dot"/> PLATFORM STATUS</span><h2>Every conversation,<br/><em>in its own voice.</em></h2><p>Build thoughtful, human-feeling service calls with complete control over quality, capacity and compliance.</p><div className="hero-actions"><button onClick={() => setSection('Voice lab')}>Open voice lab <span>↗</span></button><span>OUTBOUND ACTIVE · INBOUND PLANNED</span></div></div><div className="hero-art" aria-hidden="true"><div className="orb orb-one"/><div className="orb orb-two"/><div className="orb orb-three"/><div className="wave-line">▁▂▃▅▇▅▃▂▁▃▆█▆▃▁▂▄▇▅▃▂▁</div></div></div>
            <div className="metrics-grid"><Metric label="TOTAL CONTACTS" value={(overview?.contacts || 0).toLocaleString()} icon="♙" hint="Ready for campaigns"/><Metric label="CALL ATTEMPTS" value={(overview?.attempts || 0).toLocaleString()} icon="↗" hint="Across all campaigns"/><Metric label="ACTIVE CALLS" value={(overview?.active_calls || 0).toLocaleString()} icon="◉" hint="In progress now" live/><Metric label="COMPLETION RATE" value={`${answerRate}%`} icon="◷" hint="Simulated or completed"/></div>
            <div className="two-col"><div className="panel performance"><div className="panel-title"><div><h3>Calling activity</h3><p>Operational preview</p></div><span className="subtle-pill">Demo data</span></div><div className="chart"><div className="gridline g1"/><div className="gridline g2"/><div className="gridline g3"/>{chart.map((h, i) => <div className="bar-wrap" key={i}><div className={`bar ${i === 11 ? 'highlight' : ''}`} style={{height: `${h}%`}}/></div>)}</div><div className="chart-labels"><span>09:00</span><span>11:00</span><span>13:00</span><span>15:00</span><span>17:00</span></div></div><div className="panel"><div className="panel-title"><div><h3>Quick start</h3><p>Get your first test conversation running</p></div></div><div className="steps"><Step index="01" title="Add your contacts" detail="Import consented recipients from CSV" done={!!overview?.contacts} onClick={() => setSection('Contacts')}/><Step index="02" title="Create a campaign" detail="Set voice, script and calling limits" done={!!overview?.campaigns} onClick={() => setSection('Campaigns')}/><Step index="03" title="Test the voice" detail="Try a browser conversation first" done={voiceLabUsed} onClick={() => setSection('Voice lab')}/></div>{health?.mode === 'demo' && <button className="text-link" onClick={() => void action(async () => { await request('/v1/demo/seed', {method:'POST'}) }, 'Sample workspace ready')}>Load sample workspace <span>→</span></button>}</div></div>
            <div className="panel recent-panel"><div className="panel-title"><div><h3>Recent campaigns</h3><p>Your latest outbound operations</p></div><button className="text-link" onClick={() => setSection('Campaigns')}>View all →</button></div><CampaignTable campaigns={campaigns.slice(0,4)} onOpen={() => setSection('Campaigns')}/></div>
          </>}
          {section === 'Contacts' && <><PageTitle kicker="AUDIENCE" title="Contacts" description="Import verified recipients and keep consent visible at every step."/><div className="two-col top-cols"><div className="panel upload-panel"><div className="panel-title"><div><h3>Import a CSV</h3><p>Validate and deduplicate your audience</p></div><span className="box-icon">⇧</span></div><label className="dropzone"><input type="file" accept=".csv,text/csv" onChange={e => { const file = e.target.files?.[0]; if (file) void importFile(file) }}/><span className="drop-icon">⇧</span><strong>Drop a CSV here or browse files</strong><small>Maximum 10 MB · UTF-8 · Indian phone numbers</small></label><button className="text-link" onClick={downloadTemplate}>Download CSV template ↓</button></div><div className="panel info-panel"><h3>Import requirements</h3><p>Each contact needs a valid +91 number, reminder details, and a record of permission to receive this service call.</p><div className="requirement"><span>✓</span> Phone number in E.164 format</div><div className="requirement"><span>✓</span> Consent source and timestamp</div><div className="requirement"><span>✓</span> Unique external contact ID</div><div className="requirement"><span>✓</span> Hindi or English preference</div>{health?.mode === 'pilot' && <div className="pilot-context-select"><label htmlFor="pilot-scenario">Context for the next call</label><select id="pilot-scenario" value={pilotScenario} onChange={e => setPilotScenario(e.target.value as 'saved_reminder' | 'festival_sale_demo' | 'actnoww_subscription')}><option value="saved_reminder">Saved service reminder</option><option value="festival_sale_demo">Synthetic Vijay Sales festival sale</option><option value="actnoww_subscription">Actnoww Kids Learning Subscription</option></select><small>{pilotScenario === 'actnoww_subscription' ? 'Real Actnoww promotional pilot on your own opted-in number. One course: ₹999. Checkout details are not configured. Create its draft in Campaigns first.' : 'The Vijay Sales scenario is a fictional test on your configured own number.'}</small></div>}</div></div><div className="panel table-panel"><div className="panel-title"><div><h3>Contact directory</h3><p>{contacts.length} visible contacts · {overview?.suppressed || 0} suppressed</p></div><span className="subtle-pill">Page 1</span></div>{contacts.length ? <div className="table-scroll"><table><thead><tr><th>CONTACT</th><th>PHONE</th><th>REMINDER</th><th>LANGUAGE</th><th>STATUS</th><th/></tr></thead><tbody>{contacts.map(c => <tr key={c.id}><td><strong>{c.name}</strong><small>{c.external_id}</small></td><td>{c.phone}</td><td>{c.reminder_label}<small>{shortDate(c.reminder_at)}</small></td><td>{c.language}</td><td><Status value={c.suppressed ? 'suppressed' : 'eligible'}/></td><td><div className="row-actions">{c.suppressed && c.pilot_self_test && <button className="row-action" disabled={busy} onClick={() => void restoreSelfTest(c)}>Restore self-test</button>}{!c.suppressed && health?.mode === 'pilot' && health?.call_mode === 'exotel' && <button className="row-action" disabled={busy} onClick={() => void startPilotCall(c)}>Call once ↗</button>}{!c.suppressed && <button className="row-action" disabled={busy} onClick={() => void action(() => request(`/v1/contacts/${c.id}/suppress`, { method:'POST' }), 'Contact suppressed')}>Suppress</button>}</div></td></tr>)}</tbody></table></div> : <Empty title="No contacts yet" detail="Upload a CSV or load the sample workspace to start." action="Load sample data" onClick={() => void action(() => request('/v1/demo/seed',{method:'POST'}),'Sample workspace ready')}/>}</div></>}
          {section === 'Campaigns' && <><PageTitle kicker="OUTBOUND" title="Campaigns" description="Plan careful, capacity-aware service calls before launching them."/><div className="two-col top-cols"><div className="panel form-panel"><div className="panel-title"><div><h3>Create campaign</h3><p>A new campaign starts in draft mode</p></div></div><div className="form-grid"><label className="field full">Campaign name<input value={campaignForm.name} onChange={e => setCampaignForm({...campaignForm,name:e.target.value})} placeholder="October service reminders"/></label><label className="field">Language<select value={campaignForm.language} onChange={e => setCampaignForm({...campaignForm,language:e.target.value})}><option value="hi-IN">Hindi + Hinglish</option><option value="en-IN">English</option></select></label><label className="field">Voice<select value={campaignForm.voice} onChange={e => setCampaignForm({...campaignForm,voice:e.target.value})}><option value="priya">Priya</option><option value="shubh">Shubh</option></select></label><label className="field">Start hour (IST)<input type="number" min="0" max="23" value={campaignForm.start_hour} onChange={e => setCampaignForm({...campaignForm,start_hour:Number(e.target.value)})}/></label><label className="field">End hour (IST)<input type="number" min="1" max="24" value={campaignForm.end_hour} onChange={e => setCampaignForm({...campaignForm,end_hour:Number(e.target.value)})}/></label><label className="field">Max concurrent<input type="number" min="1" value={campaignForm.max_concurrent} onChange={e => setCampaignForm({...campaignForm,max_concurrent:Number(e.target.value)})}/></label><label className="field">Budget (₹)<input type="number" min="1" value={campaignForm.budget_inr} onChange={e => setCampaignForm({...campaignForm,budget_inr:Number(e.target.value)})}/></label></div><div className="form-footer"><span>Calls only to eligible, consented contacts.</span><button className="primary-button" disabled={busy || !campaignForm.name.trim()} onClick={() => void action(() => request('/v1/campaigns',{method:'POST',body:JSON.stringify(campaignForm)}),'Campaign created as draft')}>Create draft →</button></div></div><div className="panel launch-notes"><div className="note-symbol">◈</div><h3>Actnoww Kids Learning Subscription</h3><p>Real, single-contact promotional pilot. The AI introduces Actnoww, asks about early learning needs, states only approved facts and the ₹999 one-course price, and records interest or opt-out. Checkout is not configured.</p><button className="primary-button" disabled={busy} onClick={() => void action(() => request('/v1/campaigns/actnoww-draft', {method:'POST'}), 'Actnoww campaign draft is ready. Select it in Contacts before calling.')}>Create Actnoww draft →</button><h3>Controlled by design</h3><p>Campaigns launch only after you review the audience, calling window, capacity limit, and spend cap. Live dialing also requires explicit server configuration.</p><div className="note-list"><span>01 <strong>Consent screening</strong></span><span>02 <strong>IST calling windows</strong></span><span>03 <strong>Capacity admission</strong></span><span>04 <strong>Instant pause</strong></span></div></div></div><div className="panel table-panel"><div className="panel-title"><div><h3>All campaigns</h3><p>Drafts and active programs</p></div></div>{campaigns.length ? <div className="table-scroll"><table><thead><tr><th>CAMPAIGN</th><th>LANGUAGE</th><th>WINDOW</th><th>LIMIT</th><th>STATUS</th><th>ACTIONS</th></tr></thead><tbody>{campaigns.map(c => <tr key={c.id}><td><strong>{c.name}</strong><small>{shortDate(c.created_at)}</small></td><td>{c.language}</td><td>{c.start_hour}:00–{c.end_hour}:00 IST</td><td>{c.max_concurrent} concurrent</td><td><Status value={c.status}/></td><td><div className="row-actions">{c.name === 'Actnoww Kids Learning Subscription' && <button onClick={async () => { try { setCampaignMetrics(await request<CampaignMetrics>(`/v1/campaigns/${c.id}/metrics`)) } catch { setError('Could not load campaign metrics') } }} disabled={busy}>Metrics</button>}{health?.mode === 'demo' && ['draft','paused'].includes(c.status) && <button onClick={() => void action(() => request(`/v1/campaigns/${c.id}/launch`,{method:'POST'}),'Campaign launched')} disabled={busy}>Launch</button>}{c.status === 'active' && <button onClick={() => void action(() => request(`/v1/campaigns/${c.id}/pause`,{method:'POST'}),'Campaign paused')} disabled={busy}>Pause</button>}{c.status !== 'stopped' && <button onClick={() => void action(() => request(`/v1/campaigns/${c.id}/stop`,{method:'POST'}),'Campaign stopped')} disabled={busy}>Stop</button>}</div></td></tr>)}</tbody></table></div> : <Empty title="No campaigns created" detail="Create a draft to configure your first outbound program."/>}{campaignMetrics && <div className="campaign-metrics"><h3>Actnoww pilot results</h3><p>{campaignMetrics.attempts} attempts · {campaignMetrics.connected} connected · {campaignMetrics.outcomes.no_answer || 0} no answer</p><p>Interested-parent rate: {campaignMetrics.interested_parent_rate === null ? '—' : `${Math.round(campaignMetrics.interested_parent_rate * 100)}%`} · Qualified subscription intent: {campaignMetrics.qualified_subscription_intent_rate === null ? '—' : `${Math.round(campaignMetrics.qualified_subscription_intent_rate * 100)}%`} · Opt-out rate: {campaignMetrics.opt_out_rate === null ? '—' : `${Math.round(campaignMetrics.opt_out_rate * 100)}%`}</p><p>Completed subscriptions and callback conversion need verified checkout and callback events.</p></div>}</div></>}
          {section === 'Voice lab' && <VoiceLab onSessionStarted={() => setVoiceLabUsed(true)} />}
          {section === 'Live calls' && <><PageTitle kicker="OPERATIONS" title="Live calls" description="A clear view of each call and its current state."/><div className="metrics-grid compact"><Metric label="ACTIVE NOW" value={String(overview?.active_calls || 0)} icon="◉" hint="Dialing or connected" live/><Metric label="QUEUED" value={String(overview?.attempt_status.queued || 0)} icon="◷" hint="Awaiting capacity"/><Metric label="COMPLETED" value={String(overview?.attempt_status.completed || 0)} icon="✓" hint="Finalized attempts"/><Metric label="FAILED" value={String(overview?.attempt_status.failed || 0)} icon="!" hint="Needs review"/></div><div className="panel table-panel"><div className="panel-title"><div><h3>Call activity</h3><p>Refreshes every 15 seconds</p></div><button className="text-link" onClick={() => void refresh()}>Refresh ↻</button></div>{calls.length ? <CallTable calls={calls} onOpen={async c => { const detail = await request<Call>(`/v1/calls/${c.id}`); setSelectedCall(detail) }}/> : <Empty title="No calls to show" detail="Run a voice lab test or launch a simulated campaign." action="Open voice lab" onClick={() => setSection('Voice lab')}/>}</div></>}
          {section === 'Review' && <><PageTitle kicker="QUALITY" title="Call review" description="Inspect outcomes, decisions, and the conversation behind them."/><div className="two-col review-grid"><div className="panel table-panel"><div className="panel-title"><div><h3>Recent sessions</h3><p>Select a call to inspect its timeline</p></div></div>{calls.length ? <div className="review-list">{calls.map(c => <button key={c.id} className={`review-item ${selectedCall?.id === c.id ? 'selected' : ''}`} onClick={async () => { try { setSelectedCall(await request<Call>(`/v1/calls/${c.id}`)) } catch { setError('Could not open call') } }}><span className="review-icon">◉</span><span><strong>{titleCase(c.outcome)}</strong><small>{shortDate(c.created_at)} · {c.language}</small></span><span>→</span></button>)}</div> : <Empty title="No call history" detail="Completed tests and campaigns will appear here."/>}</div><div className="panel detail-panel"><div className="panel-title"><div><h3>Session timeline</h3><p>{selectedCall ? selectedCall.id : 'Select a session'}</p></div>{selectedCall && <Status value={selectedCall.status}/>}</div>{selectedCall ? <><div className="detail-meta"><span>Direction <strong>{selectedCall.direction}</strong></span><span>Provider <strong>{selectedCall.provider || 'Unknown'}</strong></span><span>Outcome <strong>{titleCase(selectedCall.outcome)}</strong></span><span>Language <strong>{selectedCall.language}</strong></span></div><div className="timeline">{selectedCall.events?.map((e,i) => <div className="timeline-item" key={i}><span className={`timeline-dot ${e.kind}`}/><div><small>{e.kind.toUpperCase()} · {shortDate(e.created_at)}</small><p>{e.text}</p></div></div>)}</div><p className="muted footnote">Recording is unavailable in the local demo. Outcome corrections are audited through the API.</p></> : <div className="detail-empty">Choose a session to view its transcript and tool actions.</div>}</div></div></>}
          {section === 'Settings' && <><PageTitle kicker="ADMINISTRATION" title="Settings" description="Provider, security, and operational controls for this workspace."/><div className="settings-grid"><div className="panel"><div className="panel-title"><div><h3>Environment</h3><p>Current application state</p></div></div><div className="settings-row"><span>Platform mode</span><Status value={health?.mode || 'unknown'}/></div><div className="settings-row"><span>Dial mode</span><strong>{health?.call_mode || 'Unknown'}</strong></div><div className="settings-row"><span>Default region</span><strong>India · Mumbai</strong></div><div className="settings-row"><span>Inbound calling</span><span className="subtle-pill">Planned</span></div></div><div className="panel"><div className="panel-title"><div><h3>Operational safeguards</h3><p>Built into outbound processing</p></div></div>{['Consent and suppression check','India calling window','Capacity admission','Global dial-rate limit','Campaign pause and stop','Audited operator actions'].map(item => <div className="requirement" key={item}><span>✓</span>{item}</div>)}</div><div className="panel audit-panel"><div className="panel-title"><div><h3>Audit trail</h3><p>Latest workspace actions</p></div></div>{audit.length ? audit.slice(0,10).map((a,i) => <div className="audit-row" key={i}><div><strong>{a.action.replaceAll('.',' · ')}</strong><small>{a.target_id.slice(0,12)}</small></div><span>{shortDate(a.created_at)}</span></div>) : <div className="detail-empty">Actions will appear here.</div>}</div></div></>}
        </>}
      </div>
    </main>
  </div>
}

function Metric({label,value,icon,hint,live}: {label:string;value:string;icon:string;hint:string;live?:boolean}) { return <div className="metric"><div className="metric-top"><span>{label}</span><span className="metric-icon">{icon}</span></div><strong>{value}</strong><small>{live && <span className="tiny-dot"/>}{hint}</small></div> }
function PageTitle({kicker,title,description}: {kicker:string;title:string;description:string}) { return <div className="page-head"><div><div className="eyebrow">{kicker}</div><h1>{title}</h1><p>{description}</p></div></div> }
function Status({value}: {value:string}) { return <span className={`status status-${value.replaceAll('_','-')}`}><span/>{titleCase(value)}</span> }
function Step({index,title,detail,done,onClick}: {index:string;title:string;detail:string;done:boolean;onClick:()=>void}) { return <button className="step" onClick={onClick}><span className={`step-index ${done?'done':''}`}>{done?'✓':index}</span><span><strong>{title}</strong><small>{detail}</small></span><span className="step-arrow">↗</span></button> }
function Empty({title,detail,action,onClick}: {title:string;detail:string;action?:string;onClick?:()=>void}) { return <div className="empty"><div>◇</div><h3>{title}</h3><p>{detail}</p>{action && <button className="text-link" onClick={onClick}>{action} →</button>}</div> }
function CampaignTable({campaigns,onOpen}: {campaigns:Campaign[];onOpen:()=>void}) { return campaigns.length ? <div className="table-scroll"><table><thead><tr><th>CAMPAIGN</th><th>LANGUAGE</th><th>STATUS</th><th>CREATED</th><th/></tr></thead><tbody>{campaigns.map(c => <tr key={c.id}><td><strong>{c.name}</strong></td><td>{c.language}</td><td><Status value={c.status}/></td><td>{shortDate(c.created_at)}</td><td><button className="row-action" onClick={onOpen}>Open ↗</button></td></tr>)}</tbody></table></div> : <Empty title="Your campaigns will appear here" detail="Create a campaign or load the sample workspace."/> }
function CallTable({calls,onOpen}: {calls:Call[];onOpen:(call:Call)=>void}) { return <div className="table-scroll"><table><thead><tr><th>SESSION</th><th>DIRECTION</th><th>LANGUAGE</th><th>OUTCOME</th><th>STATUS</th><th>STARTED</th><th/></tr></thead><tbody>{calls.map(c => <tr key={c.id}><td><strong>{c.id.slice(0,8)}</strong></td><td>{titleCase(c.direction)}</td><td>{c.language}</td><td>{titleCase(c.outcome)}</td><td><Status value={c.status}/></td><td>{shortDate(c.created_at)}</td><td><button className="row-action" onClick={() => onOpen(c)}>Details ↗</button></td></tr>)}</tbody></table></div> }
export default App
