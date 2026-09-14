/** App 级状态：只描述工作台编排，不混入后端响应模型。 */

import type { DBError } from '../types/common'
import type { QueryTask } from '../types/query'
import type { ReplacementPolicy } from '../types/storage'

export type WorkspaceMode = 'sql' | 'storage'
export type DatabaseDialogMode = 'open' | 'create'

export interface RunSource {
  line: number
  column: number
  original: string
}

export interface QuerySelection {
  from: number
  to: number
}

export interface QueryTabState {
  id: string
  title: string
  sql: string
  cursor: number
  selection: QuerySelection
  task: QueryTask | null
  runs: QueryTask[]
  resultIndex: number
  executionError: DBError | null
}

export interface DatabaseCreateConfig {
  page_size: number
  buffer_pool_size: number
  replacement_policy: ReplacementPolicy
}

export interface ToastState {
  message: string
  error: boolean
}
