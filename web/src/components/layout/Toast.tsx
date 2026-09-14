/** 非阻塞操作反馈；由 App 控制自动消失时机。 */

import { CheckCircle2, CircleAlert, X } from 'lucide-react'
import type { ToastState } from '../../app/types'
import { classNames } from '../../view-classes'

export interface ToastProps {
  toast: ToastState
  onClose: () => void
}

export default function Toast({ toast, onClose }: ToastProps) {
  return (
    <div className={classNames('toast', toast.error && 'error')} role={toast.error ? 'alert' : 'status'}>
      {toast.error ? <CircleAlert size={17} /> : <CheckCircle2 size={17} />}
      <span>{toast.message}</span>
      <button aria-label="关闭消息" onClick={onClose}>
        <X size={14} />
      </button>
    </div>
  )
}
