import { forwardRef, useCallback, useEffect, useRef, useState, useTransition } from 'react'
import type { ChangeEvent, CSSProperties, FormEvent, PointerEvent as ReactPointerEvent } from 'react'
import type { EditorView } from '@codemirror/view'
import { AlignLeft, Braces, CheckCircle2, CircleAlert, Database, FilePlus2, FolderOpen, HardDrive, LogOut, PanelLeftClose, PanelLeftOpen, Play, Plus, RefreshCw, ShieldCheck, Square, Trash2, UserRound, Workflow, X } from 'lucide-react'
import { api, ApiError, errorMessage, upload } from './api'
import { currentStatement, formatSQL } from './sql'
import type { DatabaseFile, DatabaseFiles, DatabaseSwitch, DBError, Dialect, History, Metadata, QueryTask, SessionInfo, Stage } from './types'
import SqlEditor from './components/SqlEditor'
import SchemaBrowser from './components/SchemaBrowser'
import ResultPanel from './components/ResultPanel'
import PipelinePanel from './components/PipelinePanel'
import StoragePanel from './components/StoragePanel'

const initialSQL = 'SHOW TABLES;\n\n-- 从左侧选择表，或在这里编写 SQL。\n'
interface RunSource {line: number; column: number; original: string}
interface QueryTabState {
  id: string
  title: string
  sql: string
  cursor: number
  selection: {from: number; to: number}
  task: QueryTask | null
  runs: QueryTask[]
  resultIndex: number
  executionError: DBError | null
}

function createQueryTab(id: string, title: string, sql = initialSQL): QueryTabState {
  return {id, title, sql, cursor: 0, selection: {from: 0, to: 0}, task: null, runs: [], resultIndex: 0, executionError: null}
}

