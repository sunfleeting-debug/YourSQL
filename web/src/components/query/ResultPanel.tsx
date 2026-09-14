/** 查询结果、分页结果和结果级执行计划。 */

import { useEffect, useState, useTransition } from 'react'
import {
  BarChart3,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clipboard,
  Clock3,
  Download,
  FileJson,
  Play,
  RefreshCw,
  Table2,
  TriangleAlert
} from 'lucide-react'
import { api, elapsed, errorMessage } from '../../api'
import { csv } from '../../sql'
import type { JsonValue } from '../../types/common'
import type { ExecutionStatisticsResponse } from '../../types/statistics'
import type { History, QueryResult, QueryTask, QueryTaskStatus, ResultViewMode, Stage } from '../../types/query'
import ExecutionStatistics from './ExecutionStatistics'
import ExplainResult from './ExplainResult'

export interface ResultPanelProps {
  task: QueryTask | null
  index: number
  onIndex: (index: number) => void
  notify: (message: string, error?: boolean) => void
  history: History | null
  onHistory: (id: string) => void
  onPipelineStages: (stages: Stage[]) => void
}

const RESULT_VIEW_OPTIONS: Array<readonly [ResultViewMode, string]> = [
  ['table', '结果'],
  ['messages', '消息'],
  ['statistics', '统计'],
  ['history', '历史'],
  ['json', 'JSON']
]

const TASK_STATUS_LABELS: Record<QueryTaskStatus, string> = {
  success: '执行成功',
  error: '执行失败',
  running: '执行中',
  queued: '排队中',
  cancelled: '已取消',
  timeout: '已超时'
}

