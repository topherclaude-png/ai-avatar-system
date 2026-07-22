'use client'

/**
 * Kiosk mode — the lobby presentation.
 *
 * Full-screen streaming avatar, no navigation, no forms, no chrome. One
 * touch to begin (browsers require a gesture for mic access), then fully
 * hands-free: talk, get answered, QR overlays appear when tickets come up.
 *
 * Prereq: sign in once in the normal app on this browser (the kiosk reuses
 * the stored token), and open /kiosk?avatar=<name-or-id> (defaults to the
 * avatar named "Sam", else the newest one).
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { api, buildSessionWsUrl } from '@/lib/api'
import LiveTalkingView from '@/components/LiveTalkingView'
import QrOverlay, { type QrPayment } from '@/components/QrOverlay'
import type { WsMessage } from '@/lib/types'

const LIVETALKING_URL = process.env.NEXT_PUBLIC_LIVETALKING_URL || ''

function float32ToWav16(samples: Float32Array, sampleRate = 16000): ArrayBuffer {
  const buf = new ArrayBuffer(44 + samples.length * 2)
  const v = new DataView(buf)
  const w = (o: number, s: string) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)) }
  w(0, 'RIFF'); v.setUint32(4, 36 + samples.length * 2, true); w(8, 'WAVE')
  w(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true)
  v.setUint32(24, sampleRate, true); v.setUint32(28, sampleRate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true)
  w(36, 'data'); v.setUint32(40, samples.length * 2, true)
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]))
    v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true)
  }
  return buf
}

type Phase = 'splash' | 'connecting' | 'live' | 'error'

export default function KioskPage() {
  const [phase, setPhase] = useState<Phase>('splash')
  const [errorMsg, setErrorMsg] = useState('')
  const [listening, setListening] = useState(false)   // VAD hears speech now
  const [thinking, setThinking] = useState(false)
  const [userLine, setUserLine] = useState('')        // last thing the guest said
  const [answerLine, setAnswerLine] = useState('')    // streaming answer text
  const [qrPayment, setQrPayment] = useState<QrPayment | null>(null)

  const wsRef = useRef<WebSocket | null>(null)
  const vadRef = useRef<{ destroy: () => void } | null>(null)
  const ltSidRef = useRef<string | null>(null)
  const sessionRef = useRef<string | null>(null)

  const handleWs = useCallback((data: WsMessage) => {
    switch (data.type) {
      case 'token':
        setThinking(false)
        setAnswerLine(prev => prev + data.token)
        break
      case 'transcription':
        setUserLine(data.text)
        setAnswerLine('')
        setThinking(true)
        break
      case 'message':
        setThinking(false)
        setAnswerLine(data.content)
        break
      case 'interrupted':
        setThinking(false)
        break
      case 'tool_call':
        if (data.name === 'show_payment_qr' && typeof data.result?.payment_url === 'string') {
          setQrPayment({
            paymentUrl: data.result.payment_url,
            price: typeof data.result.price === 'number' ? data.result.price : undefined,
          })
        }
        break
      case 'error':
        // Kiosk stays quiet about transient errors; log for the operator.
        console.warn('kiosk ws error:', data.message)
        setThinking(false)
        break
    }
  }, [])

  const begin = useCallback(async () => {
    setPhase('connecting')
    try {
      if (!api.getToken()) {
        throw new Error('Not signed in — open the main app once and log in, then reload /kiosk.')
      }

      // Resolve the avatar: ?avatar=<name-or-id>, else "Sam", else newest.
      const params = new URLSearchParams(window.location.search)
      const want = (params.get('avatar') || 'Sam').toLowerCase()
      const avatars: Array<{ id: string; name: string }> = await api.getAvatars()
      const av =
        avatars.find(a => a.id === params.get('avatar')) ||
        avatars.find(a => a.name.toLowerCase() === want) ||
        avatars[0]
      if (!av) throw new Error('No avatar found — upload one in the main app first.')

      const session = await api.createSession(av.id)
      sessionRef.current = session.id

      // Chat WS
      const ws = new WebSocket(buildSessionWsUrl(session.id))
      wsRef.current = ws
      ws.onmessage = (e) => handleWs(JSON.parse(e.data))
      ws.onclose = () => setPhase(p => (p === 'live' ? 'error' : p))
      await new Promise<void>((resolve, reject) => {
        ws.onopen = () => resolve()
        ws.onerror = () => reject(new Error('Could not reach the avatar service.'))
      })
      if (ltSidRef.current) {
        ws.send(JSON.stringify({ type: 'set_livetalk_session', sessionid: ltSidRef.current }))
      }

      // Hands-free mic (the tap that got us here satisfies the gesture rule)
      const { MicVAD, utils } = await import('@ricky0123/vad-web')
      // Kiosk = open speakers + open mic. Without explicit AEC the avatar's
      // own voice trips the VAD and barge-ins every reply mid-sentence, so
      // we open the mic ourselves with echo cancellation forced on.
      const micStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      })
      const vad = await MicVAD.new({
        baseAssetPath: '/vad/',
        onnxWASMBasePath: '/vad/',
        stream: micStream,
        positiveSpeechThreshold: 0.65, // stricter than default — ignore playback bleed
        minSpeechFrames: 8, // ~250ms of sustained speech before it counts
        onSpeechStart: () => {
          setListening(true)
          const sock = wsRef.current
          if (sock && sock.readyState === WebSocket.OPEN) {
            sock.send(JSON.stringify({ type: 'stop' }))
          }
        },
        onSpeechEnd: (audio: Float32Array) => {
          setListening(false)
          if (audio.length < 16000 * 0.3) return
          const sock = wsRef.current
          if (!sock || sock.readyState !== WebSocket.OPEN) return
          setQrPayment(null)
          sock.send(JSON.stringify({
            type: 'audio',
            audio: utils.arrayBufferToBase64(float32ToWav16(audio)),
          }))
          setThinking(true)
        },
      })
      vad.start()
      vadRef.current = vad
      setPhase('live')
    } catch (e) {
      setErrorMsg(e instanceof Error ? e.message : 'Something went wrong.')
      setPhase('error')
    }
  }, [handleWs])

  useEffect(() => () => {
    vadRef.current?.destroy()
    wsRef.current?.close()
  }, [])

  return (
    <div className="fixed inset-0 bg-black overflow-hidden select-none">
      {/* Stream fills the screen once live (mounted from `connecting` so the
          WebRTC session is often up before the splash fades) */}
      {phase !== 'splash' && LIVETALKING_URL && (
        <LiveTalkingView
          serverUrl={LIVETALKING_URL}
          muted={false}
          onSessionId={(sid) => {
            ltSidRef.current = sid
            const sock = wsRef.current
            if (sock && sock.readyState === WebSocket.OPEN) {
              sock.send(JSON.stringify({ type: 'set_livetalk_session', sessionid: sid }))
            }
          }}
        />
      )}

      {/* Brand mark */}
      <div className="absolute top-6 left-8 z-40 pointer-events-none">
        <span className="text-2xl font-black tracking-tight text-white/90">YOU<span className="text-violet-400">42</span></span>
      </div>

      {/* Listening indicator */}
      {phase === 'live' && (
        <div className="absolute top-7 right-8 z-40 flex items-center gap-2">
          <span
            className={`w-3 h-3 rounded-full ${
              listening ? 'bg-green-400 animate-ping' : thinking ? 'bg-amber-400 animate-pulse' : 'bg-white/30'
            }`}
          />
          <span className="text-xs text-white/60">
            {listening ? 'Listening…' : thinking ? 'Thinking…' : 'Just talk'}
          </span>
        </div>
      )}

      {/* QR overlay */}
      {qrPayment && (
        <div className="absolute inset-y-0 right-10 z-40 flex items-center">
          <div className="scale-125 origin-right">
            <QrOverlay payment={qrPayment} onDismiss={() => setQrPayment(null)} />
          </div>
        </div>
      )}

      {/* Caption strip */}
      {phase === 'live' && (userLine || answerLine) && (
        <div className="absolute bottom-0 inset-x-0 z-30 pointer-events-none">
          <div className="bg-gradient-to-t from-black/85 via-black/50 to-transparent px-10 pt-16 pb-8">
            {userLine && (
              <p className="text-sm text-violet-300/90 mb-1 max-w-3xl mx-auto text-center italic">“{userLine}”</p>
            )}
            {answerLine && (
              <p className="text-lg text-white/95 max-w-3xl mx-auto text-center leading-relaxed line-clamp-3">
                {answerLine}
              </p>
            )}
          </div>
        </div>
      )}

      {/* Splash */}
      {phase === 'splash' && (
        <button
          onClick={begin}
          className="absolute inset-0 z-50 flex flex-col items-center justify-center gap-6 bg-gradient-to-b from-black via-[#0d0620] to-black"
        >
          <span className="text-6xl font-black tracking-tight text-white">YOU<span className="text-violet-400">42</span></span>
          <span className="text-xl text-white/70">Meet Sam, your virtual host</span>
          <span className="mt-6 px-8 py-4 rounded-full border border-violet-400/50 text-violet-200 text-lg animate-pulse">
            Touch to start talking
          </span>
        </button>
      )}

      {/* Connecting / error states */}
      {phase === 'connecting' && (
        <div className="absolute inset-0 z-50 flex items-center justify-center bg-black/70">
          <span className="text-white/70 text-lg animate-pulse">Waking Sam up…</span>
        </div>
      )}
      {phase === 'error' && (
        <button onClick={() => window.location.reload()} className="absolute inset-0 z-50 flex flex-col items-center justify-center gap-4 bg-black/85">
          <span className="text-white/80 text-lg">Sam stepped away for a moment.</span>
          {errorMsg && <span className="text-white/40 text-sm max-w-md text-center">{errorMsg}</span>}
          <span className="px-6 py-3 rounded-full border border-white/30 text-white/70">Tap to retry</span>
        </button>
      )}
    </div>
  )
}
