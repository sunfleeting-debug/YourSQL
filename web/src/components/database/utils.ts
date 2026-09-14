import type { ReplacementPolicy } from '../../types/storage'
import type { PayloadCodecName } from '../../types/common'

/** 数据库选择器共享的展示和路径规则。 */

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`
}

export function siblingDatabasePath(path: string): string {
  const separator = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'))
  return separator >= 0 ? `${path.slice(0, separator + 1)}new_database.db` : 'data/new_database.db'
}

export function isReplacementPolicy(value: string): value is ReplacementPolicy {
  return value === 'lru' || value === 'fifo' || value === '2q'
}

export function isPayloadCodec(value: string): value is PayloadCodecName {
  return value === 'json' || value === 'manual'
}
