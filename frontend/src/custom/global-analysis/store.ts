import { useSyncExternalStore } from 'react'
import type { LevelType, PriceLevel } from '@/lib/api'
import { getLocale, t } from '@/lib/i18n'
import {
  globalApi,
  normalizeGlobalAiError,
  type GlobalMarket,
  type GlobalReport,
} from './client'

export type Phase = 'loading' | 'streaming' | 'done' | 'error'

export interface ActiveGlobalTask {
  id: string
  market: GlobalMarket
  kind: 'market' | 'stock'
  symbol: string
  name: string
  focus: string
  locale: string
  phase: Phase
  content: string
  error: string
  meta: {
    summary?: string
    currency?: string
    close?: number | null
    levels?: Record<LevelType, PriceLevel[]>
  } | null
  createdAt: number
  savedReportId?: string
}

const MAX_ACTIVE = 3

let activeTasks: ActiveGlobalTask[] = []
let history: GlobalReport[] = []
let historyLoaded = false
let activeDialogTaskId: string | null = null
let dialogMinimized = false
const listeners = new Set<() => void>()

let _activeSnap: ActiveGlobalTask[] = []
let _historySnap: GlobalReport[] = []
let _dialogSnap: { taskId: string | null; minimized: boolean } = { taskId: activeDialogTaskId, minimized: dialogMinimized }

function rebuildSnap() {
  _activeSnap = activeTasks
  _historySnap = history
  _dialogSnap = { taskId: activeDialogTaskId, minimized: dialogMinimized }
}

function emit() {
  rebuildSnap()
  listeners.forEach(fn => fn())
}

