import { useState } from 'react'
import { ChevronLeft, ChevronRight, Clock3, RefreshCw } from 'lucide-react'
import { elapsed } from '../api'
import type { History } from '../types'

export default function HistoryPanel({history, refresh, inspect}: {history: History | null; refresh: () => void; inspect: (id: string) => void}) {
  const [page, setPage] = useState(0)
  const [search, setSearch] = useState('')
  const items = history?.items.filter(item => item.sql.toLowerCase().includes(search.toLowerCase())) ?? []
  const visible = items.slice(page * 6, page * 6 + 6)
  return <section className="history-region" aria-label="查询历史">
    <div className="panel-heading"><strong><Clock3 size={14}/>查询历史</strong><button className="icon-button" aria-label="刷新查询历史" onClick={refresh}><RefreshCw size={13}/></button></div>
    <input className="history-search" aria-label="搜索查询历史" placeholder="搜索历史 SQL…" value={search} onChange={event => {setSearch(event.target.value); setPage(0)}}/>
    {visible.length === 0 ? <p className="side-empty">暂无查询记录</p> : visible.map(item => <button key={item.id} className="history-item" onClick={() => inspect(item.task_id)} title={`${item.sql}\n${item.error_summary ?? '成功'} · ${elapsed(item.elapsed_ms)}`}>
      <span className={`history-dot ${item.status}`}/><code>{item.sql}</code><small>{elapsed(item.elapsed_ms)}</small>
      <time>{new Date(item.executed_at).toLocaleTimeString()}</time>
    </button>)}
    {items.length > 6 && <div className="small-pagination"><button aria-label="上一页历史" disabled={!page} onClick={() => setPage(page - 1)}><ChevronLeft size={13}/></button><span>{page + 1} / {Math.ceil(items.length / 6)}</span><button aria-label="下一页历史" disabled={(page + 1) * 6 >= items.length} onClick={() => setPage(page + 1)}><ChevronRight size={13}/></button></div>}
    <p className="history-note" title={history?.retention}>最近 200 条 · 字面量已脱敏 · 不可直接重放</p>
  </section>
}
