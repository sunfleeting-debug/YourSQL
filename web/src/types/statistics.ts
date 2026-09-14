/** 单条 SQL 执行统计与历史趋势的响应模型。 */

import type { MonitorQuery } from './monitoring'

export interface ExecutionStatisticsResponse {
  current: MonitorQuery
  items: MonitorQuery[]
  total: number
  fingerprint: string
}
