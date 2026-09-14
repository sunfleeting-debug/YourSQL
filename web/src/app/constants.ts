/** App 默认值与状态工厂，集中避免 JSX 和事件处理器各自复制约定。 */

import type { DatabaseCreateConfig, QueryTabState } from './types'

export const INITIAL_SQL = 'SHOW TABLES;\n\n-- 从左侧选择表，或在这里编写 SQL。\n'
export const FIRST_QUERY_ID = 'query-1'

export const DEFAULT_DATABASE_CONFIG: DatabaseCreateConfig = {
  page_size: 4096,
  buffer_pool_size: 64,
  replacement_policy: 'lru',
  payload_codec: 'json'
}

export function createQueryTab(id: string, title: string, sql = INITIAL_SQL): QueryTabState {
  return {
    id,
    title,
    sql,
    cursor: 0,
    selection: { from: 0, to: 0 },
    task: null,
    runs: [],
    resultIndex: 0,
    executionError: null
  }
}
