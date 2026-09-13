import { useCallback, useState } from 'react'
import type { FocusEvent, PointerEvent } from 'react'

export interface StorageTooltipState {
  text: string
  left: number
  top: number
  placement: 'top' | 'bottom'
}

/** 存储工作台统一的浮动提示，避免小尺寸块只能依赖原生 title。 */
export function useStorageTooltip() {
  const [tooltip, setTooltip] = useState<StorageTooltipState | null>(null)
  const hideTooltip = useCallback(() => setTooltip(null), [])
  const showTooltip = useCallback((text: string, target: HTMLElement) => {
    const rect = target.getBoundingClientRect()
    const placement = rect.top < 80 ? 'bottom' : 'top'
    setTooltip({
      text,
      left: Math.max(12, Math.min(window.innerWidth - 12, rect.left + rect.width / 2)),
      top: placement === 'top' ? rect.top - 8 : rect.bottom + 8,
      placement
    })
  }, [])
  const tooltipProps = useCallback(
    (text: string) => ({
      onPointerEnter: (event: PointerEvent<HTMLElement>) => showTooltip(text, event.currentTarget),
      onPointerLeave: hideTooltip,
      onFocus: (event: FocusEvent<HTMLElement>) => showTooltip(text, event.currentTarget),
      onBlur: hideTooltip
    }),
    [hideTooltip, showTooltip]
  )
  return { tooltip, tooltipProps }
}

export function StorageTooltip({ tooltip }: { tooltip: StorageTooltipState | null }) {
  if (!tooltip) return null
  return (
    <div className={`storage-tooltip ${tooltip.placement}`} role="tooltip" style={{ left: tooltip.left, top: tooltip.top }}>
      {tooltip.text}
    </div>
  )
}