export default function App() {
  const [session, setSession] = useState<SessionInfo | null>(null)
  const [booting, setBooting] = useState(true)
  const [online, setOnline] = useState(false)
  const [metadata, setMetadata] = useState<Metadata | null>(null)
  const [dialect, setDialect] = useState<Dialect | null>(null)
  const [history, setHistory] = useState<History | null>(null)
  const [queryTabs, setQueryTabs] = useState<QueryTabState[]>(() => [createQueryTab('query-1', '查询 1')])
  const [activeQueryId, setActiveQueryId] = useState('query-1')
  const [leftOpen, setLeftOpen] = useState(true)
  const [workspaceMode, setWorkspaceMode] = useState<'sql' | 'storage'>('sql')
  const [storageVisited, setStorageVisited] = useState(false)
  const [isModePending, startModeTransition] = useTransition()
  const [selectedTableName, setSelectedTableName] = useState<string | null>(null)
  const [selectedViewName, setSelectedViewName] = useState<string | null>(null)
  const [metaBusy, setMetaBusy] = useState(false)
  const [metaError, setMetaError] = useState('')
  const [activeId, setActiveId] = useState<string | null>(null)
  const [activeRunTabId, setActiveRunTabId] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [pipelineStages, setPipelineStages] = useState<Stage[]>([])
  const [pipelineOpen, setPipelineOpen] = useState(false)
  const [pipelineWidth, setPipelineWidth] = useState(500)
  const [pipelineResizing, setPipelineResizing] = useState(false)
  const [toast, setToast] = useState<{message: string; error: boolean} | null>(null)
  const [pollError, setPollError] = useState('')
  const [databaseFiles, setDatabaseFiles] = useState<DatabaseFiles | null>(null)
  const [databasePickerBusy, setDatabasePickerBusy] = useState(false)
  const [databaseDialogMode, setDatabaseDialogMode] = useState<'open' | 'create'>('open')
  const [openDatabasePath, setOpenDatabasePath] = useState('')
  const [createDatabasePath, setCreateDatabasePath] = useState('data/new_database.db')
  const [createDatabaseConfig, setCreateDatabaseConfig] = useState({page_size: 4096, buffer_pool_size: 64, replacement_policy: 'lru' as 'lru' | 'fifo'})
  const [storageRefreshToken, setStorageRefreshToken] = useState(0)
  const [rowLimit, setRowLimit] = useState(1000)
  const [timeout, setTimeoutValue] = useState(15)
  const editor = useRef<EditorView | null>(null)
  const permissionsDialog = useRef<HTMLDialogElement>(null)
  const databaseDialog = useRef<HTMLDialogElement>(null)
  const sources = useRef(new Map<string, RunSource>())
  const pipelineResizeStart = useRef<{x: number; width: number} | null>(null)
  const activeQuery = queryTabs.find(tab => tab.id === activeQueryId) ?? queryTabs[0]
  const sql = activeQuery?.sql ?? initialSQL
  const cursor = activeQuery?.cursor ?? 0
  const selection = activeQuery?.selection ?? {from: 0, to: 0}
  const task = activeQuery?.task ?? null
  const runs = activeQuery?.runs ?? []
  const resultIndex = activeQuery?.resultIndex ?? 0
  const executionError = activeQuery?.executionError ?? null
  const running = !!activeId || submitting
  const activeTabRunning = running && activeRunTabId === activeQueryId
  const tables = metadata?.databases[0]?.tables ?? []
  const views = metadata?.databases[0]?.views ?? []
  const selectedTable = tables.find(table => table.name === selectedTableName) ?? null
  const notify = useCallback((message: string, error = false) => setToast({message, error}), [])
  const updatePipelineStages = useCallback((stages: Stage[]) => {
    setPipelineStages(stages)
    if (!stages.length) setPipelineOpen(false)
  }, [])
  const updateActiveQuery = useCallback((update: (tab: QueryTabState) => QueryTabState) => {
    setQueryTabs(current => current.map(tab => tab.id === activeQueryId ? update(tab) : tab))
  }, [activeQueryId])
  const setActiveSQL = useCallback((value: string) => updateActiveQuery(tab => ({...tab, sql: value})), [updateActiveQuery])
  const setActiveCursor = useCallback((value: number) => updateActiveQuery(tab => ({...tab, cursor: value})), [updateActiveQuery])
  const setActiveSelection = useCallback((from: number, to: number) => updateActiveQuery(tab => ({...tab, selection: {from, to}})), [updateActiveQuery])
  const setActiveExecutionError = useCallback((value: DBError | null) => updateActiveQuery(tab => ({...tab, executionError: value})), [updateActiveQuery])
  const setActiveResultIndex = useCallback((value: number) => updateActiveQuery(tab => ({...tab, resultIndex: value})), [updateActiveQuery])
  const createNewQuery = useCallback(() => {
    const next = queryTabs.reduce((max, tab) => {
      const match = tab.title.match(/(\d+)$/)
      return Math.max(max, match ? Number(match[1]) : max)
    }, 0) + 1
    const id = `query-${Date.now()}`
    setQueryTabs(current => [...current, createQueryTab(id, `查询 ${next}`, '-- 新建查询\n')])
    setActiveQueryId(id)
    setWorkspaceMode('sql')
  }, [queryTabs])
  const closeQuery = useCallback((id: string) => {
    if (queryTabs.length === 1) return
    if (activeRunTabId === id) {notify('执行中的查询不能关闭。'); return}
    const index = queryTabs.findIndex(tab => tab.id === id)
    setQueryTabs(current => current.filter(tab => tab.id !== id))
    if (id === activeQueryId) {
      const next = queryTabs[index + 1] ?? queryTabs[index - 1]
      if (next) setActiveQueryId(next.id)
    }
  }, [activeQueryId, activeRunTabId, notify, queryTabs])
  const reset = useCallback(() => {
    setSession(null); setMetadata(null); setHistory(null)
    setQueryTabs([createQueryTab('query-1', '查询 1')]); setActiveQueryId('query-1')
    setActiveId(null); setActiveRunTabId(null); setPipelineStages([]); setPipelineOpen(false); setPollError(''); setWorkspaceMode('sql'); setStorageVisited(false); setSelectedTableName(null); setSelectedViewName(null); setDatabaseFiles(null); setDatabasePickerBusy(false); setStorageRefreshToken(0); databaseDialog.current?.close(); sources.current.clear()
  }, [])

  const refreshHistory = useCallback(async () => {
    try {setHistory(await api<History>('/api/history?limit=200'))}
    catch (error) {notify(errorMessage(error), true)}
  }, [notify])
  const refreshMetadata = useCallback(async () => {
    setMetaBusy(true); setMetaError('')
    try {setMetadata(await api<Metadata>('/api/databases'))}
    catch (error) {setMetaError(errorMessage(error))}
    finally {setMetaBusy(false)}
  }, [])
  const refreshSession = useCallback(async () => {
    try {setSession(await api<SessionInfo>('/api/session'))}
    catch (error) {if (!(error instanceof ApiError && error.status === 401)) notify(errorMessage(error), true)}
  }, [notify])

  useEffect(() => {
    const expired = () => {reset(); notify('会话已过期，请重新登录。', true)}
    window.addEventListener('yoursql:expired', expired)
    api<SessionInfo>('/api/session').then(value => {setSession(value); if (value.active_task) {setActiveRunTabId('query-1'); setActiveId(value.active_task)}})
      .catch(() => {}).finally(() => setBooting(false))
    const health = () => fetch('/health', {signal: AbortSignal.timeout(4000)}).then(response => setOnline(response.ok)).catch(() => setOnline(false))
    void health()
    const timer = window.setInterval(health, 10000)
    return () => {clearInterval(timer); window.removeEventListener('yoursql:expired', expired)}
  }, [reset, notify])
  useEffect(() => {
    if (!session?.user) return
    void refreshMetadata(); void refreshHistory()
    api<Dialect>('/api/dialect').then(setDialect).catch(error => notify(errorMessage(error), true))
  }, [session?.user, refreshMetadata, refreshHistory, notify])
  useEffect(() => {
    if (session || booting || !online) return
    api<DatabaseFiles>('/api/databases/available-before-login').then(setDatabaseFiles).catch(() => {})
  }, [session, booting, online])
  useEffect(() => {if (toast) {const timer = setTimeout(() => setToast(null), 7000); return () => clearTimeout(timer)}}, [toast])
  useEffect(() => {
    if (!pipelineResizing) return
    const move = (event: globalThis.PointerEvent) => {
      const start = pipelineResizeStart.current
      if (!start) return
      setPipelineWidth(Math.min(760, Math.max(320, start.width + start.x - event.clientX)))
    }
    const stop = () => {pipelineResizeStart.current = null; setPipelineResizing(false)}
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
    let stopped = false, timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const value = await api<QueryTask>(`/api/queries/${activeId}`)
        if (stopped) return
        setQueryTabs(current => current.map(tab => tab.id === runTabId ? {...tab, task: value} : tab)); setPollError('')
        if (value.status === 'queued' || value.status === 'running') {timer = setTimeout(poll, 400); return}
        const base = sources.current.get(value.id)
        const resolvedError = value.error ? {...value.error, line: (value.error.line ?? 1) + (base?.line ?? 1) - 1,
          column: (value.error.column ?? 1) + ((value.error.line ?? 1) === 1 ? (base?.column ?? 1) - 1 : 0)} : null
        setQueryTabs(current => current.map(tab => tab.id === runTabId ? {...tab, task: value, runs: [value, ...tab.runs.filter(run => run.id !== value.id)].slice(0, 20), resultIndex: Math.max(0, value.results.length - 1), executionError: resolvedError} : tab))
        setActiveId(null); setActiveRunTabId(null)
        if (value.error) {
          notify(value.error.message, true)
        } else notify(`已完成 ${value.results.length} 条语句`)
        void refreshSession(); void refreshMetadata(); void refreshHistory(); setStorageRefreshToken(token => token + 1)
      } catch (error) {
        if (stopped) return
        setPollError(errorMessage(error)); timer = setTimeout(poll, 2000)
      }
    }
    void poll()
    return () => {stopped = true; clearTimeout(timer)}
  }, [activeId, activeRunTabId, notify, refreshSession, refreshMetadata, refreshHistory])

  async function execute(all: boolean, command?: string) {
    if (running || !session) return
    const tabId = activeQueryId
    const text = editor.current?.state.doc.toString() ?? sql
    const range = currentStatement(text, editor.current?.state.selection.main.head ?? cursor)
    const from = all || command ? 0 : range?.from ?? 0
    const query = command ?? (all ? text : range ? text.slice(range.from, range.to) : '')
    if (!query.trim()) {notify('请输入可执行的 SQL。', true); return}
    if (query.length > 64000) {notify('SQL 超过 64000 字符，请拆分执行。', true); return}
    setSubmitting(true); setActiveExecutionError(null); setPollError('')
    try {
      const submitted = await api<{id: string}>('/api/queries', {sql: query, row_limit: rowLimit, timeout_seconds: timeout})
      const prefix = text.slice(0, from)
      sources.current.set(submitted.id, {line: prefix.split('\n').length, column: [...prefix.split('\n').at(-1)!].length + 1, original: text})
      setQueryTabs(current => current.map(tab => tab.id === tabId ? {...tab, task: {id: submitted.id, status: 'queued', results: [], error: null, elapsed_ms: 0, submitted_at: new Date().toISOString(), cancel_requested: false}, resultIndex: 0} : tab))
      setActiveRunTabId(tabId); setActiveId(submitted.id)
    } catch (error) {notify(errorMessage(error), true)}
    finally {setSubmitting(false)}
  }
  async function cancel() {
    if (!activeId) return
    try {const data = await api<{note: string}>(`/api/queries/${activeId}/cancel`, {}); notify(data.note)}
    catch (error) {notify(errorMessage(error), true)}
  }
  async function inspectHistory(id: string) {
    if (running) {notify('请等待当前任务完成。'); return}
    try {const value = await api<QueryTask>(`/api/queries/${id}`); setQueryTabs(current => current.map(tab => tab.id === activeQueryId ? {...tab, task: value, resultIndex: 0, executionError: null} : tab))}
    catch {notify('该历史结果已过期，或来自另一连接。脱敏 SQL 不支持直接重放。', true)}
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
    } catch (error) {notify(errorMessage(error), true)}
    finally {setDatabasePickerBusy(false)}
  }
  async function selectDatabase(path: string) {
    if (databasePickerBusy) return
    if (path === (databaseFiles?.active_path ?? databaseFiles?.active ?? session?.database)) {databaseDialog.current?.close(); return}
    setDatabasePickerBusy(true)
    try {
      const route = session ? '/api/databases/select' : '/api/databases/select-before-login'
      const value = await api<DatabaseSwitch>(route, {path})
      databaseDialog.current?.close()
      if (session) {
        reset()
        notify(`已切换到 ${value.path ?? value.database}，请重新登录。`)
      } else {
        setDatabaseFiles(files => files ? {...files, active: value.database, active_path: value.path,
          files: files.files.map(file => ({...file, active: file.path === value.path || file.name === value.database}))} : files)
        notify(`已切换到 ${value.path ?? value.database}，请使用该数据库中的账号登录。`)
      }
    } catch (error) {notify(errorMessage(error), true)}
    finally {setDatabasePickerBusy(false)}
  }
  async function createDatabase(path: string) {
    if (!session || databasePickerBusy) return
    setDatabasePickerBusy(true)
    try {
      const value = await api<DatabaseSwitch & {config: {page_size: number; buffer_pool_size: number; replacement_policy: string}}>(
        '/api/databases/create', {path, ...createDatabaseConfig})
      databaseDialog.current?.close()
      reset()
      notify(`已创建 ${value.path ?? value.database}，请使用新库账号 admin / admin 登录。`)
    } catch (error) {notify(errorMessage(error), true)}
    finally {setDatabasePickerBusy(false)}
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
        setDatabaseFiles(files => files ? {...files, active: value.database, active_path: value.path,
          files: [...files.files.map(item => ({...item, active: false})),
            {name: value.database, path: value.path, size_bytes: file.size, active: true}]} : files)
        notify(`已导入 ${file.name}，请使用目标库账号登录。`)
      }
    } catch (error) {notify(errorMessage(error), true)}
    finally {setDatabasePickerBusy(false)}
  }
  async function logout() {
    try {await api('/api/auth/logout', {}); reset(); notify('已退出。')}
    catch (error) {notify(errorMessage(error), true)}
  }
  function insertSQL(value: string) {
    if (running) {notify('执行期间编辑器已锁定。'); return}
    setActiveSQL(value); setActiveExecutionError(null); editor.current?.focus()
  }
  function switchWorkspaceMode(mode: 'sql' | 'storage') {
    if (mode === workspaceMode || isModePending) return
    // WHY：先挂载存储页再交给 transition，避免首次进入时把数据请求和视图切换绑在同一帧。
    if (mode === 'storage') {setStorageVisited(true); setPipelineOpen(false)}
    startModeTransition(() => setWorkspaceMode(mode))
  }
  function beginPipelineResize(event: ReactPointerEvent<HTMLDivElement>) {
    event.preventDefault()
    pipelineResizeStart.current = {x: event.clientX, width: pipelineWidth}
    setPipelineResizing(true)
  }
  const line = sql.slice(0, cursor).split('\n').length
  const column = [...sql.slice(0, cursor).split('\n').at(-1)!].length + 1

  return <div className="app-shell">
    <header className="app-header"><div className="brand"><Database size={24}/><strong>YourSQL</strong></div>
      <div className="connection"><span className={`status-dot ${online ? 'online' : 'offline'}`}/><span title={session?.database_path ?? undefined}>{online ? (session ? `已连接 · ${session.database_path ?? session.database}` : '已连接') : '服务不可用'}</span>{session && <button className="database-switch" onClick={() => void openDatabasePicker()} disabled={running || databasePickerBusy} title="选择或新建数据库"><FolderOpen size={13}/><span>数据库</span></button>}<code>{window.location.host}</code></div>
      {session && <nav className="header-workspace-nav" aria-label="工作区模式"><div className="mode-switch" role="tablist" aria-label="工作区模式">
        <button role="tab" aria-selected={workspaceMode === 'sql'} className={workspaceMode === 'sql' ? 'active' : ''} onClick={() => switchWorkspaceMode('sql')} disabled={isModePending}><Braces size={13}/>SQL 工作台</button>
        <button role="tab" aria-selected={workspaceMode === 'storage'} className={workspaceMode === 'storage' ? 'active' : ''} onClick={() => switchWorkspaceMode('storage')} disabled={isModePending}><HardDrive size={13}/>存储检查</button>
      </div></nav>}
      <div className="header-spacer"/>{session && <><button className="user-button" onClick={() => permissionsDialog.current?.showModal()}><span className="avatar"><UserRound size={15}/></span>{session.user}<ShieldCheck size={13}/></button><button className="subtle logout-button" onClick={logout}><LogOut size={14}/>退出</button></>}
    </header>
    {!session ? <Login booting={booting} online={online} activeDatabase={databaseFiles?.active_path ?? databaseFiles?.active ?? null} databasePickerBusy={databasePickerBusy} onOpenDatabasePicker={() => void openDatabasePicker()} onLogin={value => {setSession(value); setToast(null)}}/> : <>
      <main className={`workbench ${leftOpen ? '' : 'left-collapsed'} ${workspaceMode === 'storage' ? 'storage-active' : ''} ${pipelineOpen ? 'pipeline-open' : ''}`} style={{'--pipeline-width': `${pipelineWidth}px`} as CSSProperties} aria-busy={isModePending}>
        {leftOpen && <aside className="sidebar"><SchemaBrowser database={session.database} tables={tables} views={views} refresh={refreshMetadata} insertSQL={insertSQL} loading={metaBusy} error={metaError} selectedTableName={selectedTableName} onSelectTable={name => {setSelectedTableName(name); setSelectedViewName(null)}} selectedViewName={selectedViewName} onSelectView={name => {setSelectedViewName(name); setSelectedTableName(null); setWorkspaceMode('sql')}}/></aside>}
        <div className="main-workspace">
          {isModePending && <div className="workspace-transition-status" role="status" aria-live="polite"><RefreshCw size={13} className="spin"/>正在切换工作区…</div>}
          <div className="query-tabbar">
            <button className="icon-button" onClick={() => setLeftOpen(!leftOpen)} aria-label={leftOpen ? '折叠数据库侧栏' : '展开数据库侧栏'}>{leftOpen ? <PanelLeftClose size={16}/> : <PanelLeftOpen size={16}/>}</button>
            <div className="query-tabs" role="tablist" aria-label="SQL 查询标签">
              {queryTabs.map(tab => <div key={tab.id} className={`query-tab ${tab.id === activeQueryId ? 'active' : ''} ${tab.id === activeRunTabId && activeId ? 'running' : ''}`}>
                <button role="tab" aria-selected={tab.id === activeQueryId} className="query-tab-select" onClick={() => {setActiveQueryId(tab.id); setWorkspaceMode('sql')}} title={`${tab.title} · 独立编辑与结果`}>
                  <Braces size={13}/><span>{tab.title}</span>{tab.sql !== initialSQL && <span className="unsaved-dot" aria-label="有未保存编辑"/>}{tab.id === activeRunTabId && activeId && <RefreshCw size={12} className="spin"/>}
                </button>
                {queryTabs.length > 1 && <button className="query-tab-close" aria-label={`关闭${tab.title}`} onClick={() => closeQuery(tab.id)}><X size={12}/></button>}
              </div>)}
              <button className="query-tab-add" onClick={createNewQuery} aria-label="新建 SQL 查询" title="新建 SQL 查询"><Plus size={15}/></button>
            </div>
            <span className="grow"/>
            {workspaceMode === 'sql' && runs.length > 0 && <select aria-label="切换执行批次" className="run-select" value={task?.id ?? runs[0].id} onChange={event => inspectHistory(event.target.value)} disabled={running}>{runs.map((run, i) => <option key={run.id} value={run.id}>{i === 0 ? '最近执行' : '执行记录'} · {new Date(run.submitted_at).toLocaleTimeString()}</option>)}{activeId && <option value={activeId}>执行中…</option>}</select>}
          </div>
          <div className="workspace-pane" hidden={workspaceMode !== 'sql'}>
            <section className="editor-section" aria-label="SQL 工作区"><div className="editor-toolbar">
              <button className="primary" onClick={() => execute(false)} disabled={running} aria-keyshortcuts="F5"><Play size={14} fill="currentColor"/>执行当前 <kbd>F5</kbd></button>
              <button onClick={() => execute(true)} disabled={running} title="Shift+F5" aria-keyshortcuts="Shift+F5"><Play size={14}/>执行全部</button>
              <button className="subtle" onClick={() => {setActiveSQL(formatSQL(sql, dialect?.keywords ?? [])); notify('已格式化 SQL，保留字符串与注释。')}} disabled={running} title="格式化 SQL"><AlignLeft size={15}/><span>格式化</span></button>
              <button className="subtle" onClick={() => {setActiveSQL(''); setActiveExecutionError(null)}} disabled={running} title="清空编辑器"><Trash2 size={14}/><span>清空</span></button>
              <span className="grow"/>{activeTabRunning && <button className="danger-subtle" onClick={cancel} disabled={!activeId}><Square size={12}/>取消</button>}
              <button className={`pipeline-launch subtle ${pipelineOpen ? 'active' : ''}`} onClick={() => setPipelineOpen(open => !open)} aria-expanded={pipelineOpen} disabled={running || pipelineStages.length === 0} title={pipelineStages.length ? '在右侧工作区查看本次执行的阶段和算子图' : '执行 SQL 后可查看执行流水线'}><Workflow size={14}/><span>{pipelineOpen ? '收起流水线' : '查看执行流水线'}</span></button>
            </div>
            <SqlEditor value={sql} onChange={value => {setActiveSQL(value); setActiveExecutionError(null)}} tables={tables} dialect={dialect} execute={execute} onCursor={setActiveCursor} onSelection={setActiveSelection} error={executionError} running={activeTabRunning} editorRef={editor}/>
            </section>
            {pollError && <div className="poll-error" role="alert">{pollError} 正在获取任务状态… <code>{activeId}</code></div>}
            <ResultPanel task={task} index={resultIndex} onIndex={setActiveResultIndex} notify={notify} history={history} onHistory={inspectHistory} onPipelineStages={updatePipelineStages}/>
          </div>
          {storageVisited && <div className="workspace-pane" hidden={workspaceMode !== 'storage'}><StoragePanel fullMode active={workspaceMode === 'storage'} refreshToken={storageRefreshToken} selectedTable={selectedTable} onSelectTable={name => {setSelectedTableName(name); setSelectedViewName(null)}} onExit={() => switchWorkspaceMode('sql')}/></div>}
        </div>
        {pipelineOpen && <aside className={`pipeline-workspace ${pipelineResizing ? 'is-resizing' : ''}`} aria-label="执行流水线工作区">
          <div className="pipeline-resize-handle" role="separator" aria-label="调整执行流水线宽度" aria-orientation="vertical" aria-valuemin={320} aria-valuemax={760} aria-valuenow={pipelineWidth} onPointerDown={beginPipelineResize}/>
          <div className="pipeline-workspace-inner">
            <header className="pipeline-workspace-header"><div><Workflow size={16}/><div><strong>执行流水线</strong></div></div><button className="icon-button" aria-label="收起执行流水线" onClick={() => setPipelineOpen(false)}><X size={16}/></button></header>
            <div className="pipeline-workspace-body"><PipelinePanel stages={pipelineStages}/></div>
          </div>
        </aside>}
      </main>
      <footer className="app-status"><span className={`status-dot ${online ? 'online' : 'offline'}`}/><span>{running ? '执行中' : '就绪'}</span><div className="app-status-settings"><label>结果上限 <select aria-label="结果保留行数" value={rowLimit} onChange={event => setRowLimit(Number(event.target.value))}>{[100, 1000, 5000].map(count => <option key={count} value={count}>{count} 行</option>)}</select></label><label>期限 <select aria-label="查询执行期限" value={timeout} onChange={event => setTimeoutValue(Number(event.target.value))}>{[5, 15, 30].map(seconds => <option key={seconds} value={seconds}>{seconds} s</option>)}</select></label></div><span className="grow"/>{selection.from !== selection.to ? <span>已选 {Array.from(sql.slice(selection.from, selection.to)).length} 字符</span> : <span>Ln {line}, Col {column}</span>}</footer>
      <dialog ref={permissionsDialog} className="permission-dialog"><div className="panel-heading"><strong><ShieldCheck size={18}/>当前用户权限</strong><button className="icon-button" aria-label="关闭权限" onClick={() => permissionsDialog.current?.close()}><X size={18}/></button></div><p>{session.user} · {session.roles.join(', ') || '无角色'}</p><div className="privileges">{session.permissions.length ? session.permissions.map(permission => <code key={permission}>{permission}</code>) : <span>暂无权限</span>}</div><p className="hint">会话剩余约 {Math.ceil(session.session_expires_in / 60)} 分钟；重启服务需重新登录。</p></dialog>
    </>}
    <DatabasePickerDialog ref={databaseDialog} files={databaseFiles} busy={databasePickerBusy} loggedIn={!!session} mode={databaseDialogMode} onModeChange={setDatabaseDialogMode} openPath={openDatabasePath} onOpenPathChange={setOpenDatabasePath} createPath={createDatabasePath} onCreatePathChange={setCreateDatabasePath} createConfig={createDatabaseConfig} onCreateConfigChange={setCreateDatabaseConfig} onClose={() => databaseDialog.current?.close()} onSelect={selectDatabase} onCreate={createDatabase} onImport={importDatabase}/>
    {toast && <div className={`toast ${toast.error ? 'error' : ''}`} role={toast.error ? 'alert' : 'status'}>{toast.error ? <CircleAlert size={17}/> : <CheckCircle2 size={17}/>}<span>{toast.message}</span><button aria-label="关闭消息" onClick={() => setToast(null)}><X size={14}/></button></div>}
  </div>
}

