import { useCallback, useEffect, useRef, useState, useTransition } from 'react'
import type { CSSProperties, PointerEvent as ReactPointerEvent } from 'react'
import type { EditorView } from '@codemirror/view'
import { api, ApiError, errorMessage, upload } from './api'
import { DEFAULT_DATABASE_CONFIG, FIRST_QUERY_ID, INITIAL_SQL, createQueryTab } from './app/constants'
import type { DatabaseCreateConfig, DatabaseDialogMode, QueryTabState, RunSource, ToastState, WorkspaceMode } from './app/types'
import Login from './components/auth/Login'
import { AppHeader, PermissionDialog, Toast } from './components/layout'
import DatabasePickerDialog from './components/database/DatabasePickerDialog'
import { siblingDatabasePath } from './components/database/utils'
import { QueryWorkspace } from './components/query'
import { SchemaBrowser } from './components/schema'
import { StoragePanel } from './components/storage'
import { PerformancePanel } from './components/monitoring'
import WorkbenchStatusBar from './components/layout/WorkbenchStatusBar'
import { currentStatement, formatSQL } from './sql'
import { workbenchClassName } from './view-classes'
import type { DBError, SessionInfo } from './types/common'
import type { DatabaseFiles, DatabaseSwitch, Dialect, Metadata } from './types/catalog'
import type { History, QueryTask, Stage } from './types/query'

