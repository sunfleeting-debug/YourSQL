/** 登录页：只负责认证表单与登录前数据库入口。 */

import { useState } from 'react'
import type { FormEvent } from 'react'
import { Database, FolderOpen, RefreshCw } from 'lucide-react'
import { api, errorMessage } from '../../api'
import type { SessionInfo } from '../../types/common'

export interface LoginProps {
  booting: boolean
  online: boolean
  activeDatabase: string | null
  databasePickerBusy: boolean
  onOpenDatabasePicker: () => void
  onLogin: (value: SessionInfo) => void
}

export default function Login({ booting, online, activeDatabase, databasePickerBusy, onOpenDatabasePicker, onLogin }: LoginProps) {
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (databasePickerBusy) {
      setError('请等待数据库切换完成。')
      return
    }
    setBusy(true)
    setError('')
    try {
      const session = await api<SessionInfo>('/api/auth/login', { username, password })
      setPassword('')
      onLogin(session)
    } catch (error) {
      setError(errorMessage(error))
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="login-surface">
      <div className="login-form">
        <div className="login-icon">
          <Database size={28} />
        </div>
        <h1>连接到 YourSQL</h1>
        {booting ? (
          <div className="empty-state">
            <RefreshCw className="spin" />
            正在恢复会话…
          </div>
        ) : (
          <>
            <div className="login-database-picker">
              <div>
                <span>目标数据库</span>
                <strong title={activeDatabase ?? undefined}>{activeDatabase ?? '尚未选择'}</strong>
              </div>
              <button type="button" onClick={onOpenDatabasePicker} disabled={!online || databasePickerBusy} title="选择数据库文件">
                <FolderOpen size={15} />
                {databasePickerBusy ? '读取中…' : '选择'}
              </button>
            </div>
            <form onSubmit={submit}>
              <label>
                用户名
                <input autoComplete="username" value={username} onChange={event => setUsername(event.target.value)} maxLength={128} required />
              </label>
              <label>
                密码
                <input
                  autoFocus
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={event => setPassword(event.target.value)}
                  maxLength={1024}
                  required
                />
              </label>
              {error && (
                <div className="inline-error" role="alert">
                  {error}
                </div>
              )}
              <button className="primary" type="submit" disabled={busy || databasePickerBusy}>
                {busy ? <RefreshCw size={15} className="spin" /> : <Database size={15} />} {busy ? '正在连接…' : '连接数据库'}
              </button>
            </form>
          </>
        )}
        {!online && <p className="error-text">后端未连接，请先启动 python -m yoursql.web。</p>}
      </div>
    </main>
  )
}
