import { friendlyStreamError, type KlineRow, type LevelSeries, type LevelType, type PriceLevel } from '@/lib/api'
import { getLocale, t, type Locale } from '@/lib/i18n'

export type GlobalMarket = 'JP' | 'US'

export interface GlobalCatalogItem {
  symbol: string
  name_zh: string
  name_en: string
  name_ja: string
  category: string
  exchange?: string
}

export interface GlobalQuoteItem extends GlobalCatalogItem {
  price: number | null
  prev_close: number | null
  change: number | null
  change_pct: number | null
  currency: string
  exchange: string
  as_of: string | null
}

export interface GlobalNewsItem {
  title: string
  publisher: string
  link: string
  published_at: string | null
  /** "ticker" = 与该标的相关; "market" = 大盘背景,不一定与该标的相关 */
  scope?: 'ticker' | 'market'
}

export interface GlobalOverviewResponse {
  market: GlobalMarket
  as_of: string | null
  benchmarks: GlobalQuoteItem[]
  sectors: GlobalQuoteItem[]
  leaders: GlobalQuoteItem[]
  news: GlobalNewsItem[]
  catalog_stocks: GlobalCatalogItem[]
}

export interface GlobalStockQuoteMeta {
  symbol: string
  name: string
  long_name: string
  name_zh: string
  name_en: string
  name_ja: string
  exchange: string
  currency: string
  price: number | null
  prev_close: number | null
  change: number | null
  change_pct: number | null
  high_52w: number | null
  low_52w: number | null
  day_high: number | null
  day_low: number | null
  volume: number | null
  as_of: string | null
  sector: string
  industry: string
}

export interface GlobalStockDetailResponse {
  market: GlobalMarket
  symbol: string
  quote: GlobalStockQuoteMeta
  rows: KlineRow[]
  levels: Record<LevelType, PriceLevel[]>
  series?: LevelSeries
  dates?: string[]
  close: number | null
  summary: string
  news: GlobalNewsItem[]
}

export interface GlobalReport {
  id: string
  market: GlobalMarket
  kind: 'market' | 'stock'
  symbol: string
  name: string
  focus: string
  locale: string
  content: string
  summary?: string
  currency?: string
  close?: number | null
  levels?: Record<LevelType, PriceLevel[]>
  created_at: string
}

export type GlobalStreamChunk =
  | {
      type: 'meta'
      market: GlobalMarket
      kind: 'market' | 'stock'
      symbol?: string
      name?: string
      currency?: string
      as_of?: string
      close?: number | null
      summary?: string
      levels?: Record<LevelType, PriceLevel[]>
    }
  | { type: 'delta'; content: string }
  | { type: 'error'; message: string }
  | { type: 'done' }
  | { type: 'heartbeat' }

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    ...init,
  })
  if (!res.ok) {
    let detail = ''
    try {
      const data = await res.json()
      detail = data?.detail || data?.message || ''
    } catch { /* ignore */ }
    throw new Error(detail || `${res.status} ${res.statusText}`)
  }
  return res.json() as Promise<T>
}

async function* streamNdjson(url: string, body: Record<string, unknown>): AsyncGenerator<GlobalStreamChunk> {
  let res: Response
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  } catch (err) {
    throw new Error(friendlyStreamError(err instanceof Error ? err.message : String(err)))
  }
  if (!res.ok || !res.body) {
    let detail = ''
    try {
      const data = await res.json()
      detail = data?.detail || data?.message || ''
    } catch { /* ignore */ }
    throw new Error(detail || `${res.status} ${res.statusText}`)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let nl = buf.indexOf('\n')
    while (nl !== -1) {
      const line = buf.slice(0, nl).trim()
      buf = buf.slice(nl + 1)
      if (line) {
        try {
          const parsed = JSON.parse(line) as GlobalStreamChunk
          if (parsed.type !== 'heartbeat') yield parsed
        } catch { /* ignore malformed heartbeat/line */ }
      }
      nl = buf.indexOf('\n')
    }
  }
  const rest = buf.trim()
  if (rest) {
    try {
      const parsed = JSON.parse(rest) as GlobalStreamChunk
      if (parsed.type !== 'heartbeat') yield parsed
    } catch { /* ignore */ }
  }
}

export function localizedItemName(
  item: { name_zh?: string; name_en?: string; name_ja?: string; name?: string; symbol: string },
  locale: Locale = getLocale(),
): string {
  if (locale === 'ja') return item.name_ja || item.name_en || item.name || item.symbol
  if (locale === 'en') return item.name_en || item.name || item.name_ja || item.symbol
  return item.name_zh || item.name || item.name_en || item.symbol
}

export const globalApi = {
  overview: (market: GlobalMarket) =>
    fetchJson<GlobalOverviewResponse>(`/api/global-analysis/overview?market=${encodeURIComponent(market)}`),

  search: (market: GlobalMarket, q: string) =>
    fetchJson<{ market: GlobalMarket; items: GlobalCatalogItem[] }>(
      `/api/global-analysis/search?market=${encodeURIComponent(market)}&q=${encodeURIComponent(q)}`,
    ),

  stock: (market: GlobalMarket, symbol: string, days = 250) =>
    fetchJson<GlobalStockDetailResponse>(
      `/api/global-analysis/stock?market=${encodeURIComponent(market)}&symbol=${encodeURIComponent(symbol)}&days=${days}`,
    ),

  analyzeMarketStream: (market: GlobalMarket, focus: string, locale: Locale = getLocale()) =>
    streamNdjson('/api/global-analysis/analyze/market', { market, focus, locale }),

  analyzeStockStream: (market: GlobalMarket, symbol: string, focus: string, locale: Locale = getLocale()) =>
    streamNdjson('/api/global-analysis/analyze/stock', { market, symbol, focus, locale }),

  listReports: () =>
    fetchJson<{ reports: GlobalReport[] }>('/api/global-analysis/reports'),

  saveReport: (payload: Omit<GlobalReport, 'id' | 'created_at'>) =>
    fetchJson<{ ok: boolean; report: GlobalReport }>('/api/global-analysis/reports', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  deleteReport: (id: string) =>
    fetchJson<{ ok: boolean }>(`/api/global-analysis/reports/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),
}

export function normalizeGlobalAiError(msg: string): string {
  if (msg.includes('API Key') || msg.includes('api_key') || msg.includes('未配置')) {
    return t('common.aiNotConfigured')
  }
  return friendlyStreamError(msg)
}