export default function App() {
  // 会话与全局连接状态
  const [session, setSession] = useState<SessionInfo | null>(null)
  const [booting, setBooting] = useState(true)
  const [online, setOnline] = useState(false)
  const [metadata, setMetadata] = useState<Metadata | null>(null)
  const [dialect, setDialect] = useState<Dialect | null>(null)
  const [history, setHistory] = useState<History | null>(null)

  // SQL 标签与当前工作区
  const [queryTabs, setQueryTabs] = useState<QueryTabState[]>(() => [createQueryTab(FIRST_QUERY_ID, '查询 1')])
  const [activeQueryId, setActiveQueryId] = useState(FIRST_QUERY_ID)
  const [leftOpen, setLeftOpen] = useState(true)
  const [workspaceMode, setWorkspaceMode] = useState<WorkspaceMode>('sql')
  const [storageVisited, setStorageVisited] = useState(false)
  const [isModePending, startModeTransition] = useTransition()
  const [selectedTableName, setSelectedTableName] = useState<string | null>(null)
  const [selectedViewName, setSelectedViewName] = useState<string | null>(null)

  // 查询执行与流水线
  const [metaBusy, setMetaBusy] = useState(false)
  const [metaError, setMetaError] = useState('')
  const [activeId, setActiveId] = useState<string | null>(null)
  const [activeRunTabId, setActiveRunTabId] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [pipelineStages, setPipelineStages] = useState<Stage[]>([])
  const [pipelineOpen, setPipelineOpen] = useState(false)
  const [pipelineWidth, setPipelineWidth] = useState(500)
  const [pipelineResizing, setPipelineResizing] = useState(false)

  // 数据库选择器与存储页配置
  const [toast, setToast] = useState<ToastState | null>(null)
  const [pollError, setPollError] = useState('')
  const [databaseFiles, setDatabaseFiles] = useState<DatabaseFiles | null>(null)
  const [databasePickerBusy, setDatabasePickerBusy] = useState(false)
  const [databaseDialogMode, setDatabaseDialogMode] = useState<DatabaseDialogMode>('open')
  const [openDatabasePath, setOpenDatabasePath] = useState('')
  const [createDatabasePath, setCreateDatabasePath] = useState('data/new_database.db')
  const [createDatabaseConfig, setCreateDatabaseConfig] = useState<DatabaseCreateConfig>(DEFAULT_DATABASE_CONFIG)
  const [storageRefreshToken, setStorageRefreshToken] = useState(0)
  const [rowLimit, setRowLimit] = useState(1000)
  const [timeout, setTimeoutValue] = useState(15)

  // 非渲染资源：编辑器、弹窗、请求来源和拖拽起点
  const editor = useRef<EditorView | null>(null)
  const permissionsDialog = useRef<HTMLDialogElement>(null)
  const databaseDialog = useRef<HTMLDialogElement>(null)
  const sources = useRef(new Map<string, RunSource>())
  const pipelineResizeStart = useRef<{ x: number; width: number } | null>(null)

  // 从当前标签派生的视图数据
  const activeQuery = queryTabs.find(tab => tab.id === activeQueryId) ?? queryTabs[0]
  const sql = activeQuery?.sql ?? INITIAL_SQL
  const cursor = activeQuery?.cursor ?? 0
  const selection = activeQuery?.selection ?? { from: 0, to: 0 }
  const task = activeQuery?.task ?? null
  const runs = activeQuery?.runs ?? []
  const resultIndex = activeQuery?.resultIndex ?? 0
  const executionError = activeQuery?.executionError ?? null
  const running = !!activeId || submitting
  const activeTabRunning = running && activeRunTabId === activeQueryId
  const tables = metadata?.databases[0]?.tables ?? []
  const views = metadata?.databases[0]?.views ?? []
  const selectedTable = tables.find(table => table.name === selectedTableName) ?? null

  // 标签、通知和流水线更新
  const notify = useCallback((message: string, error = false) => setToast({ message, error }), [])
  const updatePipelineStages = useCallback((stages: Stage[]) => {
    setPipelineStages(stages)
    if (!stages.length) setPipelineOpen(false)
  }, [])
  const updateActiveQuery = useCallback(
    (update: (tab: QueryTabState) => QueryTabState) => {
      setQueryTabs(current => current.map(tab => (tab.id === activeQueryId ? update(tab) : tab)))
    },
    [activeQueryId]
  )
  const setActiveSQL = useCallback((value: string) => updateActiveQuery(tab => ({ ...tab, sql: value })), [updateActiveQuery])
  const setActiveCursor = useCallback((value: number) => updateActiveQuery(tab => ({ ...tab, cursor: value })), [updateActiveQuery])
  const setActiveSelection = useCallback(
    (from: number, to: number) => updateActiveQuery(tab => ({ ...tab, selection: { from, to } })),
    [updateActiveQuery]
  )
  const setActiveExecutionError = useCallback(
    (value: DBError | null) => updateActiveQuery(tab => ({ ...tab, executionError: value })),
    [updateActiveQuery]
  )
  const setActiveResultIndex = useCallback((value: number) => updateActiveQuery(tab => ({ ...tab, resultIndex: value })), [updateActiveQuery])
  const createNewQuery = useCallback(() => {
    const next =
      queryTabs.reduce((max, tab) => {
        const match = tab.title.match(/(\d+)$/)
        return Math.max(max, match ? Number(match[1]) : max)
      }, 0) + 1
    const id = `query-${Date.now()}`
    setQueryTabs(current => [...current, createQueryTab(id, `查询 ${next}`, '-- 新建查询\n')])
    setActiveQueryId(id)
    setWorkspaceMode('sql')
  }, [queryTabs])
  const closeQuery = useCallback(
    (id: string) => {
      if (queryTabs.length === 1) return
      if (activeRunTabId === id) {
        notify('执行中的查询不能关闭。')
        return
      }
      const index = queryTabs.findIndex(tab => tab.id === id)
      setQueryTabs(current => current.filter(tab => tab.id !== id))
      if (id === activeQueryId) {
        const next = queryTabs[index + 1] ?? queryTabs[index - 1]
        if (next) setActiveQueryId(next.id)
      }
    },
    [activeQueryId, activeRunTabId, notify, queryTabs]
  )
  const reset = useCallback(() => {
    setSession(null)
    setMetadata(null)
    setHistory(null)
    setQueryTabs([createQueryTab(FIRST_QUERY_ID, '查询 1')])
    setActiveQueryId(FIRST_QUERY_ID)
    setActiveId(null)
    setActiveRunTabId(null)
    setPipelineStages([])
    setPipelineOpen(false)
    setPollError('')
    setWorkspaceMode('sql')
    setStorageVisited(false)
    setSelectedTableName(null)
    setSelectedViewName(null)
    setDatabaseFiles(null)
    setDatabasePickerBusy(false)
    setStorageRefreshToken(0)
    databaseDialog.current?.close()
    sources.current.clear()
  }, [])

  const refreshHistory = useCallback(async () => {
    try {
      setHistory(await api<History>('/api/history?limit=200'))
    } catch (error) {
      notify(errorMessage(error), true)
    }
  }, [notify])
  const refreshMetadata = useCallback(async () => {
    setMetaBusy(true)
    setMetaError('')
    try {
      setMetadata(await api<Metadata>('/api/databases'))
    } catch (error) {
      setMetaError(errorMessage(error))
    } finally {
      setMetaBusy(false)
    }
  }, [])
  const refreshSession = useCallback(async () => {
    try {
      setSession(await api<SessionInfo>('/api/session'))
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) notify(errorMessage(error), true)
    }
  }, [notify])

  // 连接恢复、元数据与提示反馈
  useEffect(() => {
    const expired = () => {
      reset()
      notify('会话已过期，请重新登录。', true)
    }
    window.addEventListener('yoursql:expired', expired)
    api<SessionInfo>('/api/session')
      .then(value => {
        setSession(value)
        if (value.active_task) {
          setActiveRunTabId(FIRST_QUERY_ID)
          setActiveId(value.active_task)
        }
      })
      .catch(() => {})
      .finally(() => setBooting(false))
    const health = () =>
      fetch('/health', { signal: AbortSignal.timeout(4000) })
        .then(response => setOnline(response.ok))
        .catch(() => setOnline(false))
    void health()
    const timer = window.setInterval(health, 10000)
    return () => {
      clearInterval(timer)
      window.removeEventListener('yoursql:expired', expired)
    }
  }, [reset, notify])
  useEffect(() => {
    if (!session?.user) return
    void refreshMetadata()
    void refreshHistory()
    api<Dialect>('/api/dialect')
      .then(setDialect)
      .catch(error => notify(errorMessage(error), true))
  }, [session?.user, refreshMetadata, refreshHistory, notify])
  useEffect(() => {
    if (session || booting || !online) return
    api<DatabaseFiles>('/api/databases/available-before-login')
      .then(setDatabaseFiles)
      .catch(() => {})
  }, [session, booting, online])
  useEffect(() => {
    if (toast) {
      const timer = setTimeout(() => setToast(null), 7000)
      return () => clearTimeout(timer)
    }
  }, [toast])
  useEffect(() => {
    if (!pipelineResizing) return
    const move = (event: globalThis.PointerEvent) => {
      const start = pipelineResizeStart.current
      if (!start) return
      setPipelineWidth(Math.min(760, Math.max(320, start.width + start.x - event.clientX)))
    }
    const stop = () => {
      pipelineResizeStart.current = null
      setPipelineResizing(false)
    }
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', stop)
    return () => {
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', stop)
    }
  }, [pipelineResizing])

  useEffect(() => {
    if (!activeId || !activeRunTabId) return
    const runTabId = activeRunTabId
    let stopped = false,
      timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const value = await api<QueryTask>(`/api/queries/${activeId}`)
        if (stopped) return
        setQueryTabs(current => current.map(tab => (tab.id === runTabId ? { ...tab, task: value } : tab)))
        setPollError('')
        if (value.status === 'queued' || value.status === 'running') {
          timer = setTimeout(poll, 400)
          return
        }
        const base = sources.current.get(value.id)
        const resolvedError = value.error
          ? {
              ...value.error,
              line: (value.error.line ?? 1) + (base?.line ?? 1) - 1,
              column: (value.error.column ?? 1) + ((value.error.line ?? 1) === 1 ? (base?.column ?? 1) - 1 : 0)
            }
          : null
        setQueryTabs(current =>
          current.map(tab =>
            tab.id === runTabId
              ? {
                  ...tab,
                  task: value,
                  runs: [value, ...tab.runs.filter(run => run.id !== value.id)].slice(0, 20),
                  resultIndex: Math.max(0, value.results.length - 1),
                  executionError: resolvedError
                }
              : tab
          )
        )
        setActiveId(null)
        setActiveRunTabId(null)
        if (value.error) {
          notify(value.error.message, true)
        } else notify(`已完成 ${value.results.length} 条语句`)
        void refreshSession()
        void refreshMetadata()
        void refreshHistory()
        setStorageRefreshToken(token => token + 1)
      } catch (error) {
        if (stopped) return
        setPollError(errorMessage(error))
        timer = setTimeout(poll, 2000)
      }
    }
    void poll()
    return () => {
      stopped = true
      clearTimeout(timer)
    }
  }, [activeId, activeRunTabId, notify, refreshSession, refreshMetadata, refreshHistory])

  // 查询执行与历史操作
  async function execute(all: boolean, command?: string) {
    if (running || !session) return
    const tabId = activeQueryId
    const text = editor.current?.state.doc.toString() ?? sql
    const range = currentStatement(text, editor.current?.state.selection.main.head ?? cursor)
    const from = all || command ? 0 : (range?.from ?? 0)
    const query = command ?? (all ? text : range ? text.slice(range.from, range.to) : '')
    if (!query.trim()) {
      notify('请输入可执行的 SQL。', true)
      return
    }
    if (query.length > 64000) {
      notify('SQL 超过 64000 字符，请拆分执行。', true)
      return
    }
    setSubmitting(true)
    setActiveExecutionError(null)
    setPollError('')
    try {
      const submitted = await api<{ id: string }>('/api/queries', {
        sql: query,
        row_limit: rowLimit,
        timeout_seconds: timeout
      })
      const prefix = text.slice(0, from)
      sources.current.set(submitted.id, {
        line: prefix.split('\n').length,
        column: [...prefix.split('\n').at(-1)!].length + 1,
        original: text
      })
      setQueryTabs(current =>
        current.map(tab =>
          tab.id === tabId
            ? {
                ...tab,
                task: {
                  id: submitted.id,
                  status: 'queued',
                  results: [],
                  error: null,
                  elapsed_ms: 0,
                  submitted_at: new Date().toISOString(),
                  cancel_requested: false
                },
                resultIndex: 0
              }
            : tab
        )
      )
      setActiveRunTabId(tabId)
      setActiveId(submitted.id)
    } catch (error) {
      notify(errorMessage(error), true)
    } finally {
      setSubmitting(false)
    }
  }
  async function cancel() {
    if (!activeId) return
    try {
      const data = await api<{ note: string }>(`/api/queries/${activeId}/cancel`, {})
      notify(data.note)
    } catch (error) {
      notify(errorMessage(error), true)
    }
  }
  async function inspectHistory(id: string) {
    if (running) {
      notify('请等待当前任务完成。')
      return
    }
    try {
      const value = await api<QueryTask>(`/api/queries/${id}`)
      setQueryTabs(current => current.map(tab => (tab.id === activeQueryId ? { ...tab, task: value, resultIndex: 0, executionError: null } : tab)))
    } catch {
      notify('该历史结果已过期，或来自另一连接。脱敏 SQL 不支持直接重放。', true)
    }
  }
  async function openDatabasePicker() {
    if (running || databasePickerBusy) return
    setDatabasePickerBusy(true)
    try {
      const route = session ? '/api/databases/available' : '/api/databases/available-before-login'
      const value = await api<DatabaseFiles>(route)
      setDatabaseFiles(value)
      setDatabaseDialogMode('open')
      setOpenDatabasePath(value.active_path ?? value.active)
      setCreateDatabasePath(siblingDatabasePath(value.active_path ?? value.active))
      if (!databaseDialog.current?.open) databaseDialog.current?.showModal()
    } catch (error) {
      notify(errorMessage(error), true)
    } finally {
      setDatabasePickerBusy(false)
    }
  }
  async function selectDatabase(path: string) {
    if (databasePickerBusy) return
    if (path === (databaseFiles?.active_path ?? databaseFiles?.active ?? session?.database)) {
      databaseDialog.current?.close()
      return
    }
    setDatabasePickerBusy(true)
    try {
      const route = session ? '/api/databases/select' : '/api/databases/select-before-login'
      const value = await api<DatabaseSwitch>(route, { path })
      databaseDialog.current?.close()
      if (session) {
        reset()
        notify(`已切换到 ${value.path ?? value.database}，请重新登录。`)
      } else {
        setDatabaseFiles(files =>
          files
            ? {
                ...files,
                active: value.database,
                active_path: value.path,
                files: files.files.map(file => ({
                  ...file,
                  active: file.path === value.path || file.name === value.database
                }))
              }
            : files
        )
        notify(`已切换到 ${value.path ?? value.database}，请使用该数据库中的账号登录。`)
      }
    } catch (error) {
      notify(errorMessage(error), true)
    } finally {
      setDatabasePickerBusy(false)
    }
  }
  async function createDatabase(path: string) {
    if (!session || databasePickerBusy) return
    setDatabasePickerBusy(true)
    try {
      const value = await api<DatabaseSwitch & { config: DatabaseCreateConfig }>('/api/databases/create', { path, ...createDatabaseConfig })
      databaseDialog.current?.close()
      reset()
      notify(`已创建 ${value.path ?? value.database}，请使用新库账号 admin / admin 登录。`)
    } catch (error) {
      notify(errorMessage(error), true)
    } finally {
      setDatabasePickerBusy(false)
    }
  }
  async function importDatabase(file: File) {
    if (databasePickerBusy) return
    setDatabasePickerBusy(true)
    try {
      const route = session ? '/api/databases/import' : '/api/databases/import-before-login'
      const value = await upload<DatabaseSwitch>(route, file)
      databaseDialog.current?.close()
      if (session) {
        reset()
        notify(`已导入 ${file.name}，请重新登录。`)
      } else {
        setDatabaseFiles(files =>
          files
            ? {
                ...files,
                active: value.database,
                active_path: value.path,
                files: [
                  ...files.files.map(item => ({ ...item, active: false })),
                  { name: value.database, path: value.path, size_bytes: file.size, active: true }
                ]
              }
            : files
        )
        notify(`已导入 ${file.name}，请使用目标库账号登录。`)
      }
    } catch (error) {
      notify(errorMessage(error), true)
    } finally {
      setDatabasePickerBusy(false)
    }
  }
  async function logout() {
    try {
      await api('/api/auth/logout', {})
      reset()
      notify('已退出。')
    } catch (error) {
      notify(errorMessage(error), true)
    }
  }
  // 编辑器插入、工作区切换和流水线拖拽
  function insertSQL(value: string) {
    if (running) {
      notify('执行期间编辑器已锁定。')
      return
    }
    setActiveSQL(value)
    setActiveExecutionError(null)
    editor.current?.focus()
  }
  function switchWorkspaceMode(mode: WorkspaceMode) {
    if (mode === workspaceMode || isModePending) return
    // WHY：先挂载存储页再交给 transition，避免首次进入时把数据请求和视图切换绑在同一帧。
    if (mode === 'storage') {
      setStorageVisited(true)
      setPipelineOpen(false)
    }
    if (mode === 'monitor') setPipelineOpen(false)
    startModeTransition(() => setWorkspaceMode(mode))
  }
  function beginPipelineResize(event: ReactPointerEvent<HTMLDivElement>) {
    event.preventDefault()
    pipelineResizeStart.current = { x: event.clientX, width: pipelineWidth }
    setPipelineResizing(true)
  }
  // 应用壳层渲染
  return (
    <div className="app-shell">
      <AppHeader
        session={session}
        online={online}
        running={running}
        databasePickerBusy={databasePickerBusy}
        payloadCodec={dialect?.payload_codec ?? null}
        workspaceMode={workspaceMode}
        modePending={isModePending}
        onOpenDatabasePicker={() => void openDatabasePicker()}
        onSwitchWorkspaceMode={switchWorkspaceMode}
        onOpenPermissions={() => permissionsDialog.current?.showModal()}
        onLogout={logout}
      />
      {!session ? (
        <Login
          booting={booting}
          online={online}
          activeDatabase={databaseFiles?.active_path ?? databaseFiles?.active ?? null}
          databasePickerBusy={databasePickerBusy}
          onOpenDatabasePicker={() => void openDatabasePicker()}
          onLogin={value => {
            setSession(value)
            setToast(null)
          }}
        />
      ) : (
        <>
          <main
            className={workbenchClassName({ leftOpen, storage: workspaceMode === 'storage', pipelineOpen })}
            style={{ '--pipeline-width': String(pipelineWidth) + 'px' } as CSSProperties}
            aria-busy={isModePending}
          >
            {leftOpen && (
              <aside className="sidebar">
                <SchemaBrowser
                  database={session.database}
                  tables={tables}
                  views={views}
                  refresh={refreshMetadata}
                  insertSQL={insertSQL}
                  loading={metaBusy}
                  error={metaError}
                  selectedTableName={selectedTableName}
                  onSelectTable={name => {
                    setSelectedTableName(name)
                    setSelectedViewName(null)
                  }}
                  selectedViewName={selectedViewName}
                  onSelectView={name => {
                    setSelectedViewName(name)
                    setSelectedTableName(null)
                    setWorkspaceMode('sql')
                  }}
                />
              </aside>
            )}
            <QueryWorkspace
              leftOpen={leftOpen}
              workspaceMode={workspaceMode}
              modePending={isModePending}
              queryTabs={queryTabs}
              activeQueryId={activeQueryId}
              activeRunTabId={activeRunTabId}
              activeId={activeId}
              running={running}
              sql={sql}
              task={task}
              runs={runs}
              resultIndex={resultIndex}
              executionError={executionError}
              activeTabRunning={activeTabRunning}
              dialect={dialect}
              tables={tables}
              history={history}
              pipelineStages={pipelineStages}
              pipelineOpen={pipelineOpen}
              pipelineWidth={pipelineWidth}
              pipelineResizing={pipelineResizing}
              pollError={pollError}
              storagePane={
                storageVisited && (
                  <div className="workspace-pane" hidden={workspaceMode !== 'storage'}>
                    <StoragePanel
                      fullMode
                      active={workspaceMode === 'storage'}
                      refreshToken={storageRefreshToken}
                      selectedTable={selectedTable}
                      onSelectTable={name => {
                        setSelectedTableName(name)
                        setSelectedViewName(null)
                      }}
                      onExit={() => switchWorkspaceMode('sql')}
                    />
                  </div>
                )
              }
              performancePane={
                <div className="workspace-pane" hidden={workspaceMode !== 'monitor'}>
                  <PerformancePanel active={workspaceMode === 'monitor'} />
                </div>
              }
              editorRef={editor}
              onToggleSidebar={() => setLeftOpen(open => !open)}
              onSelectQuery={id => {
                setActiveQueryId(id)
                setWorkspaceMode('sql')
              }}
              onCreateQuery={createNewQuery}
              onCloseQuery={closeQuery}
              onInspectHistory={inspectHistory}
              onExecute={execute}
              onFormatSql={() => {
                setActiveSQL(formatSQL(sql, dialect?.keywords ?? []))
                notify('已格式化 SQL，保留字符串与注释。')
              }}
              onClearSql={() => {
                setActiveSQL('')
                setActiveExecutionError(null)
              }}
              onCancel={cancel}
              onChangeSql={value => {
                setActiveSQL(value)
                setActiveExecutionError(null)
              }}
              onCursor={setActiveCursor}
              onSelection={setActiveSelection}
              onResultIndex={setActiveResultIndex}
              onPipelineStages={updatePipelineStages}
              onTogglePipeline={() => setPipelineOpen(open => !open)}
              onBeginPipelineResize={beginPipelineResize}
              onClosePipeline={() => setPipelineOpen(false)}
              notify={notify}
            />
          </main>
          <WorkbenchStatusBar
            online={online}
            running={running}
            rowLimit={rowLimit}
            timeout={timeout}
            sql={sql}
            selection={selection}
            onRowLimitChange={setRowLimit}
            onTimeoutChange={setTimeoutValue}
          />
          <PermissionDialog ref={permissionsDialog} session={session} onClose={() => permissionsDialog.current?.close()} />
        </>
      )}
      <DatabasePickerDialog
        ref={databaseDialog}
        files={databaseFiles}
        busy={databasePickerBusy}
        loggedIn={!!session}
        mode={databaseDialogMode}
        onModeChange={setDatabaseDialogMode}
        openPath={openDatabasePath}
        onOpenPathChange={setOpenDatabasePath}
        createPath={createDatabasePath}
        onCreatePathChange={setCreateDatabasePath}
        createConfig={createDatabaseConfig}
        onCreateConfigChange={setCreateDatabaseConfig}
        onClose={() => databaseDialog.current?.close()}
        onSelect={selectDatabase}
        onCreate={createDatabase}
        onImport={importDatabase}
      />
      {toast && <Toast toast={toast} onClose={() => setToast(null)} />}
    </div>
  )
}
