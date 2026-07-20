'use client'

import QRCode from 'react-qr-code'
import { X, Ticket } from 'lucide-react'

export interface QrPayment {
  paymentUrl: string
  eventId?: string
  price?: number
}

interface QrOverlayProps {
  payment: QrPayment
  onDismiss: () => void
}

/**
 * Payment QR overlay — rendered over the avatar video when the LLM fires a
 * `show_payment_qr` tool call. The guest scans with their phone; the URL is
 * a stub in the POC (checkout integration is post-POC, backend-only swap).
 */
export default function QrOverlay({ payment, onDismiss }: QrOverlayProps) {
  return (
    <div
      className="absolute top-3 left-3 z-30 glass-card rounded-2xl border border-white/15 p-4
                 flex flex-col items-center gap-3 animate-slide-up shadow-2xl"
      role="dialog"
      aria-label="Payment QR code"
    >
      <div className="flex items-center justify-between w-full gap-6">
        <div className="flex items-center gap-2">
          <Ticket size={14} className="text-primary-400" />
          <span className="text-xs font-semibold text-gray-200">Scan to pay</span>
        </div>
        <button
          onClick={onDismiss}
          className="btn-icon !p-1"
          title="Dismiss"
          aria-label="Dismiss payment QR code"
        >
          <X size={13} />
        </button>
      </div>

      {/* QR codes need a light, high-contrast background to scan reliably */}
      <div className="bg-white p-2.5 rounded-xl">
        <QRCode value={payment.paymentUrl} size={132} />
      </div>

      {payment.price !== undefined && (
        <span className="text-sm font-bold text-white">
          ${Number(payment.price).toFixed(2)}
        </span>
      )}
      <span className="text-[10px] text-gray-400">Point your phone camera at the code</span>
    </div>
  )
}
