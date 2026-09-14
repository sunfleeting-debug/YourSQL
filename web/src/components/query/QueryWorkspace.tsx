/** SQL 工作区：查询标签、编辑器、结果区和执行流水线的布局编排。 */

import type { PointerEvent as ReactPointerEvent, ReactNode, RefObject } from 'react'
import type { EditorView } from '@codemirror/view'
import { AlignLeft, Braces, PanelLeftClose, PanelLeftOpen, Play, Plus, RefreshCw, Square, Trash2, Workflow, X } from 'lucide-react'
import { INITIAL_SQL } from '../../app/constants'
import type { DBError } from '../../types/common'
import type { Dialect, TableMeta } from '../../types/catalog'
import type { History, QueryTask, Stage } from '../../types/query'
import type { QueryTabState, WorkspaceMode } from '../../app/types'
import { pipelineLaunchClassName, pipelineWorkspaceClassName, queryTabClassName } from '../../view-classes'
import PipelinePanel from './PipelinePanel'
import ResultPanel from './ResultPanel'
import SqlEditor from './SqlEditor'

export interface QueryWorkspaceProps {
  leftOpen: boolean
  workspaceMode: WorkspaceMode
  modePending: boolean
  queryTabs: QueryTabState[]
  activeQueryId: string
  activeRunTabId: string | null
  activeId: string | null
  sql: string
  task: QueryTask | null
  runs: QueryTask[]
  resultIndex: number
  executionError: DBError | null
  running: boolean
  activeTabRunning: boolean
  dialect: Dialect | null
  tables: TableMeta[]
  history: History | null
  pipelineStages: Stage[]
  pipelineOpen: boolean
  pipelineWidth: number
  pipelineResizing: boolean
  pollError: string
  storagePane: ReactNode
  editorRef: RefObject<EditorView | null>
  onToggleSidebar: () => void
  onSelectQuery: (id: string) => void
  onCreateQuery: () => void
  onCloseQuery: (id: string) => void
  onInspectHistory: (id: string) => void
  onExecute: (all: boolean) => void
  onFormatSql: () => void
  onClearSql: () => void
  onCancel: () => void
  onChangeSql: (value: string) => void
  onCursor: (position: number) => void
  onSelection: (from: number, to: number) => void
  onResultIndex: (index: number) => void
  onPipelineStages: (stages: Stage[]) => void
  onTogglePipeline: () => void
  onBeginPipelineResize: (event: ReactPointerEvent<HTMLDivElement>) => void
  onClosePipeline: () => void
  notify: (message: string, error?: boolean) => void
}

export default function QueryWorkspace({
  leftOpen,
  workspaceMode,
  modePending,
  queryTabs,
  activeQueryId,
  activeRunTabId,
  activeId,
  sql,
  task,
  runs,
  resultIndex,
  executionError,
  running,
  activeTabRunning,
  dialect,
  tables,
  history,
  pipelineStages,
  pipelineOpen,
  pipelineWidth,
  pipelineResizing,
  pollError,
  storagePane,
  editorRef,
  onToggleSidebar,
  onSelectQuery,
  onCreateQuery,
  onCloseQuery,
  onInspectHistory,
  onExecute,
  onFormatSql,
  onClearSql,
  onCancel,
  onChangeSql,
  onCursor,
  onSelection,
  onResultIndex,
  onPipelineStages,
  onTogglePipeline,
  onBeginPipelineResize,
  onClosePipeline,
  notify
}: QueryWorkspaceProps) {
  const hasPipeline = pipelineStages.length > 0

  return (
    <>
      <div className="main-workspace">
        {modePending && (
          <div className="workspace-transition-status" role="status" aria-live="polite">
            <RefreshCw size={13} className="spin" />
            正在切换工作区…
          </div>
        )}
        <QueryTabBar
          leftOpen={leftOpen}
          workspaceMode={workspaceMode}
          queryTabs={queryTabs}
          activeQueryId={activeQueryId}
          activeRunTabId={activeRunTabId}
          activeId={activeId}
          running={running}
          runs={runs}
          task={task}
          onToggleSidebar={onToggleSidebar}
          onSelectQuery={onSelectQuery}
          onCreateQuery={onCreateQuery}
          onCloseQuery={onCloseQuery}
          onInspectHistory={onInspectHistory}
        />
        <div className="workspace-pane" hidden={workspaceMode !== 'sql'}>
          <section className="editor-section" aria-label="SQL 工作区">
            <div className="editor-toolbar">
              <button className="primary" onClick={() => onExecute(false)} disabled={running} aria-keyshortcuts="F5">
                <Play size={14} fill="currentColor" />
                执行当前 <kbd>F5</kbd>
              </button>
              <button onClick={() => onExecute(true)} disabled={running} title="Shift+F5" aria-keyshortcuts="Shift+F5">
                <Play size={14} />
                执行全部
              </button>
              <button className="subtle" onClick={onFormatSql} disabled={running} title="格式化 SQL">
                <AlignLeft size={15} />
                <span>格式化</span>
              </button>
              <button className="subtle" onClick={onClearSql} disabled={running} title="清空编辑器">
                <Trash2 size={14} />
                <span>清空</span>
              </button>
              <span className="grow" />
              {activeTabRunning && (
                <button className="danger-subtle" onClick={onCancel} disabled={!activeId}>
                  <Square size={12} />
                  取消
                </button>
              )}
              <button
                className={pipelineLaunchClassName(pipelineOpen)}
                onClick={onTogglePipeline}
                aria-expanded={pipelineOpen}
                disabled={running || !hasPipeline}
                title={hasPipeline ? '在右侧工作区查看本次执行的阶段和算子图' : '执行 SQL 后可查看执行流水线'}
              >
                <Workflow size={14} />
                <span>{pipelineOpen ? '收起流水线' : '查看执行流水线'}</span>
              </button>
            </div>
            <SqlEditor
              value={sql}
              onChange={onChangeSql}
              tables={tables}
              dialect={dialect}
              execute={onExecute}
              onCursor={onCursor}
              onSelection={onSelection}
              error={executionError}
              running={activeTabRunning}
              editorRef={editorRef}
            />
          </section>
          {pollError && (
            <div className="poll-error" role="alert">
              {pollError} 正在获取任务状态… <code>{activeId}</code>
            </div>
          )}
          <ResultPanel
            task={task}
            index={resultIndex}
            onIndex={onResultIndex}
            notify={notify}
            history={history}
            onHistory={onInspectHistory}
            onPipelineStages={onPipelineStages}
          />
        </div>
        {storagePane}
      </div>
      {pipelineOpen && (
        <aside className={pipelineWorkspaceClassName(pipelineResizing)} aria-label="执行流水线工作区">
          <div
            className="pipeline-resize-handle"
            role="separator"
            aria-label="调整执行流水线宽度"
            aria-orientation="vertical"
            aria-valuemin={320}
            aria-valuemax={760}
            aria-valuenow={pipelineWidth}
            onPointerDown={onBeginPipelineResize}
          />
          <div className="pipeline-workspace-inner">
            <header className="pipeline-workspace-header">
              <div>
                <Workflow size={16} />
                <div>
                  <strong>执行流水线</strong>
                </div>
              </div>
              <button className="icon-button" aria-label="收起执行流水线" onClick={onClosePipeline}>
                <X size={16} />
              </button>
            </header>
            <div className="pipeline-workspace-body">
              <PipelinePanel stages={pipelineStages} />
            </div>
          </div>
        </aside>
      )}
    </>
  )
}

