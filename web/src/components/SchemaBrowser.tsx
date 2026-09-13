import { useState } from 'react'
import { ChevronDown, ChevronRight, Columns3, Database, FileCode2, KeyRound, ListTree, RefreshCw, Search, Table2 } from 'lucide-react'
import type { TableMeta, ViewMeta } from '../types'
import { quoteName } from '../sql'

interface Props {database: string; tables: TableMeta[]; views: ViewMeta[]; refresh: () => void; insertSQL: (sql: string) => void; loading: boolean; error: string; selectedTableName?: string | null; onSelectTable?: (tableName: string | null) => void; selectedViewName?: string | null; onSelectView?: (viewName: string) => void}
export default function SchemaBrowser({database, tables, views, refresh, insertSQL, loading, error, selectedTableName, onSelectTable, selectedViewName, onSelectView}: Props) {
  const [search, setSearch] = useState('')
  const [expanded, setExpanded] = useState<string[]>([])
  const keyword = search.toLowerCase()
  const filtered = tables.filter(table => table.name.toLowerCase().includes(keyword) || table.columns.some(column => column.name.toLowerCase().includes(keyword)))
  const filteredViews = views.filter(view => !view.system && (view.name.toLowerCase().includes(keyword) || view.columns.some(column => column.name.toLowerCase().includes(keyword))))
  const filteredSystemViews = views.filter(view => view.system && (view.name.toLowerCase().includes(keyword) || view.columns.some(column => column.name.toLowerCase().includes(keyword))))
  const toggleExpanded = (key: string) => setExpanded(current => current.includes(key) ? current.filter(item => item !== key) : [...current, key])
  const openView = (view: ViewMeta) => {
    onSelectView?.(view.name)
    // HOW：系统视图定义引用隐藏的 _sys_* 表，编辑器应载入可执行的公开视图查询。
    const sql = view.system
      ? `SELECT * FROM ${quoteName(view.name)} LIMIT 100;`
      : `${view.definition_sql.trim().replace(/;$/, '')};`
    insertSQL(sql)
  }
  const openDefinition = (view: ViewMeta) => {
    onSelectView?.(view.name)
    insertSQL(`${view.definition_sql.trim().replace(/;$/, '')};`)
  }
  const renderView = (view: ViewMeta) => {
    const open = expanded.includes(`view:${view.name}`) || !!search
    return <div key={view.name} className={`schema-table schema-view ${view.system ? 'system-view' : ''}`}>
      <div className={`table-row view-row ${open ? 'open' : ''} ${selectedViewName === view.name ? 'selected' : ''}`}>
        <button type="button" className="table-row-toggle" aria-label={`${open ? '折叠' : '展开'}视图 ${view.name}`} aria-expanded={open} onClick={() => toggleExpanded(`view:${view.name}`)}>{open ? <ChevronDown size={13}/> : <ChevronRight size={13}/>}</button>
        <button type="button" className="table-row-main" aria-pressed={selectedViewName === view.name} onClick={() => openView(view)} title="载入视图查询语句"><FileCode2 size={15}/><span>{view.name}</span><small>{view.system ? 'SYS' : 'VIEW'}</small></button>
      </div>
      {open && <div className="schema-children">
        <div className="schema-section-label"><Columns3 size={12}/>输出字段 <small>{view.columns.length}</small></div>
        {view.columns.map(column => <button key={column.name} className="column-row" onClick={() => insertSQL(`SELECT ${quoteName(column.name)} FROM ${quoteName(view.name)} LIMIT 100;`)} title={`${column.name} ${column.type}`}>
          <span className="field-dot"/><span>{column.name}</span><code>{column.type}</code>
        </button>)}
        <div className="schema-section-label"><FileCode2 size={12}/><button className="text-button definition-button" onClick={() => openDefinition(view)} title={view.system ? '载入系统视图内部定义；仅供查看，执行请查询系统视图名。' : '载入视图查询定义'}>查询定义</button></div>
        <div className="schema-actions"><button className="text-button" onClick={() => insertSQL(`SELECT * FROM ${quoteName(view.name)} LIMIT 100;`)}>查询视图</button>
          <button className="text-button" onClick={() => openView(view)} title="在 SQL 编辑器中查看查询语句"><FileCode2 size={12}/>SQL</button>
          <button className="text-button" onClick={() => insertSQL(`SHOW CREATE VIEW ${quoteName(view.name)};`)} title="查看视图定义"><FileCode2 size={12}/>DDL</button></div>
      </div>}
    </div>
  }
  return <section className="schema-region" aria-label="数据库浏览器">
    <div className="panel-heading"><strong>数据库</strong><button className="subtle" onClick={refresh} disabled={loading} title="刷新数据库元数据"><RefreshCw size={14} className={loading ? 'spin' : ''} />刷新</button></div>
    <label className="search"><Search size={15} /><input aria-label="搜索表或字段" placeholder="搜索表名或字段…" value={search} onChange={event => setSearch(event.target.value)} /></label>
    <div className="database-label"><ChevronDown size={14}/><Database size={16}/><strong title={database}>{database}</strong></div>
    <div className="table-count">对象 <span>{filtered.length + filteredViews.length + filteredSystemViews.length}</span><small>{filtered.length} 表 · {filteredViews.length + filteredSystemViews.length} 视图</small></div>
    {error && <p className="inline-error" role="alert">{error}<button className="text-button" onClick={refresh}>重试</button></p>}
    {!error && !loading && tables.length === 0 && views.length === 0 && <div className="side-empty">暂无可见表或视图<p>创建对象，或由管理员授予 SELECT 权限。</p></div>}
    <div className="schema-tables">{filtered.map(table => {
      const open = expanded.includes(table.name) || !!search
      return <div key={table.name} className="schema-table">
        <div className={`table-row ${open ? 'open' : ''} ${selectedTableName === table.name ? 'selected' : ''}`}>
          <button type="button" className="table-row-toggle" aria-label={`${open ? '折叠' : '展开'}表 ${table.name}`} aria-expanded={open} onClick={() => toggleExpanded(table.name)}>{open ? <ChevronDown size={13}/> : <ChevronRight size={13}/>}</button>
          <button type="button" className="table-row-main" aria-pressed={selectedTableName === table.name} onClick={() => onSelectTable?.(selectedTableName === table.name ? null : table.name)} title="点击选择或取消选择数据表"><Table2 size={15}/><span>{table.name}</span><small>{table.row_count}</small></button>
        </div>
        {open && <div className="schema-children">
          <div className="schema-section-label"><Columns3 size={12}/>字段 <small>{table.columns.length}</small></div>
          {table.columns.map(column => <button key={column.name} className="column-row" onClick={() => insertSQL(`SELECT ${quoteName(column.name)} FROM ${quoteName(table.name)} LIMIT 100;`)} title={`${column.name} ${column.type} · ${column.nullable ? '可空' : '非空'}${column.unique ? ' · UNIQUE' : ''}`}>
            {column.primary_key ? <KeyRound size={12} className="key"/> : <span className="field-dot"/>}<span>{column.name}</span><code>{column.type}</code>
          </button>)}
          <div className="schema-section-label"><ListTree size={12}/>索引 <small>{table.indexes.length}</small></div>
          {table.indexes.length === 0 ? <span className="small-note">暂无显式索引</span> : table.indexes.map(index => <div className="index-row" key={index.name} title={index.columns.join(', ')}><ListTree size={12}/><span>{index.name}</span><small>{index.unique ? '唯一' : ''}</small></div>)}
          <div className="schema-actions"><button className="text-button" onClick={() => insertSQL(`SELECT * FROM ${quoteName(table.name)} LIMIT 100;`)}>查询表</button>
            <button className="text-button" onClick={() => insertSQL(`DESC ${quoteName(table.name)};`)}>结构</button>
            <button className="text-button" onClick={() => insertSQL(`SHOW CREATE TABLE ${quoteName(table.name)};`)} title="查看建表语句"><FileCode2 size={12}/>DDL</button></div>
        </div>}
      </div>
    })}
      {filteredViews.length > 0 && <div className="schema-object-group"><div className="schema-object-label"><FileCode2 size={12}/>视图 <small>{filteredViews.length}</small></div>{filteredViews.map(renderView)}</div>}
      {filteredSystemViews.length > 0 && <div className="schema-object-group system-view-group"><div className="schema-object-label"><FileCode2 size={12}/>系统视图 <small>{filteredSystemViews.length}</small></div>{filteredSystemViews.map(renderView)}</div>}
    </div>
    <div className="schema-foot">单数据库 · 元数据按权限过滤</div>
  </section>
}
