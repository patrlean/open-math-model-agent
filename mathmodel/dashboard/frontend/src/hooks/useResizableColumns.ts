import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
} from 'react'

export type ResizableColumn = 'left' | 'right'

interface ColumnWidths {
  left: number
  right: number
}

const STORAGE_KEY = 'mathmodel.dashboard.column-widths.v1'
const DEFAULT_WIDTHS: ColumnWidths = { left: 236, right: 328 }
const LEFT_RANGE = { min: 190, max: 360 }
const RIGHT_RANGE = { min: 260, max: 520 }
const MIN_WORKBENCH_WIDTH = 460
const RIGHT_COLUMN_BREAKPOINT = 1060

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value))
}

function normalizedWidths(widths: ColumnWidths, viewportWidth: number): ColumnWidths {
  const left = clamp(widths.left, LEFT_RANGE.min, LEFT_RANGE.max)
  const right = clamp(widths.right, RIGHT_RANGE.min, RIGHT_RANGE.max)
  if (viewportWidth <= RIGHT_COLUMN_BREAKPOINT) return { left, right }

  const available = Math.max(
    LEFT_RANGE.min + RIGHT_RANGE.min,
    viewportWidth - MIN_WORKBENCH_WIDTH,
  )
  let nextLeft = left
  let nextRight = right
  const overflow = nextLeft + nextRight - available
  if (overflow > 0) {
    const rightReduction = Math.min(overflow, nextRight - RIGHT_RANGE.min)
    nextRight -= rightReduction
    nextLeft = Math.max(LEFT_RANGE.min, nextLeft - (overflow - rightReduction))
  }
  return { left: nextLeft, right: nextRight }
}

function readStoredWidths(): ColumnWidths {
  try {
    const stored = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || '{}') as Partial<ColumnWidths>
    return normalizedWidths({
      left: Number.isFinite(stored.left) ? Number(stored.left) : DEFAULT_WIDTHS.left,
      right: Number.isFinite(stored.right) ? Number(stored.right) : DEFAULT_WIDTHS.right,
    }, window.innerWidth)
  } catch {
    return normalizedWidths(DEFAULT_WIDTHS, window.innerWidth)
  }
}

export function useResizableColumns() {
  const [widths, setWidths] = useState<ColumnWidths>(readStoredWidths)
  const widthsRef = useRef(widths)
  const shellRef = useRef<HTMLDivElement>(null)
  const dragCleanupRef = useRef<(() => void) | null>(null)

  const applyWidths = useCallback((next: ColumnWidths) => {
    widthsRef.current = next
    shellRef.current?.style.setProperty('--sidebar-width', `${next.left}px`)
    shellRef.current?.style.setProperty('--inspector-width', `${next.right}px`)
  }, [])

  const commitWidths = useCallback((next: ColumnWidths) => {
    applyWidths(next)
    setWidths(next)
  }, [applyWidths])

  const setColumnWidth = useCallback((column: ResizableColumn, value: number) => {
    const current = widthsRef.current
    const viewportWidth = window.innerWidth
    let next: ColumnWidths

    if (column === 'left') {
      const max = viewportWidth > RIGHT_COLUMN_BREAKPOINT
        ? Math.min(LEFT_RANGE.max, viewportWidth - MIN_WORKBENCH_WIDTH - current.right)
        : LEFT_RANGE.max
      next = { ...current, left: clamp(value, LEFT_RANGE.min, Math.max(LEFT_RANGE.min, max)) }
    } else {
      const max = Math.min(
        RIGHT_RANGE.max,
        viewportWidth - MIN_WORKBENCH_WIDTH - current.left,
      )
      next = { ...current, right: clamp(value, RIGHT_RANGE.min, Math.max(RIGHT_RANGE.min, max)) }
    }
    commitWidths(next)
  }, [commitWidths])

  const startResize = useCallback((
    column: ResizableColumn,
    event: ReactPointerEvent<HTMLDivElement>,
  ) => {
    if (event.button !== 0) return
    event.preventDefault()
    dragCleanupRef.current?.()

    const startX = event.clientX
    const startWidths = widthsRef.current
    document.body.classList.add('is-resizing-columns')

    const onPointerMove = (moveEvent: PointerEvent) => {
      const delta = moveEvent.clientX - startX
      const raw = column === 'left'
        ? startWidths.left + delta
        : startWidths.right - delta
      const current = widthsRef.current
      const viewportWidth = window.innerWidth
      let next: ColumnWidths

      if (column === 'left') {
        const max = viewportWidth > RIGHT_COLUMN_BREAKPOINT
          ? Math.min(LEFT_RANGE.max, viewportWidth - MIN_WORKBENCH_WIDTH - current.right)
          : LEFT_RANGE.max
        next = {
          ...current,
          left: clamp(raw, LEFT_RANGE.min, Math.max(LEFT_RANGE.min, max)),
        }
      } else {
        const max = Math.min(
          RIGHT_RANGE.max,
          viewportWidth - MIN_WORKBENCH_WIDTH - current.left,
        )
        next = {
          ...current,
          right: clamp(raw, RIGHT_RANGE.min, Math.max(RIGHT_RANGE.min, max)),
        }
      }
      applyWidths(next)
    }

    const cleanup = () => {
      window.removeEventListener('pointermove', onPointerMove)
      window.removeEventListener('pointerup', onPointerUp)
      window.removeEventListener('pointercancel', onPointerUp)
      document.body.classList.remove('is-resizing-columns')
      dragCleanupRef.current = null
    }
    const onPointerUp = () => {
      cleanup()
      setWidths({ ...widthsRef.current })
    }

    dragCleanupRef.current = cleanup
    window.addEventListener('pointermove', onPointerMove)
    window.addEventListener('pointerup', onPointerUp)
    window.addEventListener('pointercancel', onPointerUp)
  }, [applyWidths])

  const resetColumn = useCallback((column: ResizableColumn) => {
    setColumnWidth(column, DEFAULT_WIDTHS[column])
  }, [setColumnWidth])

  const resizeWithKeyboard = useCallback((
    column: ResizableColumn,
    direction: -1 | 1,
    largeStep: boolean,
  ) => {
    const step = largeStep ? 24 : 8
    const signedStep = column === 'right' ? -direction * step : direction * step
    setColumnWidth(column, widthsRef.current[column] + signedStep)
  }, [setColumnWidth])

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(widths))
  }, [widths])

  useEffect(() => {
    function normalizeOnResize() {
      const next = normalizedWidths(widthsRef.current, window.innerWidth)
      if (next.left !== widthsRef.current.left || next.right !== widthsRef.current.right) {
        commitWidths(next)
      }
    }
    window.addEventListener('resize', normalizeOnResize)
    return () => window.removeEventListener('resize', normalizeOnResize)
  }, [commitWidths])

  useEffect(() => () => dragCleanupRef.current?.(), [])

  const style = {
    '--sidebar-width': `${widths.left}px`,
    '--inspector-width': `${widths.right}px`,
  } as CSSProperties

  return {
    shellRef,
    style,
    widths,
    startResize,
    resetColumn,
    resizeWithKeyboard,
  }
}

export const columnRanges = {
  left: LEFT_RANGE,
  right: RIGHT_RANGE,
} as const
