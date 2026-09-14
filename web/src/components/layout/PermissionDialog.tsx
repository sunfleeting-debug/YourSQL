/** 当前会话权限弹窗。 */

import { forwardRef } from 'react'
import { ShieldCheck, X } from 'lucide-react'
import type { SessionInfo } from '../../types/common'

export interface PermissionDialogProps {
  session: SessionInfo
  onClose: () => void
}

const PermissionDialog = forwardRef<HTMLDialogElement, PermissionDialogProps>(function PermissionDialog({ session, onClose }, ref) {
  return (
    <dialog ref={ref} className="permission-dialog">
      <div className="panel-heading">
        <strong>
          <ShieldCheck size={18} />
          当前用户权限
        </strong>
        <button className="icon-button" aria-label="关闭权限" onClick={onClose}>
          <X size={18} />
        </button>
      </div>
      <p>
        {session.user} · {session.roles.join(', ') || '无角色'}
      </p>
      <div className="privileges">
        {session.permissions.length ? session.permissions.map(permission => <code key={permission}>{permission}</code>) : <span>暂无权限</span>}
      </div>
      <p className="hint">会话剩余约 {Math.ceil(session.session_expires_in / 60)} 分钟；重启服务需重新登录。</p>
    </dialog>
  )
})

export default PermissionDialog