function subscribe(fn: () => void) {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

function patchTask(id: string, patch: Partial<ActiveGlobalTask>) {
  activeTasks = activeTasks.map(task => (task.id === id ? { ...task, ...patch } : task))
  emit()
}

export function useGlobalHistory(): { reports: GlobalReport[]; loaded: boolean } {
  const reports = useSyncExternalStore(subscribe, () => _historySnap, () => [])
  return { reports, loaded: historyLoaded }
}

export function useGlobalDialogState() {
  return useSyncExternalStore(subscribe, () => _dialogSnap, () => ({ taskId: null, minimized: false }))
}

export function useGlobalDialogTask(): {
  task: ActiveGlobalTask | GlobalReport | null
  mode: 'active' | 'history' | null
} {
  const ds = useGlobalDialogState()
  const active = useSyncExternalStore(subscribe, () => _activeSnap, () => [])
  const hist = useSyncExternalStore(subscribe, () => _historySnap, () => [])
  if (!ds.taskId) return { task: null, mode: null }
  if (ds.taskId.startsWith('history:')) {
    const rid = ds.taskId.slice('history:'.length)
    return { task: hist.find(r => r.id === rid) ?? null, mode: 'history' }
  }
  return { task: active.find(x => x.id === ds.taskId) ?? null, mode: 'active' }
}

export function useGlobalRunningTasks(): ActiveGlobalTask[] {
  const active = useSyncExternalStore(subscribe, () => _activeSnap, () => [])
  return active.filter(x => x.phase === 'loading' || x.phase === 'streaming')
}

export async function loadGlobalHistory(): Promise<void> {
  try {
    const res = await globalApi.listReports()
    history = res.reports ?? []
    historyLoaded = true
    emit()
  } catch { /* keep silent */ }
}

export async function deleteGlobalReport(id: string): Promise<void> {
  try {
    await globalApi.deleteReport(id)
    history = history.filter(r => r.id !== id)
    if (activeDialogTaskId === `history:${id}`) {
      activeDialogTaskId = null
      dialogMinimized = false
    }
    emit()
  } catch { /* keep silent */ }
}

export function openGlobalHistoryReport(reportId: string): void {
  activeDialogTaskId = `history:${reportId}`
  dialogMinimized = false
  emit()
}

export function openGlobalDialog(taskId: string): void {
  activeDialogTaskId = taskId
  dialogMinimized = false
  emit()
}

export function minimizeGlobalDialog(): void {
  dialogMinimized = true
  emit()
}

export function closeGlobalDialog(): void {
  activeDialogTaskId = null
  dialogMinimized = false
  emit()
}

export async function startGlobalAnalysis(params: {
  market: GlobalMarket
  kind: 'market' | 'stock'
  symbol: string
  name: string
  focus?: string
}): Promise<{ id?: string; error?: string }> {
  const { market, kind, symbol, name, focus = '' } = params
  const existing = activeTasks.find(
    x =>
      x.market === market
      && x.kind === kind
      && x.symbol === symbol
      && (x.phase === 'loading' || x.phase === 'streaming'),
  )
  if (existing) {
    activeDialogTaskId = existing.id
    dialogMinimized = false
    emit()
    return { id: existing.id }
  }

  const ongoing = activeTasks.filter(x => x.phase === 'loading' || x.phase === 'streaming')
  if (ongoing.length >= MAX_ACTIVE) {
    return { error: t('global.tooManyTasks', { n: MAX_ACTIVE }) }
  }

  const locale = getLocale()
  const id = `gtask_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`
  const task: ActiveGlobalTask = {
    id,
    market,
    kind,
    symbol,
    name,
    focus,
    locale,
    phase: 'loading',
    content: '',
    error: '',
    meta: null,
    createdAt: Date.now(),
  }
  activeTasks = [...activeTasks, task]
  activeDialogTaskId = id
  dialogMinimized = false
  emit()

  void runStream(task)
  return { id }
}

async function runStream(task: ActiveGlobalTask): Promise<void> {
  const { id, market, kind, symbol, focus, locale } = task
  try {
    let firstDelta = true
    const stream = kind === 'market'
      ? globalApi.analyzeMarketStream(market, focus, locale as 'zh-CN' | 'en' | 'ja')
      : globalApi.analyzeStockStream(market, symbol, focus, locale as 'zh-CN' | 'en' | 'ja')

    for await (const chunk of stream) {
      const cur = activeTasks.find(x => x.id === id)
      if (!cur) return
      if (chunk.type === 'meta') {
        patchTask(id, {
          name: chunk.name || cur.name,
          meta: {
            summary: chunk.summary,
            currency: chunk.currency,
            close: chunk.close,
            levels: chunk.levels,
          },
        })
      } else if (chunk.type === 'delta') {
        if (firstDelta) {
          patchTask(id, { phase: 'streaming' })
          firstDelta = false
        }
        patchTask(id, { content: cur.content + (chunk.content ?? '') })
      } else if (chunk.type === 'error') {
        patchTask(id, { phase: 'error', error: normalizeGlobalAiError(chunk.message || t('common.analysisFailed')) })
        return
      } else if (chunk.type === 'done') {
        patchTask(id, { phase: 'done' })
      }
    }

    const final = activeTasks.find(x => x.id === id)
    if (final && final.phase !== 'error') {
      if (!final.content) {
        patchTask(id, { phase: 'error', error: t('common.emptyAiOutput') })
        return
      }
      if (final.phase !== 'done') {
        patchTask(id, { phase: 'done' })
      }
      try {
        const res = await globalApi.saveReport({
          market: final.market,
          kind: final.kind,
          symbol: final.symbol,
          name: final.name,
          focus: final.focus,
          locale: final.locale,
          content: final.content,
          summary: final.meta?.summary ?? '',
          currency: final.meta?.currency ?? '',
          close: final.meta?.close ?? null,
          levels: final.meta?.levels,
        })
        if (res.report) {
          patchTask(id, { savedReportId: res.report.id })
          history = [res.report, ...history.filter(r => r.id !== res.report.id)]
          historyLoaded = true
          emit()
        }
      } catch { /* ignore persistence failure */ }
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : t('common.analysisFailed')
    patchTask(id, { phase: 'error', error: normalizeGlobalAiError(msg) })
  }
}
