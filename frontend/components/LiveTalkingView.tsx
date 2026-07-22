'use client'

import { useEffect, useRef, useState } from 'react'
import { Loader2 } from 'lucide-react'

interface LiveTalkingViewProps {
  /** Base URL of the LiveTalking server, e.g. https://pod-8010.proxy.runpod.net */
  serverUrl: string
  /** Called once the WebRTC session is up, with LiveTalking's sessionid —
   *  the chat backend needs it to route speakable text to this stream. */
  onSessionId: (sessionid: string) => void
  muted: boolean
  /** 'cover' crops to fill (default; fine for matched aspect ratios).
   *  'contain' letterboxes — full-screen kiosk with a portrait source
   *  otherwise crops down to just the face. */
  objectFit?: 'cover' | 'contain'
}

/**
 * Continuous-stream avatar view (engine v2).
 *
 * Negotiates a recv-only WebRTC session with a LiveTalking server and plays
 * the stream: real-time lip-sync while the avatar speaks, natural idle
 * motion while it listens. Replaces the chunked <video> player entirely —
 * there is no per-sentence loading, so no frozen gaps.
 */
export default function LiveTalkingView({ serverUrl, onSessionId, muted, objectFit = 'cover' }: LiveTalkingViewProps) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const pcRef = useRef<RTCPeerConnection | null>(null)
  const [status, setStatus] = useState<'connecting' | 'streaming' | 'failed'>('connecting')

  useEffect(() => {
    let cancelled = false

    const pc = new RTCPeerConnection({
      iceServers: [{ urls: ['stun:stun.l.google.com:19302'] }],
    })
    pcRef.current = pc

    pc.addTransceiver('video', { direction: 'recvonly' })
    pc.addTransceiver('audio', { direction: 'recvonly' })

    pc.ontrack = (event) => {
      if (videoRef.current && event.streams[0]) {
        videoRef.current.srcObject = event.streams[0]
      }
    }
    pc.onconnectionstatechange = () => {
      if (cancelled) return
      if (pc.connectionState === 'connected') setStatus('streaming')
      if (pc.connectionState === 'failed') setStatus('failed')
    }

    ;(async () => {
      try {
        const offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        // Wait for ICE gathering like LiveTalking's own client does
        await new Promise<void>((resolve) => {
          if (pc.iceGatheringState === 'complete') return resolve()
          const check = () => {
            if (pc.iceGatheringState === 'complete') {
              pc.removeEventListener('icegatheringstatechange', check)
              resolve()
            }
          }
          pc.addEventListener('icegatheringstatechange', check)
          setTimeout(resolve, 3000) // don't hang on slow gathering
        })

        const local = pc.localDescription!
        const resp = await fetch(`${serverUrl.replace(/\/$/, '')}/offer`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sdp: local.sdp, type: local.type }),
        })
        if (!resp.ok) throw new Error(`offer failed: ${resp.status}`)
        const answer = await resp.json()
        if (cancelled) return
        await pc.setRemoteDescription(
          new RTCSessionDescription({ sdp: answer.sdp, type: answer.type })
        )
        onSessionId(String(answer.sessionid))
      } catch (e) {
        console.error('LiveTalking connect failed:', e)
        if (!cancelled) setStatus('failed')
      }
    })()

    return () => {
      cancelled = true
      pc.close()
      pcRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverUrl])

  return (
    <div className="absolute inset-0">
      <video
        ref={videoRef}
        className={`w-full h-full ${objectFit === 'contain' ? 'object-contain' : 'object-cover'}`}
        autoPlay
        playsInline
        muted={muted}
      />
      {status !== 'streaming' && (
        <div className="absolute inset-0 bg-surface-950/80 flex flex-col items-center justify-center gap-3">
          {status === 'connecting' ? (
            <>
              <Loader2 size={24} className="animate-spin text-primary-400" />
              <p className="text-sm text-gray-400">Connecting avatar stream…</p>
            </>
          ) : (
            <p className="text-sm text-red-400">
              Avatar stream failed — check the LiveTalking server.
            </p>
          )}
        </div>
      )}
    </div>
  )
}
