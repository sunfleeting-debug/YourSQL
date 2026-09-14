/** 应用页头：连接状态、工作区模式和当前用户操作。 */

import { Activity, Braces, Database, FolderOpen, HardDrive, LogOut, ShieldCheck, UserRound } from 'lucide-react'
import type { PayloadCodecName, SessionInfo } from '../../types/common'
import type { WorkspaceMode } from '../../app/types'
import { statusDotClassName } from '../../view-classes'

export interface AppHeaderProps {
  session: SessionInfo | null
  online: boolean
  running: boolean
  databasePickerBusy: boolean
  payloadCodec: PayloadCodecName | null
  workspaceMode: WorkspaceMode
  modePending: boolean
  onOpenDatabasePicker: () => void
  onSwitchWorkspaceMode: (mode: WorkspaceMode) => void
  onOpenPermissions: () => void
  onLogout: () => void
}

export default function AppHeader({
  session,
  online,
  running,
  databasePickerBusy,
  payloadCodec,
  workspaceMode,
  modePending,
  onOpenDatabasePicker,
  onSwitchWorkspaceMode,
  onOpenPermissions,
  onLogout
}: AppHeaderProps) {
  return (
    <header className="app-header">
      <div className="brand">
        <Database size={24} />
        <strong>YourSQL</strong>
      </div>
      <div className="connection">
        <span className={statusDotClassName(online)} />
        <span title={session?.database_path ?? undefined}>
          {online ? (session ? `已连接 · ${session.database_path ?? session.database}` : '已连接') : '服务不可用'}
        </span>
        {session && payloadCodec && (
          <code title="该数据库的 payload 编码记录在 superblock 中">payload:{payloadCodec === 'manual' ? '手写' : 'JSON'}</code>
        )}
        {session && (
          <button className="database-switch" onClick={onOpenDatabasePicker} disabled={running || databasePickerBusy} title="选择或新建数据库">
            <FolderOpen size={13} />
            <span>数据库</span>
          </button>
        )}
        <code>{window.location.host}</code>
      </div>
      {session && (
        <nav className="header-workspace-nav" aria-label="工作区模式">
          <div className="mode-switch" role="tablist" aria-label="工作区模式">
            <button
              role="tab"
              aria-selected={workspaceMode === 'sql'}
              className={workspaceMode === 'sql' ? 'active' : ''}
              onClick={() => onSwitchWorkspaceMode('sql')}
              disabled={modePending}
            >
              <Braces size={13} />
              SQL 工作台
            </button>
            <button
              role="tab"
              aria-selected={workspaceMode === 'storage'}
              className={workspaceMode === 'storage' ? 'active' : ''}
              onClick={() => onSwitchWorkspaceMode('storage')}
              disabled={modePending}
            >
              <HardDrive size={13} />
              存储检查
            </button>
            <button
              role="tab"
              aria-selected={workspaceMode === 'monitor'}
              className={workspaceMode === 'monitor' ? 'active' : ''}
              onClick={() => onSwitchWorkspaceMode('monitor')}
              disabled={modePending}
            >
              <Activity size={13} />
              性能监控
            </button>
          </div>
        </nav>
      )}
      <div className="header-spacer" />
      {session && (
        <>
          <button className="user-button" onClick={onOpenPermissions}>
            <span className="avatar">
              <UserRound size={15} />
            </span>
            {session.user}
            <ShieldCheck size={13} />
          </button>
          <button className="subtle logout-button" onClick={onLogout}>
            <LogOut size={14} />
            退出
          </button>
        </>
      )}
    </header>
  )
}
