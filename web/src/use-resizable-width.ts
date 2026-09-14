import { useCallback, useEffect, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent } from 'react'

type ResizeEdge = 'left' | 'right'

interface ResizeStart {
  clientX: number
  width: number
}

export interface UseResizableWidthOptions {
  initialWidth: number | null
  minWidth: number
  maxWidth: number
  edge: ResizeEdge
}

export interface ResizableWidthState {
  width: number | null
  resizing: boolean
  beginResize: (event: ReactPointerEvent<HTMLElement>) => void
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

/** 横向分栏拖拽：统一处理指针生命周期、宽度边界和拖拽期间的全局交互状态。 */
export function useResizableWidth({ initialWidth, minWidth, maxWidth, edge }: UseResizableWidthOptions): ResizableWidthState {
  const [width, setWidth] = useState<number | null>(initialWidth)
  const [resizing, setResizing] = useState(false)
  const resizeStart = useRef<ResizeStart | null>(null)

  const beginResize = useCallback(
    (event: ReactPointerEvent<HTMLElement>) => {
      event.preventDefault()
      const currentWidth = event.currentTarget.parentElement?.getBoundingClientRect().width
      const startWidth = width ?? currentWidth
      if (startWidth == null) return
      resizeStart.current = { clientX: event.clientX, width: startWidth }
      setWidth(startWidth)
      setResizing(true)
    },
    [width]
  )

  useEffect(() => {
    if (!resizing) return
    const move = (event: globalThis.PointerEvent) => {
      const start = resizeStart.current
      if (!start) return
      const delta = edge === 'right' ? event.clientX - start.clientX : start.clientX - event.clientX
      setWidth(clamp(start.width + delta, minWidth, maxWidth))
    }
    const stop = () => {
      resizeStart.current = null
      setResizing(false)
    }

    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', stop)
    window.addEventListener('pointercancel', stop)
    return () => {
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', stop)
      window.removeEventListener('pointercancel', stop)
    }
  }, [edge, maxWidth, minWidth, resizing])

  return { width, resizing, beginResize }
}