interface QueryTabBarProps {
  leftOpen: boolean
  workspaceMode: WorkspaceMode
  queryTabs: QueryTabState[]
  activeQueryId: string
  activeRunTabId: string | null
  activeId: string | null
  running: boolean
  runs: QueryTask[]
  task: QueryTask | null
  onToggleSidebar: () => void
  onSelectQuery: (id: string) => void
  onCreateQuery: () => void
  onCloseQuery: (id: string) => void
  onInspectHistory: (id: string) => void
}

/** 查询标签独立管理切换、关闭和执行批次选择，避免主工作区混入标签细节。 */
function QueryTabBar({
  leftOpen,
  workspaceMode,
  queryTabs,
  activeQueryId,
  activeRunTabId,
  activeId,
  running,
  runs,
  task,
  onToggleSidebar,
  onSelectQuery,
  onCreateQuery,
  onCloseQuery,
  onInspectHistory
}: QueryTabBarProps) {
  return (
    <div className="query-tabbar">
      <button className="icon-button" onClick={onToggleSidebar} aria-label={leftOpen ? '折叠数据库侧栏' : '展开数据库侧栏'}>
        {leftOpen ? <PanelLeftClose size={16} /> : <PanelLeftOpen size={16} />}
      </button>
      <div className="query-tabs" role="tablist" aria-label="SQL 查询标签">
        {queryTabs.map(tab => (
          <div key={tab.id} className={queryTabClassName({ active: tab.id === activeQueryId, running: tab.id === activeRunTabId && !!activeId })}>
            <button
              role="tab"
              aria-selected={tab.id === activeQueryId}
              className="query-tab-select"
              onClick={() => onSelectQuery(tab.id)}
              title={`${tab.title} · 独立编辑与结果`}
            >
              <Braces size={13} />
              <span>{tab.title}</span>
              {tab.sql !== INITIAL_SQL && <span className="unsaved-dot" aria-label="有未保存编辑" />}
              {tab.id === activeRunTabId && activeId && <RefreshCw size={12} className="spin" />}
            </button>
            {queryTabs.length > 1 && (
              <button className="query-tab-close" aria-label={`关闭${tab.title}`} onClick={() => onCloseQuery(tab.id)}>
                <X size={12} />
              </button>
            )}
          </div>
        ))}
        <button className="query-tab-add" onClick={onCreateQuery} aria-label="新建 SQL 查询" title="新建 SQL 查询">
          <Plus size={15} />
        </button>
      </div>
      <span className="grow" />
      {workspaceMode === 'sql' && runs.length > 0 && (
        <select
          aria-label="切换执行批次"
          className="run-select"
          value={task?.id ?? runs[0].id}
          onChange={event => onInspectHistory(event.target.value)}
          disabled={running}
        >
          {runs.map((run, i) => (
            <option key={run.id} value={run.id}>
              {i === 0 ? '最近执行' : '执行记录'} · {new Date(run.submitted_at).toLocaleTimeString()}
            </option>
          ))}
          {activeId && <option value={activeId}>执行中…</option>}
        </select>
      )}
    </div>
  )
}
