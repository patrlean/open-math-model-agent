import type { KeyboardEvent, PointerEvent } from 'react'
import {
  columnRanges,
  type ResizableColumn,
} from '../hooks/useResizableColumns'
import { useLanguage } from '../i18n'

interface ColumnResizerProps {
  column: ResizableColumn
  value: number
  onPointerDown: (column: ResizableColumn, event: PointerEvent<HTMLDivElement>) => void
  onReset: (column: ResizableColumn) => void
  onKeyboardResize: (
    column: ResizableColumn,
    direction: -1 | 1,
    largeStep: boolean,
  ) => void
}

export function ColumnResizer({
  column,
  value,
  onPointerDown,
  onReset,
  onKeyboardResize,
}: ColumnResizerProps) {
  const { language } = useLanguage()
  const en = language === 'en'
  const label = column === 'left'
    ? (en ? 'Resize conversation sidebar' : '调整左侧会话栏宽度')
    : (en ? 'Resize inspector panel' : '调整右侧检查器宽度')

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
    event.preventDefault()
    onKeyboardResize(
      column,
      event.key === 'ArrowLeft' ? -1 : 1,
      event.shiftKey,
    )
  }

  return <div
    className={`column-resizer ${column}`}
    role="separator"
    aria-label={label}
    aria-orientation="vertical"
    aria-valuemin={columnRanges[column].min}
    aria-valuemax={columnRanges[column].max}
    aria-valuenow={Math.round(value)}
    tabIndex={0}
    title={en ? 'Drag to resize; double-click to reset' : '拖动调整宽度，双击恢复默认'}
    data-testid={`${column}-column-resizer`}
    onPointerDown={(event) => onPointerDown(column, event)}
    onDoubleClick={() => onReset(column)}
    onKeyDown={handleKeyDown}
  />
}
