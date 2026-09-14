/** 工作台跨领域共享的基础响应类型。 */

export type JsonPrimitive = string | number | boolean | null
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue }
export type JsonObject = { [key: string]: JsonValue }
export type PayloadCodecName = 'json' | 'manual'

export interface DBError {
  code: string
  message: string
  line?: number
  column?: number
  position_accuracy?: string
  position_note?: string
  suggestion?: { kind: 'keyword'; replacement: string }
}

export interface SessionInfo {
  user: string
  roles: string[]
  permissions: string[]
  database: string
  database_path?: string
  session_expires_in: number
  active_task: string | null
}
