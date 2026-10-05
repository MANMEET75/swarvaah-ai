import { useEffect, useRef, useState } from 'react'

type LabCall = {
  id: string
  status: string
  language: string
  outcome: string | null
  events: { kind: string; text: string; created_at: string }[]
}

const API = import.meta.env.VITE_API_URL || ''

async function api<T>(path: string, options: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, options)
  if (!response.ok) {
    let detail = `Request failed (${response.status})`
    try { detail = (await response.json()).detail || detail } catch { /* no JSON body */ }
    throw new Error(detail)
  }
  return response.json() as Promise<T>
}

function supportedMime(): string {
  if (typeof MediaRecorder === 'undefined') return ''
  return ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus']
    .find(type => MediaRecorder.isTypeSupported(type)) || ''
}

export default function VoiceLab({ onSessionStarted }: { onSessionStarted: () => void }) {
  const [form, setForm] = useState({ name: 'Ananya', reminder_label: 'your appointment', reminder_at: 'tomorrow at 10:00 AM', language: 'hi-IN' })
  const [call, setCall] = useState<LabCall | null>(null)
  const [draft, setDraft] = useState('')
  const [phase, setPhase] = useState<'idle' | 'starting' | 'recording' | 'transcribing' | 'processing' | 'speaking'>('idle')
  const [error, setError] = useState('')
  const [hint, setHint] = useState('Start a test, then speak or type a reply.')
  const [aiEnabled, setAiEnabled] = useState(() => window.localStorage.getItem('swarvaah.voiceLab.aiEnabled') === 'true')
  const recorder = useRef<MediaRecorder | null>(null)
  const stream = useRef<MediaStream | null>(null)
  const audio = useRef<HTMLAudioElement | null>(null)
  const audioUrl = useRef<string | null>(null)
  const playbackGeneration = useRef(0)
  const recordTimer = useRef<number | null>(null)
  const vadFrame = useRef<number | null>(null)
  const vadContext = useRef<AudioContext | null>(null)
  const bottom = useRef<HTMLDivElement | null>(null)

  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth' }) }, [call?.events.length])
  useEffect(() => {
    void api<{ conversation_mode: string }>('/health', { method: 'GET' })
      .then(status => {
        const saved = window.localStorage.getItem('swarvaah.voiceLab.aiEnabled')
        if (saved === null) setAiEnabled(status.conversation_mode === 'sarvam')
      })
      .catch(() => { /* API errors are surfaced when starting a session */ })
  }, [])
  const toggleAi = () => {
    const next = !aiEnabled
    setAiEnabled(next)
    window.localStorage.setItem('swarvaah.voiceLab.aiEnabled', String(next))
    setHint(next ? 'AI conversation will answer the next open-ended reply.' : 'Deterministic workflow will answer the next reply.')
  }
  useEffect(() => () => {
    if (recordTimer.current) window.clearTimeout(recordTimer.current)
    if (vadFrame.current) window.cancelAnimationFrame(vadFrame.current)
    if (vadContext.current) void vadContext.current.close()
    if (recorder.current) recorder.current.onstop = null
    if (recorder.current?.state === 'recording') recorder.current.stop()
    stream.current?.getTracks().forEach(track => track.stop())
    audio.current?.pause()
    if (audioUrl.current) URL.revokeObjectURL(audioUrl.current)
    playbackGeneration.current += 1
  }, [])

  const stopPlayback = () => {
    playbackGeneration.current += 1
    audio.current?.pause()
    audio.current = null
    if (audioUrl.current) URL.revokeObjectURL(audioUrl.current)
    audioUrl.current = null
    setPhase('idle')
  }

  const play = async (text: string, language: string) => {
    stopPlayback()
    const generation = playbackGeneration.current
    setError('')
    setPhase('speaking')
    try {
      const response = await fetch(`${API}/v1/voice-lab/speak`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, language }),
      })
      if (!response.ok) {
        let detail = `Voice playback failed (${response.status})`
        try { detail = (await response.json()).detail || detail } catch { /* no JSON body */ }
        throw new Error(detail)
      }
      const blob = await response.blob()
      if (generation !== playbackGeneration.current) return
      const url = URL.createObjectURL(blob)
      audioUrl.current = url
      const player = new Audio(url)
      audio.current = player
      player.onended = () => { if (generation === playbackGeneration.current) stopPlayback() }
      player.onerror = () => {
        if (generation === playbackGeneration.current) {
          stopPlayback()
          setError('Audio playback failed. Check your output device.')
        }
      }
      await player.play()
      setHint('Playing Sarvam Bulbul v3 voice.')
    } catch (exc) {
      if (generation !== playbackGeneration.current) return
      stopPlayback()
      setError(exc instanceof Error ? exc.message : 'Voice playback failed')
      setHint('The text conversation is still available below.')
    }
  }

  const start = async () => {
    stopPlayback()
    setError('')
    setPhase('starting')
    try {
      const session = await api<LabCall>('/v1/test-calls', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(form),
      })
      setCall(session)
      setDraft('')
      onSessionStarted()
      setHint('Session ready. Press the microphone or type your reply.')
      setPhase('idle')
      const greeting = session.events.find(event => event.kind === 'assistant')
      if (greeting) await play(greeting.text, session.language)
    } catch (exc) {
      setPhase('idle')
      setError(exc instanceof Error ? exc.message : 'Could not start test')
    }
  }

  const send = async () => {
    if (!call || !draft.trim() || call.status === 'ended' || phase === 'processing') return
    const text = draft.trim()
    stopPlayback()
    setError('')
    setPhase('processing')
    try {
      const result = await api<{ call: LabCall; reply: string; model: string | null }>(`/v1/test-calls/${call.id}/turn`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, conversation_mode: aiEnabled ? 'sarvam' : 'deterministic' }),
      })
      setCall(result.call)
      setDraft('')
      setPhase('idle')
      setHint(result.call.status === 'ended' ? 'Conversation completed. Start a new test to try another path.'
        : result.model ? 'Sarvam AI replied using the conversation history.'
        : aiEnabled ? 'The bounded workflow replied; continue or try an open-ended question.'
        : 'Reply recorded. Continue the conversation.')
      await play(result.reply, call.language)
    } catch (exc) {
      setPhase('idle')
      setError(exc instanceof Error ? exc.message : 'Could not send reply')
    }
  }

  const stopRecording = () => {
    if (recordTimer.current) window.clearTimeout(recordTimer.current)
    if (vadFrame.current) window.cancelAnimationFrame(vadFrame.current)
    vadFrame.current = null
    if (vadContext.current) void vadContext.current.close()
    vadContext.current = null
    if (recorder.current?.state === 'recording') recorder.current.stop()
  }

  const watchForSilence = (mic: MediaStream) => {
    if (!window.AudioContext) return
    const context = new AudioContext()
    vadContext.current = context
    const analyser = context.createAnalyser()
    analyser.fftSize = 2048
    context.createMediaStreamSource(mic).connect(analyser)
    const samples = new Uint8Array(analyser.fftSize)
    let voiceSince = 0
    let heardSpeech = false
    let silentSince = 0
    const inspect = () => {
      if (recorder.current?.state !== 'recording') return
      analyser.getByteTimeDomainData(samples)
      let energy = 0
      for (const sample of samples) energy += ((sample - 128) / 128) ** 2
      const speaking = Math.sqrt(energy / samples.length) > 0.02
      const now = performance.now()
      if (speaking) {
        if (!voiceSince) voiceSince = now
        if (now - voiceSince >= 180) heardSpeech = true
        silentSince = 0
      } else {
        voiceSince = 0
        if (heardSpeech) {
          if (!silentSince) silentSince = now
          if (now - silentSince >= 900) { stopRecording(); return }
        }
      }
      vadFrame.current = window.requestAnimationFrame(inspect)
    }
    vadFrame.current = window.requestAnimationFrame(inspect)
  }

  const record = async () => {
    if (phase === 'recording') { stopRecording(); return }
    if (!call || call.status === 'ended') return
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      setError('Microphone recording is unavailable in this browser. Type your reply instead.')
      return
    }
    stopPlayback()
    setError('')
    try {
      const mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
      stream.current = mic
      const mimeType = supportedMime()
      const capture = new MediaRecorder(mic, mimeType ? { mimeType } : undefined)
      recorder.current = capture
      const chunks: BlobPart[] = []
      capture.ondataavailable = event => { if (event.data.size) chunks.push(event.data) }
      capture.onerror = () => { setError('Recording failed. Type your reply instead.'); stopRecording() }
      capture.onstop = async () => {
        stopRecording()
        mic.getTracks().forEach(track => track.stop())
        stream.current = null
        if (!chunks.length) { setPhase('idle'); setError('No audio was captured. Try again or type your reply.'); return }
        setPhase('transcribing')
        const blob = new Blob(chunks, { type: capture.mimeType || 'audio/webm' })
        const data = new FormData()
        data.set('file', blob, 'reply')
        try {
          const result = await api<{ text: string }>('/v1/voice-lab/transcribe', { method: 'POST', body: data })
          setDraft(result.text)
          setHint(result.text ? 'Review the transcript, then press Send.' : 'No speech detected. Try again or type your reply.')
        } catch (exc) {
          setError(exc instanceof Error ? exc.message : 'Transcription failed; type your reply instead')
        } finally { setPhase('idle') }
      }
      capture.start()
      try { watchForSilence(mic) } catch { /* manual stop and 15-second limit remain available */ }
      setPhase('recording')
      setHint('Recording… pauses after you finish speaking. Press Stop anytime; maximum 15 seconds.')
      recordTimer.current = window.setTimeout(stopRecording, 15000)
    } catch (exc) {
      stream.current?.getTracks().forEach(track => track.stop())
      stream.current = null
      setPhase('idle')
      setError(exc instanceof Error && exc.name === 'NotAllowedError'
        ? 'Microphone permission was denied. Allow access in your browser or type a reply.'
        : 'Could not open the microphone. Type your reply instead.')
    }
  }

  return <div className="voice-lab-page">
    <div className="page-head"><div><div className="eyebrow">VOICE STUDIO / LOCAL TEST</div><h1>Voice lab</h1><p>Rehearse a service reminder with Sarvam speech and a safe, synthetic conversation.</p></div><span className="lab-mode-pill">● No phone call placed</span></div>
    <div className="lab-grid">
      <div className="panel lab-setup"><div className="panel-title"><div><h3>Test scenario</h3><p>Set the context before starting a conversation</p></div><span className="lab-step">01 / SETUP</span></div>
        <div className="form-grid">
          <label className="field full">Recipient name<input value={form.name} maxLength={100} onChange={e => setForm({ ...form, name: e.target.value })}/></label>
          <label className="field full">Reminder<input value={form.reminder_label} maxLength={160} onChange={e => setForm({ ...form, reminder_label: e.target.value })}/></label>
          <label className="field full">When<input value={form.reminder_at} maxLength={160} onChange={e => setForm({ ...form, reminder_at: e.target.value })}/></label>
          <label className="field full">Language<select value={form.language} onChange={e => setForm({ ...form, language: e.target.value })}><option value="hi-IN">Hindi / Hinglish</option><option value="en-IN">English</option></select></label>
        </div>
        <button className="primary-button wide" disabled={!['idle', 'speaking'].includes(phase) || !form.name.trim() || !form.reminder_label.trim()} onClick={() => void start()}>{phase === 'starting' ? 'Starting…' : call ? 'Start new conversation' : 'Start conversation'} <span>↗</span></button>
        <div className="lab-provider"><span className="lab-provider-dot"/><div className="lab-provider-copy"><strong>AI conversation {aiEnabled ? 'enabled' : 'off'}</strong><small>{aiEnabled ? 'Sarvam 105B answers open questions' : 'Bounded reminder workflow answers'}</small></div><button type="button" role="switch" aria-label="Enable AI conversation" aria-checked={aiEnabled} className={`lab-ai-switch ${aiEnabled ? 'on' : ''}`} onClick={toggleAi} disabled={phase === 'processing'}><span/></button></div>
        <p className="lab-switch-note">Applies to your next reply. Saaras transcription and Bulbul playback work in either mode.</p>
        <p className="lab-note">Use fictional details. Microphone audio is sent to Sarvam for transcription; it is not saved in the call record. You can always type instead.</p>
      </div>
      <div className="panel conversation-panel">
        <div className="panel-title conversation-header"><div><h3>Conversation</h3><p>{call ? `Session ${call.id.slice(0, 8)} · ${call.language}` : 'Ready to begin'}</p></div><span className={`lab-session-state ${call?.status === 'ended' ? 'ended' : ''}`}>{call ? call.status : 'Not started'}</span></div>
        <div className="messages" aria-live="polite">
          {call?.events?.length ? call.events.filter(event => event.kind !== 'action').map((event, index) => <div className={`message ${event.kind}`} key={`${event.created_at}-${index}`}><div className="message-icon">{event.kind === 'assistant' ? '✦' : 'Y'}</div><div className="message-body"><span>{event.kind === 'assistant' ? 'Swarvaah AI' : 'You'}</span><p>{event.text}</p>{event.kind === 'assistant' && <button className="play-link" onClick={() => void play(event.text, call.language)}>▷ Play Sarvam voice</button>}</div></div>) : <div className="conversation-empty"><div className="wave-symbol">◌</div><h3>A conversation starts here</h3><p>Create a test scenario to hear the greeting and try the reminder flow.</p></div>}
          <div ref={bottom}/>
        </div>
        {error && <div className="lab-alert" role="alert">{error}</div>}
        <div className="lab-hint" role="status">{hint}</div>
        <div className="composer"><button type="button" aria-label={phase === 'recording' ? 'Stop recording' : 'Record reply'} className={`mic-button ${phase === 'recording' ? 'listening' : ''}`} onClick={() => void record()} disabled={!call || call.status === 'ended' || ['starting', 'transcribing', 'processing'].includes(phase)}>{phase === 'recording' ? '■' : '●'}</button><input value={draft} onChange={e => setDraft(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') void send() }} placeholder={phase === 'recording' ? 'Listening…' : phase === 'transcribing' ? 'Transcribing…' : 'Type or record your reply'} disabled={!call || call.status === 'ended' || phase === 'processing'}/><button type="button" className="send-button" onClick={() => void send()} disabled={!call || !draft.trim() || call.status === 'ended' || phase === 'processing'}>{phase === 'processing' ? '…' : '➜'}</button></div>
      </div>
    </div>
    <div className="lab-presets"><span>TRY A RESPONSE</span>{['What is this reminder about?', 'Can you explain it?', 'Yes, confirm it', 'Can we reschedule?', 'Please stop calling me'].map(text => <button key={text} onClick={() => setDraft(text)} disabled={!call || call.status === 'ended'}>{text}</button>)}</div>
  </div>
}
