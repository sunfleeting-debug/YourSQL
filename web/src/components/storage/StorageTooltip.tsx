/** 页面地图的悬浮详情状态与提示层。 */

import { useCallback, useMemo, useState } from 'react'
import type { FocusEvent, FocusEventHandler, PointerEvent, PointerEventHandler } from 'react'

export interface StorageTooltipState {
  text: string
  left: number
  top: number
  placement: 'top' | 'bottom'
}

export interface StorageTooltipContainerProps {
  onPointerOver: PointerEventHandler<HTMLDivElement>
  onPointerOut: PointerEventHandler<HTMLDivElement>
  onFocus: FocusEventHandler<HTMLDivElement>
  onBlur: FocusEventHandler<HTMLDivElement>
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
  const findTooltipTarget = useCallback((target: EventTarget | null): HTMLElement | null => {
    return target instanceof HTMLElement ? target.closest<HTMLElement>('[data-storage-tooltip]') : null
  }, [])
  const tooltipContainerProps = useMemo<StorageTooltipContainerProps>(
    () => ({
      onPointerOver: event => {
        const target = findTooltipTarget(event.target)
        if (!target || !event.currentTarget.contains(target)) return
        if (event.relatedTarget instanceof Node && target.contains(event.relatedTarget)) return
        showTooltip(target.dataset.storageTooltip ?? '', target)
      },
      onPointerOut: event => {
        const target = findTooltipTarget(event.target)
        if (!target || !event.currentTarget.contains(target)) return
        if (event.relatedTarget instanceof Node && target.contains(event.relatedTarget)) return
        hideTooltip()
      },
      onFocus: event => {
        const target = findTooltipTarget(event.target)
        if (target && event.currentTarget.contains(target)) showTooltip(target.dataset.storageTooltip ?? '', target)
      },
      onBlur: event => {
        const target = findTooltipTarget(event.target)
        if (!target || !event.currentTarget.contains(target)) return
        if (event.relatedTarget instanceof Node && target.contains(event.relatedTarget)) return
        hideTooltip()
      }
    }),
    [findTooltipTarget, hideTooltip, showTooltip]
  )
  return { tooltip, tooltipProps, tooltipContainerProps }
}

export function StorageTooltip({ tooltip }: { tooltip: StorageTooltipState | null }) {
  if (!tooltip) return null
  return (
    <div className={`storage-tooltip ${tooltip.placement}`} role="tooltip" style={{ left: tooltip.left, top: tooltip.top }}>
      {tooltip.text}
    </div>
  )
}
