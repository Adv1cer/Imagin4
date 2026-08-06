import type { PendingActionState } from '../types/chat'

const ACTION_LABEL: Record<string, string> = {
  poster: 'โปสเตอร์ (Poster)',
  infographic: 'อินโฟกราฟิก (Infographic)',
}

export function PendingActionCard({
  pendingAction,
  onConfirm,
  onCancel,
}: {
  pendingAction: PendingActionState
  onConfirm: () => void
  onCancel: () => void
}) {
  const label = ACTION_LABEL[pendingAction.actionType] ?? pendingAction.actionType
  const isPending = pendingAction.status === 'pending'

  return (
    <div className="mt-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2.5 text-sm">
      <div className="font-medium text-amber-900">
        ยืนยันก่อนสร้าง{label} — มีค่าใช้จ่าย (paid)
      </div>
      <div className="mt-1 text-xs text-amber-800">{pendingAction.normalizedPrompt}</div>
      {pendingAction.exactText.length > 0 && (
        <div className="mt-1 text-xs text-amber-700">
          ข้อความที่ต้องคงไว้: {pendingAction.exactText.join(', ')}
        </div>
      )}

      {isPending && (
        <div className="mt-2 flex items-center gap-2">
          <button
            type="button"
            onClick={onConfirm}
            disabled={pendingAction.busy}
            className="rounded-full bg-amber-600 px-3 py-1 text-xs font-medium text-white hover:bg-amber-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {pendingAction.busy ? 'กำลังดำเนินการ…' : 'ยืนยันและสร้าง (Confirm)'}
          </button>
          <button
            type="button"
            onClick={onCancel}
            disabled={pendingAction.busy}
            className="rounded-full border border-amber-300 px-3 py-1 text-xs font-medium text-amber-800 hover:bg-amber-100 disabled:cursor-not-allowed disabled:opacity-50"
          >
            ยกเลิก (Cancel)
          </button>
        </div>
      )}

      {pendingAction.status === 'cancelled' && (
        <div className="mt-2 text-xs text-amber-700">ยกเลิกคำขอนี้แล้ว</div>
      )}
      {pendingAction.status === 'expired' && (
        <div className="mt-2 text-xs text-amber-700">
          คำขอนี้หมดอายุแล้ว กรุณาพิมพ์คำขอใหม่อีกครั้ง
        </div>
      )}
      {pendingAction.errorMessage && (
        <div className="mt-2 text-xs text-red-600">{pendingAction.errorMessage}</div>
      )}
    </div>
  )
}