function Login({booting, online, activeDatabase, databasePickerBusy, onOpenDatabasePicker, onLogin}: {
  booting: boolean; online: boolean; activeDatabase: string | null; databasePickerBusy: boolean
  onOpenDatabasePicker: () => void; onLogin: (value: SessionInfo) => void
}) {
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (databasePickerBusy) {setError('请等待数据库切换完成。'); return}
    setBusy(true); setError('')
    try {const session = await api<SessionInfo>('/api/auth/login', {username, password}); setPassword(''); onLogin(session)}
    catch (error) {setError(errorMessage(error))}
    finally {setBusy(false)}
  }
  return <main className="login-surface"><div className="login-form"><div className="login-icon"><Database size={28}/></div><h1>连接到 YourSQL</h1>
    {booting ? <div className="empty-state"><RefreshCw className="spin"/>正在恢复会话…</div> : <><div className="login-database-picker"><div><span>目标数据库</span><strong title={activeDatabase ?? undefined}>{activeDatabase ?? '尚未选择'}</strong></div><button type="button" onClick={onOpenDatabasePicker} disabled={!online || databasePickerBusy} title="选择数据库文件"><FolderOpen size={15}/>{databasePickerBusy ? '读取中…' : '选择'}</button></div><form onSubmit={submit}><label>用户名<input autoComplete="username" value={username} onChange={event => setUsername(event.target.value)} maxLength={128} required/></label><label>密码<input autoFocus type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} maxLength={1024} required/></label>{error && <div className="inline-error" role="alert">{error}</div>}<button className="primary" type="submit" disabled={busy || databasePickerBusy}>{busy ? <RefreshCw size={15} className="spin"/> : <Database size={15}/>} {busy ? '正在连接…' : '连接数据库'}</button></form></>}
    {!online && <p className="error-text">后端未连接，请先启动 python -m yoursql.web。</p>}
  </div></main>
}

