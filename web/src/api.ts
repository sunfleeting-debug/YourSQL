import type { DBError } from './types'

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: DBError,
    public requestId = ''
  ) {
    super(detail.message)
  }
}

export async function api<T>(path: string, body?: object, signal?: AbortSignal): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-YourSQL-Client': 'workbench' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: signal ?? AbortSignal.timeout(10000)
    })
  } catch (error) {
    if (signal?.aborted) throw error
    throw new ApiError(0, {
      code: 'NETWORK_ERROR',
      message: '连接中断或请求超时。已提交的任务可能仍在运行，请恢复连接后查看。'
    })
  }
  const result = await response.json()
  if (!response.ok || !result.ok) {
    if (response.status === 401 && path !== '/api/auth/login') window.dispatchEvent(new Event('yoursql:expired'))
    throw new ApiError(response.status, result.error ?? { code: 'SERVICE_ERROR', message: '服务返回异常' }, result.request_id)
  }
  return result.data as T
}

export async function upload<T>(path: string, file: File, signal?: AbortSignal): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/octet-stream',
        'X-YourSQL-Client': 'workbench',
        'X-YourSQL-File-Name': encodeURIComponent(file.name)
      },
      body: file,
      signal: signal ?? AbortSignal.timeout(60_000)
    })
  } catch (error) {
    if (signal?.aborted) throw error
    throw new ApiError(0, { code: 'NETWORK_ERROR', message: '文件上传中断或超时。' })
  }
  const result = await response.json()
  if (!response.ok || !result.ok) {
    if (response.status === 401) window.dispatchEvent(new Event('yoursql:expired'))
    throw new ApiError(response.status, result.error ?? { code: 'SERVICE_ERROR', message: '服务返回异常' }, result.request_id)
  }
  return result.data as T
}

export const errorMessage = (error: unknown): string => (error instanceof Error ? error.message : '操作失败')
export const elapsed = (ms: number): string => (ms < 1000 ? `${ms.toFixed(1)} ms` : `${(ms / 1000).toFixed(2)} s`)
