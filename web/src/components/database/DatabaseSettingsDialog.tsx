/** 【前端特供】当前数据库运行参数设置弹窗，不承载数据库核心逻辑。 */

import { forwardRef, useEffect, useState } from 'react'
import { Database, HardDrive, Info, RefreshCw, Settings2, X } from 'lucide-react'
import { api, errorMessage } from '../../api'
import type { PayloadCodecName } from '../../types/common'
import type { ReplacementPolicy, StoragePolicyChange, StorageResizeChange, StorageSnapshot } from '../../types/storage'

/** 【前端特供】数据库设置弹窗的会话与刷新回调。 */
export interface DatabaseSettingsDialogProps {
  database: string
  databasePath?: string
  payloadCodec: PayloadCodecName | null
  visible: boolean
  onChanged?: () => void
  onClose: () => void
}

/** 【前端特供】格式化缓存页数对应的内存占用。 */
function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`
  return `${Math.round(bytes / 1024)} KiB`
}

/** 【前端特供】展示并应用当前数据库的运行参数。 */
const DatabaseSettingsDialog = forwardRef<HTMLDialogElement, DatabaseSettingsDialogProps>(function DatabaseSettingsDialog(
  { database, databasePath, payloadCodec, visible, onChanged, onClose },
  ref
) {
  const [snapshot, setSnapshot] = useState<StorageSnapshot | null>(null)
  const [capacityDraft, setCapacityDraft] = useState('')
  const [policyDraft, setPolicyDraft] = useState<ReplacementPolicy>('lru')
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  useEffect(() => {
    if (!visible) return
    let cancelled = false
    setLoading(true)
    setError('')
    setNotice('')
    api<StorageSnapshot>('/api/storage?limit=1')
      .then(value => {
        if (cancelled) return
        setSnapshot(value)
        setCapacityDraft(String(value.buffer_pool.stats.capacity ?? 0))
        setPolicyDraft(value.buffer_pool.policy)
      })
      .catch(value => {
        if (!cancelled) setError(errorMessage(value))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [visible])

  const currentCapacity = snapshot?.buffer_pool.stats.capacity ?? 0
  const capacity = Number(capacityDraft)
  const capacityValid = Number.isInteger(capacity) && capacity >= 1 && capacity <= 4096
  const capacityChanged = capacityValid && capacity !== currentCapacity
  const pageSize = snapshot?.page_size ?? 0

  async function applyCapacity() {
    if (!capacityValid || !capacityChanged || busy) return
    setBusy(true)
    setError('')
    try {
      const value = await api<StorageResizeChange>('/api/storage/cache/resize', { buffer_pool_size: capacity })
      setSnapshot(current => (current ? { ...current, buffer_pool: value.buffer_pool } : current))
      setCapacityDraft(String(value.capacity))
      setNotice(value.changed ? `缓存页数已调整为 ${value.capacity} 页。` : '缓存页数未变化。')
      onChanged?.()
    } catch (value) {
      setError(errorMessage(value))
    } finally {
      setBusy(false)
    }
  }

  async function applyPolicy() {
    if (!snapshot || policyDraft === snapshot.buffer_pool.policy || busy) return
    setBusy(true)
    setError('')
    try {
      const value = await api<StoragePolicyChange>('/api/storage/cache/policy', { replacement_policy: policyDraft })
      setSnapshot(current => (current ? { ...current, buffer_pool: value.buffer_pool } : current))
      setPolicyDraft(value.replacement_policy)
      setNotice(value.changed ? `淘汰策略已切换为 ${value.replacement_policy.toUpperCase()}。` : '淘汰策略未变化。')
      onChanged?.()
    } catch (value) {
      setError(errorMessage(value))
    } finally {
      setBusy(false)
    }
  }

  return (
    <dialog ref={ref} className="database-dialog database-settings-dialog" onClose={onClose}>
      <div className="panel-heading">
        <strong>
          <Settings2 size={18} />
          数据库设置
        </strong>
        <button className="icon-button" aria-label="关闭数据库设置" onClick={onClose}>
          <X size={18} />
        </button>
      </div>

      <div className="database-settings-meta">
        <div>
          <Database size={14} />
          <strong>{database}</strong>
        </div>
        <code title={databasePath ?? database}>{databasePath ?? '当前会话数据库'}</code>
      </div>

      {loading && (
        <div className="database-settings-loading">
          <RefreshCw size={14} />
          读取当前参数…
        </div>
      )}
      {error && (
        <p className="database-settings-error" role="alert">
          {error}
        </p>
      )}

      {snapshot && !loading && (
        <div className="database-settings-body">
          <div className="database-settings-facts">
            <div>
              <span>页大小</span>
              <strong>{pageSize.toLocaleString()} B</strong>
              <small>数据库格式参数，只读</small>
            </div>
            <div>
              <span>Payload 编码</span>
              <strong>{payloadCodec === 'manual' ? '手写二进制' : payloadCodec === 'json' ? 'JSON' : '—'}</strong>
              <small>记录在 superblock 中，只读</small>
            </div>
            <div>
              <span>缓存占用估算</span>
              <strong>{formatBytes(currentCapacity * pageSize)}</strong>
              <small>按缓存页数 × 页大小</small>
            </div>
          </div>

          <div className="database-setting-control">
            <div className="database-setting-label">
              <HardDrive size={14} />
              <div>
                <strong>缓存页数</strong>
                <small>可在线调整，缩容时只淘汰未固定页</small>
              </div>
            </div>
            <div className="database-setting-editor">
              <input
                type="number"
                min="1"
                max="4096"
                step="1"
                value={capacityDraft}
                aria-label="缓存页数"
                onChange={event => setCapacityDraft(event.target.value)}
              />
              <span>{capacityValid ? formatBytes(capacity * pageSize) : '1–4096 页'}</span>
              <button onClick={() => void applyCapacity()} disabled={!capacityChanged || !capacityValid || busy}>
                应用
              </button>
            </div>
          </div>

          <div className="database-setting-control">
            <div className="database-setting-label">
              <Settings2 size={14} />
              <div>
                <strong>淘汰策略</strong>
                <small>影响下一次缓存页淘汰</small>
              </div>
            </div>
            <div className="database-setting-editor">
              <select value={policyDraft} aria-label="缓存淘汰策略" onChange={event => setPolicyDraft(event.target.value as ReplacementPolicy)}>
                <option value="lru">LRU · 最近最少使用</option>
                <option value="fifo">FIFO · 先进先出</option>
              </select>
              <button onClick={() => void applyPolicy()} disabled={policyDraft === snapshot.buffer_pool.policy || busy}>
                应用
              </button>
            </div>
          </div>

          <p className="database-settings-note">
            <Info size={13} />
            设置只作用于当前服务进程；正在执行查询时不允许修改。重启服务后请按配置重新启动。
          </p>
          {notice && (
            <p className="database-settings-notice" role="status">
              {notice}
            </p>
          )}
        </div>
      )}
    </dialog>
  )
})

export default DatabaseSettingsDialog
