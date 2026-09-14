/** 数据库选择、新建和导入流程；业务请求仍由 App 统一编排。 */

import { forwardRef, useRef, useState } from 'react'
import type { ChangeEvent, FormEvent } from 'react'
import { FilePlus2, FolderOpen, RefreshCw, X } from 'lucide-react'
import type { DatabaseFile, DatabaseFiles } from '../../types/catalog'
import type { DatabaseCreateConfig, DatabaseDialogMode } from '../../app/types'
import { formatFileSize, isPayloadCodec, isReplacementPolicy } from './utils'

export interface DatabasePickerProps {
  files: DatabaseFiles | null
  busy: boolean
  loggedIn: boolean
  mode: DatabaseDialogMode
  onModeChange: (mode: DatabaseDialogMode) => void
  openPath: string
  onOpenPathChange: (path: string) => void
  createPath: string
  onCreatePathChange: (path: string) => void
  createConfig: DatabaseCreateConfig
  onCreateConfigChange: (config: DatabaseCreateConfig) => void
  onClose: () => void
  onSelect: (path: string) => void
  onCreate: (path: string) => void
  onImport: (file: File) => void
}

const DatabasePickerDialog = forwardRef<HTMLDialogElement, DatabasePickerProps>(function DatabasePickerDialog(
  {
    files,
    busy,
    loggedIn,
    mode,
    onModeChange,
    openPath,
    onOpenPathChange,
    createPath,
    onCreatePathChange,
    createConfig,
    onCreateConfigChange,
    onClose,
    onSelect,
    onCreate,
    onImport
  },
  ref
) {
  const fileInput = useRef<HTMLInputElement>(null)
  const [selectedFile, setSelectedFile] = useState<File | null>(null)

  function clearSelectedFile() {
    setSelectedFile(null)
    if (fileInput.current) fileInput.current.value = ''
  }

  function submitOpen(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (selectedFile) {
      const file = selectedFile
      clearSelectedFile()
      onImport(file)
    } else if (openPath.trim()) {
      onSelect(openPath.trim())
    }
  }

  function submitCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (createPath.trim()) onCreate(createPath.trim())
  }

  function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    if (file) {
      setSelectedFile(file)
      onOpenPathChange(file.name)
    }
  }

  function handlePathChange(path: string) {
    clearSelectedFile()
    onOpenPathChange(path)
  }

  function handleExistingSelection(path: string) {
    clearSelectedFile()
    onOpenPathChange(path)
  }

  function handleModeChange(nextMode: DatabaseDialogMode) {
    clearSelectedFile()
    if (nextMode === 'open' && files) onOpenPathChange(files.active_path ?? files.active)
    onModeChange(nextMode)
  }

  function handleClose() {
    clearSelectedFile()
    onClose()
  }

  const effectiveMode = loggedIn ? mode : 'open'
  return (
    <dialog ref={ref} className="database-dialog" aria-labelledby="database-picker-title">
      <div className="panel-heading">
        <strong id="database-picker-title">
          {effectiveMode === 'open' ? (
            <>
              <FolderOpen size={17} />
              选择数据库
            </>
          ) : (
            <>
              <FilePlus2 size={17} />
              新建数据库
            </>
          )}
        </strong>
        <button className="icon-button" aria-label="关闭数据库选择" onClick={handleClose}>
          <X size={18} />
        </button>
      </div>
      {loggedIn && (
        <div className="database-dialog-tabs" role="tablist" aria-label="数据库操作">
          <button
            type="button"
            role="tab"
            aria-selected={effectiveMode === 'open'}
            className={effectiveMode === 'open' ? 'active' : ''}
            onClick={() => handleModeChange('open')}
          >
            <FolderOpen size={13} />
            已有库
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={effectiveMode === 'create'}
            className={effectiveMode === 'create' ? 'active' : ''}
            onClick={() => handleModeChange('create')}
          >
            <FilePlus2 size={13} />
            新建
          </button>
        </div>
      )}
      <div className="database-picker-body">
        {effectiveMode === 'open' ? (
          <form className="database-path-form" onSubmit={submitOpen}>
            <label htmlFor="database-path">数据库路径</label>
            <input
              id="database-path"
              value={openPath}
              onChange={event => handlePathChange(event.target.value)}
              placeholder="例如 data/analytics.db"
              title={loggedIn ? '支持绝对路径或相对项目目录路径' : '登录前只能选择当前服务目录中的 .db 文件'}
              spellCheck={false}
              maxLength={4096}
            />
            <label htmlFor="database-file-input">本机文件</label>
            <input
              ref={fileInput}
              id="database-file-input"
              className="database-file-input"
              type="file"
              accept=".db,application/octet-stream"
              onChange={handleFileChange}
              aria-label="选择本机数据库文件"
            />
            <div className="database-list-heading">
              <span>同目录文件</span>
            </div>
            {!files ? (
              <div className="empty-state">
                <RefreshCw size={17} className="spin" />
                正在读取数据库列表…
              </div>
            ) : files.files.length === 0 ? (
              <div className="empty-state">
                <FolderOpen size={18} />
                <strong>没有 .db 文件</strong>
              </div>
            ) : (
              <div className="database-file-list">
                {files.files.map((file: DatabaseFile) => (
                  <button
                    type="button"
                    key={file.path ?? file.name}
                    className={`database-file ${(file.path ?? file.name) === openPath || (file.active && !selectedFile) ? 'active' : ''}`}
                    disabled={busy}
                    onClick={() => handleExistingSelection(file.path ?? file.name)}
                  >
                    <FolderOpen size={15} />
                    <span>
                      <strong>{file.name}</strong>
                      <small>{formatFileSize(file.size_bytes)}</small>
                    </span>
                    {file.active && <em>当前</em>}
                  </button>
                ))}
              </div>
            )}
            <p className="database-picker-warning">{loggedIn ? '确认后需重新登录；执行中不可切换。' : '确认后使用目标库账号登录。'}</p>
            <button className="primary database-confirm-submit" type="submit" disabled={busy || (!selectedFile && !openPath.trim())}>
              {busy ? <RefreshCw size={14} className="spin" /> : <FolderOpen size={14} />} {busy ? '处理中…' : '确认'}
            </button>
          </form>
        ) : (
          <form className="database-create-form" onSubmit={submitCreate}>
            <div className="database-create-default">默认账号：admin / admin</div>
            <label htmlFor="new-database-path">
              文件路径
              <input
                id="new-database-path"
                value={createPath}
                onChange={event => onCreatePathChange(event.target.value)}
                placeholder="例如 data/analytics.db"
                title="目标文件需不存在，父目录需已存在"
                spellCheck={false}
                maxLength={4096}
              />
            </label>
            <div className="database-config-grid">
              <label>
                页大小
                <input
                  type="number"
                  min={512}
                  max={65536}
                  step={1}
                  required
                  value={createConfig.page_size || ''}
                  onChange={event => onCreateConfigChange({ ...createConfig, page_size: Number(event.target.value) })}
                  title="512–65536，必须是 2 的幂"
                />
              </label>
              <label>
                缓存页数
                <input
                  type="number"
                  min={1}
                  max={4096}
                  step={1}
                  required
                  value={createConfig.buffer_pool_size || ''}
                  onChange={event => onCreateConfigChange({ ...createConfig, buffer_pool_size: Number(event.target.value) })}
                  title="1–4096 页"
                />
              </label>
              <label>
                淘汰策略
                <select
                  value={createConfig.replacement_policy}
                  onChange={event => {
                    const policy = event.target.value
                    if (isReplacementPolicy(policy)) onCreateConfigChange({ ...createConfig, replacement_policy: policy })
                  }}
                  title="当前内核支持 LRU 或 FIFO"
                >
                  <option value="lru">LRU</option>
                  <option value="fifo">FIFO</option>
                  <option value="2q">2Q</option>
                </select>
              </label>
              <label>
                Payload 编码
                <select
                  value={createConfig.payload_codec}
                  onChange={event => {
                    const codec = event.target.value
                    if (isPayloadCodec(codec)) onCreateConfigChange({ ...createConfig, payload_codec: codec })
                  }}
                  title="新建后写入 superblock，已有数据库不能直接切换"
                >
                  <option value="json">JSON 兼容</option>
                  <option value="manual">手写二进制</option>
                </select>
              </label>
            </div>
            <div className="database-picker-warning">目标文件需不存在，目录需已存在。</div>
            <button className="primary database-create-submit" type="submit" disabled={busy || !loggedIn || !createPath.trim()}>
              {busy ? <RefreshCw size={14} className="spin" /> : <FilePlus2 size={14} />} {busy ? '正在创建…' : '创建并连接'}
            </button>
          </form>
        )}
      </div>
    </dialog>
  )
})

export default DatabasePickerDialog
