/** 工作台底部状态栏：运行态、查询参数与编辑器光标信息。 */

import type { QuerySelection } from '../../app/types'
import { statusDotClassName } from '../../view-classes'

export interface WorkbenchStatusBarProps {
  online: boolean
  running: boolean
  rowLimit: number
  timeout: number
  sql: string
  selection: QuerySelection
  onRowLimitChange: (value: number) => void
  onTimeoutChange: (value: number) => void
}

export default function WorkbenchStatusBar({
  online,
  running,
  rowLimit,
  timeout,
  sql,
  selection,
  onRowLimitChange,
  onTimeoutChange
}: WorkbenchStatusBarProps) {
  const line = sql.slice(0, selection.from).split('\n').length
  const column = [...sql.slice(0, selection.from).split('\n').at(-1)!].length + 1
  const selectedLength = Array.from(sql.slice(selection.from, selection.to)).length

  return (
    <footer className="app-status">
      <span className={statusDotClassName(online)} />
      <span>{running ? '执行中' : '就绪'}</span>
      <div className="app-status-settings">
        <label>
          结果上限{' '}
          <select aria-label="结果保留行数" value={rowLimit} onChange={event => onRowLimitChange(Number(event.target.value))}>
            {[100, 1000, 5000].map(count => (
              <option key={count} value={count}>
                {count} 行
              </option>
            ))}
          </select>
        </label>
        <label>
          期限{' '}
          <select aria-label="查询执行期限" value={timeout} onChange={event => onTimeoutChange(Number(event.target.value))}>
            {[5, 15, 30].map(seconds => (
              <option key={seconds} value={seconds}>
                {seconds} s
              </option>
            ))}
          </select>
        </label>
      </div>
      <span className="grow" />
      {selection.from !== selection.to ? (
        <span>已选 {selectedLength} 字符</span>
      ) : (
        <span>
          Ln {line}, Col {column}
        </span>
      )}
    </footer>
  )
}