export default function ResultPanel({ task, index, onIndex, notify, history, onHistory, onPipelineStages }: ResultPanelProps) {
  const [result, setResult] = useState<QueryResult | null>(null)
  const [mode, setMode] = useState<ResultViewMode>('table')
  const [offset, setOffset] = useState(0)
  const [pageSize, setPageSize] = useState(100)
  const [loading, setLoading] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  const [statistics, setStatistics] = useState<ExecutionStatisticsResponse | null>(null)
  const [statisticsLoading, setStatisticsLoading] = useState(false)
  const [statisticsError, setStatisticsError] = useState('')
  const [statisticsRevision, setStatisticsRevision] = useState(0)
  const [isModePending, startModeTransition] = useTransition()
  const id = task?.id
  const resultCount = task?.results.length ?? 0
  const queryId = id && resultCount ? `${id}:${index}` : null
  useEffect(() => {
    setOffset(0)
    onPipelineStages([])
  }, [id, index, onPipelineStages])
  useEffect(() => {
    setResult(null)
    setError('')
    if (!id || !resultCount) return
    const controller = new AbortController()
    setLoading(true)
    api<QueryResult>(`/api/queries/${id}/results/${index}?offset=${offset}&limit=${pageSize}`, undefined, controller.signal)
      .then(value => {
        setResult(value)
        onPipelineStages(value.stages)
      })
      .catch(error => {
        if (!controller.signal.aborted) setError(errorMessage(error))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [id, index, offset, pageSize, resultCount, revision])

  useEffect(() => {
    setStatistics(null)
    setStatisticsError('')
    if (mode !== 'statistics' || !queryId) return
    const controller = new AbortController()
    setStatisticsLoading(true)
    api<ExecutionStatisticsResponse>(`/api/monitor/queries/${encodeURIComponent(queryId)}/history?limit=30`, undefined, controller.signal)
      .then(value => setStatistics(value))
      .catch(requestError => {
        if (!controller.signal.aborted) setStatisticsError(errorMessage(requestError))
      })
      .finally(() => {
        if (!controller.signal.aborted) setStatisticsLoading(false)
      })
    return () => controller.abort()
  }, [mode, queryId, statisticsRevision])

  function switchMode(nextMode: ResultViewMode) {
    if (nextMode === mode || isModePending) return
    startModeTransition(() => setMode(nextMode))
  }

  async function copy() {
    if (!result) return
    const explain = isExplainResult(result)
    try {
      await navigator.clipboard.writeText(explain ? String(result.rows[0]?.[0] ?? '') : JSON.stringify(result.rows, null, 2))
      notify(explain ? '已复制原始执行计划' : `已复制本页 ${result.rows.length} 行 JSON`)
    } catch {
      notify('无法访问剪贴板，请在原始 JSON 中选择并复制。', true)
    }
  }
  async function download(format: 'csv' | 'json') {
    if (!result || !id) return
    setExporting(true)
    try {
      const rows: JsonValue[][] = []
      for (let from = 0; from < result.retained_rows; from += 500) {
        const part = await api<QueryResult>(`/api/queries/${id}/results/${index}?offset=${from}&limit=500`)
        rows.push(...part.rows)
      }
      const body =
        format === 'csv'
          ? csv(
              result.columns.map(column => column.name),
              rows
            )
          : JSON.stringify({ columns: result.columns, rows, total_rows: result.total_rows, truncated: result.truncated }, null, 2)
      const url = URL.createObjectURL(new Blob([body], { type: format === 'csv' ? 'text/csv;charset=utf-8' : 'application/json' }))
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `yoursql-${id.slice(0, 8)}-${index + 1}.${format}`
      anchor.click()
      URL.revokeObjectURL(url)
      notify(`已导出 ${rows.length} 行${result.truncated ? '（仅保留的结果）' : ''}`)
    } catch (error) {
      notify(errorMessage(error), true)
    } finally {
      setExporting(false)
    }
  }
  return (
    <section className="results" aria-label="执行结果" aria-busy={loading || isModePending}>
      <div className="result-heading">
        <div className="tab-strip">
          {RESULT_VIEW_OPTIONS.map(([value, label]) => (
            <button key={value} className={mode === value ? 'active' : ''} disabled={isModePending} onClick={() => switchMode(value)}>
              {value === 'history' && <Clock3 size={12} />}
              {value === 'statistics' && <BarChart3 size={12} />} {label}
              {value === 'history' && history && <span className="tab-badge">{history.total}</span>}
            </button>
          ))}
        </div>
        {task && (
          <span className={`execution-state ${task.status === 'success' ? 'success-text' : task.status === 'error' ? 'error-text' : ''}`}>
            {task.status === 'success' ? (
              <CheckCircle2 size={14} />
            ) : task.status === 'error' ? (
              <TriangleAlert size={14} />
            ) : (
              <span className="status-dot" />
            )}
            {TASK_STATUS_LABELS[task.status]}
            <small>{elapsed(task.elapsed_ms)}</small>
          </span>
        )}
      </div>
      {isModePending && (
        <div className="view-transition-status" role="status" aria-live="polite">
          <RefreshCw size={13} className="spin" />
          正在切换结果视图…
        </div>
      )}
      {resultCount > 0 && mode !== 'history' && mode !== 'statistics' && (
        <div className="result-toolbar">
          <label>
            语句结果{' '}
            <select aria-label="切换语句结果" value={index} onChange={event => onIndex(Number(event.target.value))}>
              {task!.results.map((item, i) => (
                <option key={i} value={i}>
                  {i + 1} · {item.status === 'success' ? '成功' : '失败'} · {item.sql.slice(0, 45)}
                </option>
              ))}
            </select>
          </label>
          <span className="grow" />
          <button className="subtle" onClick={copy} disabled={!result || loading}>
            <Clipboard size={13} />
            {result && isExplainResult(result) ? '复制计划' : '复制本页'}
          </button>
          {!result || !isExplainResult(result) ? (
            <button className="subtle" onClick={() => download('csv')} disabled={!result?.columns.length || exporting || loading}>
              <Download size={13} />
              CSV
            </button>
          ) : null}
          <button className="subtle" onClick={() => download('json')} disabled={!result || exporting || loading}>
            <FileJson size={13} />
            JSON
          </button>
        </div>
      )}
      {error ? (
        <div className="empty-state error-text">
          <TriangleAlert />
          <strong>结果暂不可用</strong>
          <p>{error}</p>
          <button onClick={() => setRevision(revision + 1)}>
            <RefreshCw size={14} />
            重试
          </button>
        </div>
      ) : loading ? (
        <div className="empty-state">
          <RefreshCw className="spin" />
          <span>正在读取执行结果…</span>
        </div>
      ) : mode === 'statistics' ? (
        <ExecutionStatistics
          data={statistics}
          loading={statisticsLoading}
          error={statisticsError}
          onRetry={() => setStatisticsRevision(value => value + 1)}
        />
      ) : mode === 'history' ? (
        <HistoryInline history={history} inspect={onHistory} />
      ) : !task ? (
        <div className="empty-state">
          <Table2 size={28} />
          <strong>暂无执行结果</strong>
        </div>
      ) : !result ? (
        <div className="empty-state">
          <Play size={24} />
          <strong>{task.error?.message ?? '等待执行结果'}</strong>
          {task.error && <code>{task.error.code}</code>}
        </div>
      ) : mode === 'json' ? (
        <pre className="result-json">{JSON.stringify(result, null, 2)}</pre>
      ) : mode === 'table' && isExplainResult(result) ? (
        <ExplainResult result={result} />
      ) : mode === 'messages' || result.error ? (
        <div className="messages">
          {result.error ? (
            <div className="error-message" role="alert">
              <TriangleAlert size={20} />
              <div>
                <h3>{result.error.code === 'AUTHORIZATION_ERROR' ? '权限不足' : result.error.code === 'TIMEOUT' ? '执行超时' : '执行失败'}</h3>
                <p>{result.error.message}</p>
                <code>
                  {result.error.code} · 行 {result.error.line}，列 {result.error.column}
                </code>
                <p className="hint">{result.error.position_note}</p>
              </div>
            </div>
          ) : (
            <>
              <div className="success-text">
                <CheckCircle2 size={18} />
                <strong>{result.message ?? '查询已完成'}</strong>
              </div>
              <p>
                {result.total_rows} 行结果 · 影响 {result.affected_rows} 行 · {elapsed(result.elapsed_ms)}
              </p>
            </>
          )}
          <dl className="message-details">
            <dt>请求任务</dt>
            <dd>{task.id}</dd>
            <dt>执行统计</dt>
            <dd>
              <pre>{JSON.stringify(result.stats ?? {}, null, 2)}</pre>
            </dd>
          </dl>
        </div>
      ) : result.columns.length === 0 ? (
        <div className="empty-state">
          <CheckCircle2 className="success-text" size={28} />
          <strong>{result.message || '语句执行成功'}</strong>
          <p>
            影响 {result.affected_rows} 行 · {elapsed(result.elapsed_ms)}
          </p>
        </div>
      ) : (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th className="row-number">#</th>
                {result.columns.map((column, i) => (
                  <th key={i}>
                    {column.name}
                    <small title={`类型来源：${column.type_source}`}>
                      {column.type}
                      {column.type_source === 'runtime' ? ' · 推断' : ''}
                    </small>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {result.rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  <td className="row-number">{offset + rowIndex + 1}</td>
                  {row.map((cell, i) => (
                    <td key={i} title={cell === null ? 'NULL' : String(cell)} className={typeof cell === 'number' ? 'numeric' : ''}>
                      {cell === null ? <span className="null-value">NULL</span> : typeof cell === 'object' ? JSON.stringify(cell) : String(cell)}
                    </td>
                  ))}
                </tr>
              ))}
              {result.rows.length === 0 && (
                <tr>
                  <td colSpan={result.columns.length + 1} className="no-rows">
                    查询成功，没有符合条件的数据行
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
      {result && mode !== 'statistics' && (
        <footer className="result-footer">
          <span>
            共 {result.total_rows} 行{result.truncated && <b className="warning-text"> · 保留 {result.retained_rows} 行（已截断）</b>}
            <small>影响 {result.affected_rows} 行</small>
          </span>
          <div className="pagination">
            <button aria-label="上一页结果" onClick={() => setOffset(Math.max(0, offset - pageSize))} disabled={loading || offset === 0}>
              <ChevronLeft size={14} />
            </button>
            <span>
              {Math.floor(offset / pageSize) + 1} / {Math.max(1, Math.ceil(result.retained_rows / pageSize))}
            </span>
            <button
              aria-label="下一页结果"
              onClick={() => setOffset(offset + pageSize)}
              disabled={loading || offset + pageSize >= result.retained_rows}
            >
              <ChevronRight size={14} />
            </button>
            <select
              aria-label="结果每页行数"
              value={pageSize}
              onChange={event => {
                setPageSize(Number(event.target.value))
                setOffset(0)
              }}
            >
              {[50, 100, 500].map(size => (
                <option key={size} value={size}>
                  {size} 行/页
                </option>
              ))}
            </select>
          </div>
        </footer>
      )}
    </section>
  )
}

function isExplainResult(result: QueryResult): boolean {
  return /^\s*EXPLAIN\b/i.test(result.sql) && result.columns.length === 1 && result.columns[0]?.name.toLowerCase() === 'plan'
}

function HistoryInline({ history, inspect }: { history: History | null; inspect: (id: string) => void }) {
  const [search, setSearch] = useState('')
  const query = search.trim().toLowerCase()
  const items = (history?.items ?? []).filter(item => !query || item.sql.toLowerCase().includes(query)).slice(0, 40)
  return (
    <div className="history-inline" aria-label="查询历史">
      <div className="history-inline-toolbar">
        <div>
          <Clock3 size={14} />
          <strong>最近查询</strong>
          <small>{history?.total ?? 0} 条 · 点击记录查看结果</small>
        </div>
        <input aria-label="搜索查询历史" placeholder="过滤 SQL…" value={search} onChange={event => setSearch(event.target.value)} />
      </div>
      {items.length === 0 ? (
        <div className="empty-state">
          <Clock3 size={24} />
          <strong>{history ? '没有匹配的查询' : '暂无查询记录'}</strong>
          <p>{history?.retention ?? '执行 SQL 后，历史记录会显示在这里。'}</p>
        </div>
      ) : (
        <div className="history-inline-list">
          {items.map(item => (
            <button
              key={item.id}
              className="history-inline-item"
              onClick={() => inspect(item.task_id)}
              title={`${item.sql}\n${item.error_summary ?? '成功'} · ${elapsed(item.elapsed_ms)}`}
            >
              <span className={`history-dot ${item.status}`} />
              <code>{item.sql}</code>
              <span className={item.status === 'success' ? 'success-text' : 'error-text'}>{item.status === 'success' ? '成功' : '失败'}</span>
              <small>
                {elapsed(item.elapsed_ms)} · {new Date(item.executed_at).toLocaleTimeString()}
              </small>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