interface DatabasePickerProps {
  files: DatabaseFiles | null
  busy: boolean
  loggedIn: boolean
  mode: 'open' | 'create'
  onModeChange: (mode: 'open' | 'create') => void
  openPath: string
  onOpenPathChange: (path: string) => void
  createPath: string
  onCreatePathChange: (path: string) => void
  createConfig: {page_size: number; buffer_pool_size: number; replacement_policy: 'lru' | 'fifo'}
  onCreateConfigChange: (config: {page_size: number; buffer_pool_size: number; replacement_policy: 'lru' | 'fifo'}) => void
  onClose: () => void
  onSelect: (name: string) => void
  onCreate: (path: string) => void
  onImport: (file: File) => void
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`
}

const DatabasePickerDialog = forwardRef<HTMLDialogElement, DatabasePickerProps>(function DatabasePickerDialog({files, busy, loggedIn, mode, onModeChange, openPath, onOpenPathChange, createPath, onCreatePathChange, createConfig, onCreateConfigChange, onClose, onSelect, onCreate, onImport}, ref) {
  const fileInput = useRef<HTMLInputElement>(null)
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  function clearSelectedFile() {
    setSelectedFile(null)
    if (fileInput.current) fileInput.current.value = ''
  }
  function submitOpen(event: FormEvent) {
    event.preventDefault()
    if (selectedFile) {
      const file = selectedFile
      clearSelectedFile()
      onImport(file)
    }
    else if (openPath.trim()) onSelect(openPath.trim())
  }
  function submitCreate(event: FormEvent) {
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
  function handleModeChange(nextMode: 'open' | 'create') {
    clearSelectedFile()
    if (nextMode === 'open' && files) onOpenPathChange(files.active_path ?? files.active)
    onModeChange(nextMode)
  }
  function handleClose() {
    clearSelectedFile()
    onClose()
  }
  const effectiveMode = loggedIn ? mode : 'open'
  return <dialog ref={ref} className="database-dialog" aria-labelledby="database-picker-title">
    <div className="panel-heading"><strong id="database-picker-title">{effectiveMode === 'open' ? <><FolderOpen size={17}/>选择数据库</> : <><FilePlus2 size={17}/>新建数据库</>}</strong><button className="icon-button" aria-label="关闭数据库选择" onClick={handleClose}><X size={18}/></button></div>
    {loggedIn && <div className="database-dialog-tabs" role="tablist" aria-label="数据库操作"><button type="button" role="tab" aria-selected={effectiveMode === 'open'} className={effectiveMode === 'open' ? 'active' : ''} onClick={() => handleModeChange('open')}><FolderOpen size={13}/>已有库</button><button type="button" role="tab" aria-selected={effectiveMode === 'create'} className={effectiveMode === 'create' ? 'active' : ''} onClick={() => handleModeChange('create')}><FilePlus2 size={13}/>新建</button></div>}
    <div className="database-picker-body">
      {effectiveMode === 'open' ? <>
        <form className="database-path-form" onSubmit={submitOpen}><label htmlFor="database-path">数据库路径</label><input id="database-path" value={openPath} onChange={event => handlePathChange(event.target.value)} placeholder="例如 data/analytics.db" title={loggedIn ? '支持绝对路径或相对项目目录路径' : '登录前只能选择当前服务目录中的 .db 文件'} spellCheck={false} maxLength={4096}/><label htmlFor="database-file-input">本机文件</label><input ref={fileInput} id="database-file-input" className="database-file-input" type="file" accept=".db,application/octet-stream" onChange={handleFileChange} aria-label="选择本机数据库文件"/>
        <div className="database-list-heading"><span>同目录文件</span></div>
        {!files ? <div className="empty-state"><RefreshCw size={17} className="spin"/>正在读取数据库列表…</div> : files.files.length === 0 ? <div className="empty-state"><FolderOpen size={18}/><strong>没有 .db 文件</strong></div> : <div className="database-file-list">{files.files.map((file: DatabaseFile) => <button type="button" key={file.path ?? file.name} className={`database-file ${(file.path ?? file.name) === openPath || file.active && !selectedFile ? 'active' : ''}`} disabled={busy} onClick={() => handleExistingSelection(file.path ?? file.name)}><FolderOpen size={15}/><span><strong>{file.name}</strong><small>{formatFileSize(file.size_bytes)}</small></span>{file.active && <em>当前</em>}</button>)}</div>}
        <p className="database-picker-warning">{loggedIn ? '确认后需重新登录；执行中不可切换。' : '确认后使用目标库账号登录。'}</p><button className="primary database-confirm-submit" type="submit" disabled={busy || (!selectedFile && !openPath.trim())}>{busy ? <RefreshCw size={14} className="spin"/> : <FolderOpen size={14}/>} {busy ? '处理中…' : '确认'}</button></form>
      </> : <>
        <form className="database-create-form" onSubmit={submitCreate}><div className="database-create-default">默认账号：admin / admin</div><label htmlFor="new-database-path">文件路径<input id="new-database-path" value={createPath} onChange={event => onCreatePathChange(event.target.value)} placeholder="例如 data/analytics.db" title="目标文件需不存在，父目录需已存在" spellCheck={false} maxLength={4096}/></label><div className="database-config-grid"><label>页大小<input type="number" min={512} max={65536} step={1} required value={createConfig.page_size || ''} onChange={event => onCreateConfigChange({...createConfig, page_size: Number(event.target.value)})} title="512–65536，必须是 2 的幂"/></label><label>缓存页数<input type="number" min={1} max={4096} step={1} required value={createConfig.buffer_pool_size || ''} onChange={event => onCreateConfigChange({...createConfig, buffer_pool_size: Number(event.target.value)})} title="1–4096 页"/></label><label>淘汰策略<select value={createConfig.replacement_policy} onChange={event => onCreateConfigChange({...createConfig, replacement_policy: event.target.value as 'lru' | 'fifo'})} title="当前内核支持 LRU 或 FIFO"><option value="lru">LRU</option><option value="fifo">FIFO</option></select></label></div><div className="database-picker-warning">目标文件需不存在，目录需已存在。</div><button className="primary database-create-submit" type="submit" disabled={busy || !loggedIn || !createPath.trim()}>{busy ? <RefreshCw size={14} className="spin"/> : <FilePlus2 size={14}/>} {busy ? '正在创建…' : '创建并连接'}</button></form>
      </>}
    </div>
  </dialog>
})

function siblingDatabasePath(path: string): string {
  const separator = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'))
  return separator >= 0 ? `${path.slice(0, separator + 1)}new_database.db` : 'data/new_database.db'
}
