import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  AlertTriangle,
  BarChart3,
  Check,
  Copy,
  Globe,
  History,
  LineChart,
  Loader2,
  Minimize2,
  Newspaper,
  RefreshCw,
  Search,
  Settings2,
  Sparkles,
  Trash2,
  TrendingDown,
  TrendingUp,
  X,
} from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { toast } from '@/components/Toast'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import { AnalysisKChart, type LevelType, type PriceLevel } from '@/components/stock-analysis/AnalysisKChart'
import { cn } from '@/lib/cn'
import { copyText } from '@/lib/clipboard'
import { formatRelativeTime, useI18n } from '@/lib/i18n'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'
import {
  globalApi,
  localizedItemName,
  type GlobalMarket,
  type GlobalQuoteItem,
  type GlobalReport,
} from './client'
import {
  closeGlobalDialog,
  deleteGlobalReport,
  loadGlobalHistory,
  minimizeGlobalDialog,
  openGlobalDialog,
  openGlobalHistoryReport,
  startGlobalAnalysis,
  useGlobalDialogState,
  useGlobalDialogTask,
  useGlobalHistory,
  useGlobalRunningTasks,
  type ActiveGlobalTask,
} from './store'

const DEFAULT_TICKER: Record<GlobalMarket, string> = {
  JP: '7203.T',
  US: 'NVDA',
}

function fmtQuotePrice(val: number | null | undefined, currency?: string) {
  if (val == null || !Number.isFinite(val)) return '—'
  const digits = currency === 'JPY' && val >= 100 ? 1 : 2
  return val.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

function fmtChangePct(val: number | null | undefined) {
  if (val == null || !Number.isFinite(val)) return '—'
  return `${val >= 0 ? '+' : ''}${val.toFixed(2)}%`
}

function pctTone(val: number | null | undefined) {
  if (val == null || !Number.isFinite(val) || val === 0) return 'text-muted'
  return val > 0 ? 'text-bull' : 'text-bear'
}

export function GlobalAnalysisPage() {
  const { locale, t } = useI18n()
  const [market, setMarket] = useState<GlobalMarket>('JP')
  const [tab, setTab] = useState<'market' | 'stock'>('market')
  const [selectedSymbol, setSelectedSymbol] = useState<Record<GlobalMarket, string>>(DEFAULT_TICKER)
  const [marketFocus, setMarketFocus] = useState('')
  const [stockFocus, setStockFocus] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [historyFilter, setHistoryFilter] = useState<'ALL' | GlobalMarket>('ALL')

  useEffect(() => {
    void loadGlobalHistory()
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedQuery(searchQuery.trim()), 260)
    return () => window.clearTimeout(timer)
  }, [searchQuery])

  const overviewQ = useQuery({
    queryKey: ['global-analysis', 'overview', market] as const,
    queryFn: () => globalApi.overview(market),
    staleTime: 60_000,
    retry: 1,
  })

  const activeSymbol = selectedSymbol[market]

  const stockQ = useQuery({
    queryKey: ['global-analysis', 'stock', market, activeSymbol] as const,
    queryFn: () => globalApi.stock(market, activeSymbol, 250),
    enabled: tab === 'stock' && !!activeSymbol,
    staleTime: 60_000,
    retry: 1,
  })

  const searchQ = useQuery({
    queryKey: ['global-analysis', 'search', market, debouncedQuery] as const,
    queryFn: () => globalApi.search(market, debouncedQuery),
    enabled: tab === 'stock' && debouncedQuery.length > 0,
    staleTime: 120_000,
  })

  const runningTasks = useGlobalRunningTasks()
  const { minimized } = useGlobalDialogState()

  const selectStock = (sym: string) => {
    setSelectedSymbol(prev => ({ ...prev, [market]: sym }))
    setTab('stock')
  }

  const handleAnalyzeMarket = async () => {
    const res = await startGlobalAnalysis({
      market,
      kind: 'market',
      symbol: market,
      name: market === 'JP' ? t('global.marketJP') : t('global.marketUS'),
      focus: marketFocus.trim(),
    })
    if (res.error) toast(res.error, 'error')
  }

  const handleAnalyzeStock = async () => {
    if (!activeSymbol) return
    const q = stockQ.data?.quote
    const displayName = q ? localizedItemName(q, locale) : activeSymbol
    const res = await startGlobalAnalysis({
      market,
      kind: 'stock',
      symbol: activeSymbol,
      name: displayName,
      focus: stockFocus.trim(),
    })
    if (res.error) toast(res.error, 'error')
  }

  const marketLabel = market === 'JP' ? t('global.marketJP') : t('global.marketUS')

  return (
    <>
      <PageHeader
        title={t('global.title')}
        subtitle={t('global.subtitle')}
        right={
          <div className="flex items-center gap-2">
            {/* Market switcher */}
            <div className="inline-flex rounded-btn border border-border bg-base p-0.5">
              {(['JP', 'US'] as const).map(m => {
                const active = market === m
                return (
                  <button
                    key={m}
                    type="button"
                    onClick={() => { setMarket(m); setSearchQuery('') }}
                    className={cn(
                      'inline-flex items-center gap-1.5 rounded-[5px] px-3 py-1 text-xs font-medium transition-colors cursor-pointer',
                      active
                        ? 'bg-accent text-white shadow-sm'
                        : 'text-secondary hover:text-foreground hover:bg-elevated/60',
                    )}
                  >
                    <Globe className="h-3.5 w-3.5" />
                    <span>{m === 'JP' ? t('global.marketJP') : t('global.marketUS')}</span>
                  </button>
                )
              })}
            </div>
            <button
              type="button"
              onClick={() => {
                void overviewQ.refetch()
                if (tab === 'stock' && activeSymbol) void stockQ.refetch()
              }}
              className="inline-flex items-center gap-1.5 h-8 px-2.5 rounded-btn border border-border bg-surface text-xs text-secondary hover:text-foreground hover:bg-elevated transition-colors cursor-pointer"
              title={t('common.refresh')}
            >
              <RefreshCw className={cn('h-3.5 w-3.5', (overviewQ.isFetching || stockQ.isFetching) && 'animate-spin')} />
              <span>{t('common.refresh')}</span>
            </button>
          </div>
        }
      />

      <div className="w-full px-6 py-5 lg:px-8 space-y-5">
        {/* Sub-mode tabs + active running indicator */}
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="inline-flex rounded-btn border border-border bg-surface p-1 gap-1">
            <button
              type="button"
              onClick={() => setTab('market')}
              className={cn(
                'inline-flex items-center gap-1.5 rounded-btn px-3 py-1.5 text-xs font-medium transition-colors cursor-pointer',
                tab === 'market' ? 'bg-accent/15 text-accent' : 'text-secondary hover:text-foreground',
              )}
            >
              <BarChart3 className="h-3.5 w-3.5" />
              <span>{t('global.modeMarket')}</span>
            </button>
            <button
              type="button"
              onClick={() => setTab('stock')}
              className={cn(
                'inline-flex items-center gap-1.5 rounded-btn px-3 py-1.5 text-xs font-medium transition-colors cursor-pointer',
                tab === 'stock' ? 'bg-accent/15 text-accent' : 'text-secondary hover:text-foreground',
              )}
            >
              <LineChart className="h-3.5 w-3.5" />
              <span>{t('global.modeStock')}</span>
            </button>
          </div>

          {runningTasks.length > 0 && minimized && (
            <div className="flex items-center gap-2">
              {runningTasks.map(task => (
                <button
                  key={task.id}
                  type="button"
                  onClick={() => openGlobalDialog(task.id)}
                  className="inline-flex items-center gap-1.5 rounded-full border border-sky-400/30 bg-sky-500/15 px-3 py-1 text-xs text-sky-300 hover:bg-sky-500/25 transition-colors cursor-pointer"
                >
                  <Loader2 className="h-3 w-3 animate-spin" />
                  <span>{task.market} · {task.name || task.symbol}</span>
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Main 2-column grid */}
        <div className="grid grid-cols-1 xl:grid-cols-[1fr_300px] gap-6 items-start">
          <div className="min-w-0 space-y-5">
            {tab === 'market' ? (
              <MarketOverviewPanel
                market={market}
                marketLabel={marketLabel}
                overviewQ={overviewQ}
                focus={marketFocus}
                onFocusChange={setMarketFocus}
                onAnalyze={handleAnalyzeMarket}
                onSelectSymbol={selectStock}
              />
            ) : (
              <StockAnalysisPanel
                market={market}
                symbol={activeSymbol}
                catalogStocks={overviewQ.data?.catalog_stocks ?? []}
                searchQuery={searchQuery}
                onSearchQueryChange={setSearchQuery}
                searchItems={searchQ.data?.items}
                searching={searchQ.isFetching}
                stockQ={stockQ}
                focus={stockFocus}
                onFocusChange={setStockFocus}
                onSelectSymbol={sym => {
                  setSelectedSymbol(prev => ({ ...prev, [market]: sym }))
                  setSearchQuery('')
                }}
                onAnalyze={handleAnalyzeStock}
              />
            )}
          </div>

          <GlobalHistorySidebar
            filter={historyFilter}
            onFilterChange={setHistoryFilter}
          />
        </div>
      </div>

      <GlobalReportDialog />
    </>
  )
}

function MarketOverviewPanel({
  market,
  marketLabel,
  overviewQ,
  focus,
  onFocusChange,
  onAnalyze,
  onSelectSymbol,
}: {
  market: GlobalMarket
  marketLabel: string
  overviewQ: ReturnType<typeof useQuery<Awaited<ReturnType<typeof globalApi.overview>>>>
  focus: string
  onFocusChange: (v: string) => void
  onAnalyze: () => void
  onSelectSymbol: (sym: string) => void
}) {
  const { locale, t } = useI18n()

  return (
    <div className="space-y-5">
      {/* AI Market Analysis Action Bar */}
      <div className="rounded-card border border-accent/25 bg-gradient-to-r from-accent/[0.08] via-sky-500/[0.04] to-transparent p-4">
        <div className="flex flex-col sm:flex-row sm:items-center gap-3">
          <div className="flex-1 min-w-0">
            <div className="text-xs font-medium text-foreground mb-1.5 flex items-center gap-1.5">
              <Sparkles className="h-3.5 w-3.5 text-accent" />
              <span>{marketLabel} · {market === 'JP' ? t('global.marketJPSub') : t('global.marketUSSub')}</span>
              {overviewQ.data?.as_of && (
                <span className="ml-auto text-[10px] font-mono text-muted">
                  {t('global.asOf')}: {overviewQ.data.as_of}
                </span>
              )}
            </div>
            <input
              type="text"
              value={focus}
              onChange={e => onFocusChange(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') onAnalyze() }}
              placeholder={t('global.marketFocusPlaceholder')}
              className="w-full h-9 rounded-btn border border-border bg-base/80 px-3 text-xs text-foreground placeholder:text-muted/60 outline-none focus:border-accent/50"
            />
          </div>
          <button
            type="button"
            onClick={onAnalyze}
            disabled={overviewQ.isLoading || overviewQ.isError}
            className="inline-flex items-center justify-center gap-1.5 h-9 px-4 rounded-btn bg-accent text-xs font-medium text-white hover:bg-accent/90 transition-colors disabled:opacity-40 cursor-pointer shrink-0 sm:self-end"
          >
            <Sparkles className="h-3.5 w-3.5" />
            <span>{t('global.aiMarketBtn')}</span>
          </button>
        </div>
      </div>

      {overviewQ.isLoading ? (
        <div className="flex flex-col items-center justify-center py-20 gap-2 text-muted">
          <Loader2 className="h-5 w-5 animate-spin" />
          <span className="text-xs">{t('global.loadingOverview', { market: marketLabel })}</span>
        </div>
      ) : overviewQ.isError || !overviewQ.data ? (
        <EmptyState
          icon={AlertTriangle}
          title={t('global.overviewError')}
          hint={(overviewQ.error as Error | undefined)?.message || ''}
        />
      ) : (
        <>
          {/* Benchmarks & Macro */}
          <section className="space-y-2.5">
            <h2 className="text-xs font-semibold text-secondary uppercase tracking-wider">
              {t('global.benchmarksTitle')}
            </h2>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
              {overviewQ.data.benchmarks.map(item => (
                <QuoteCard
                  key={item.symbol}
                  item={item}
                  name={localizedItemName(item, locale)}
                  onClick={() => onSelectSymbol(item.symbol)}
                />
              ))}
            </div>
          </section>

          {/* Sectors */}
          <section className="space-y-2.5">
            <h2 className="text-xs font-semibold text-secondary uppercase tracking-wider">
              {t('global.sectorsTitle')}
            </h2>
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2.5">
              {overviewQ.data.sectors.map(item => (
                <QuoteCard
                  key={item.symbol}
                  item={item}
                  name={localizedItemName(item, locale)}
                  compact
                  onClick={() => onSelectSymbol(item.symbol)}
                />
              ))}
            </div>
          </section>

          {/* Leaders + News */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <section className="rounded-card border border-border/60 bg-surface/50 p-4 space-y-2.5">
              <h2 className="text-xs font-semibold text-secondary uppercase tracking-wider">
                {t('global.leadersTitle')}
              </h2>
              <div className="divide-y divide-border/40">
                {overviewQ.data.leaders.map(item => (
                  <button
                    key={item.symbol}
                    type="button"
                    onClick={() => onSelectSymbol(item.symbol)}
                    className="w-full flex items-center justify-between gap-3 py-2 px-1.5 rounded hover:bg-elevated/50 transition-colors text-left cursor-pointer"
                  >
                    <div className="min-w-0">
                      <div className="text-xs font-medium text-foreground truncate">
                        {localizedItemName(item, locale)}
                      </div>
                      <div className="text-[10px] font-mono text-muted">{item.symbol}</div>
                    </div>
                    <div className="text-right shrink-0">
                      <div className="text-xs font-mono text-foreground">
                        {fmtQuotePrice(item.price, item.currency)} <span className="text-[10px] text-muted">{item.currency}</span>
                      </div>
                      <div className={cn('text-[11px] font-mono font-medium', pctTone(item.change_pct))}>
                        {fmtChangePct(item.change_pct)}
                      </div>
                    </div>
                  </button>
                ))}
              </div>
            </section>

            <NewsListCard title={t('global.newsTitle')} news={overviewQ.data.news} />
          </div>
        </>
      )}
    </div>
  )
}

function QuoteCard({
  item,
  name,
  compact,
  onClick,
}: {
  item: GlobalQuoteItem
  name: string
  compact?: boolean
  onClick: () => void
}) {
  const up = (item.change_pct ?? 0) > 0
  const down = (item.change_pct ?? 0) < 0
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'text-left rounded-card border border-border/60 bg-surface/60 hover:border-accent/40 hover:bg-elevated/40 transition-all cursor-pointer',
        compact ? 'p-3' : 'p-3.5',
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium text-foreground truncate">{name}</span>
        <span className="text-[10px] font-mono text-muted shrink-0">{item.symbol}</span>
      </div>
      <div className="mt-2 flex items-baseline justify-between gap-2">
        <span className={cn('font-mono font-semibold text-foreground', compact ? 'text-sm' : 'text-base')}>
          {fmtQuotePrice(item.price, item.currency)}
        </span>
        <span className={cn('inline-flex items-center gap-0.5 text-xs font-mono font-medium', pctTone(item.change_pct))}>
          {up && <TrendingUp className="h-3 w-3" />}
          {down && <TrendingDown className="h-3 w-3" />}
          {fmtChangePct(item.change_pct)}
        </span>
      </div>
    </button>
  )
}

function StockAnalysisPanel({
  market,
  symbol,
  catalogStocks,
  searchQuery,
  onSearchQueryChange,
  searchItems,
  searching,
  stockQ,
  focus,
  onFocusChange,
  onSelectSymbol,
  onAnalyze,
}: {
  market: GlobalMarket
  symbol: string
  catalogStocks: { symbol: string; name_zh: string; name_en: string; name_ja: string; category: string }[]
  searchQuery: string
  onSearchQueryChange: (q: string) => void
  searchItems?: { symbol: string; name_zh: string; name_en: string; name_ja: string; category: string; exchange?: string }[]
  searching: boolean
  stockQ: ReturnType<typeof useQuery<Awaited<ReturnType<typeof globalApi.stock>>>>
  focus: string
  onFocusChange: (v: string) => void
  onSelectSymbol: (sym: string) => void
  onAnalyze: () => void
}) {
  const { locale, t } = useI18n()

  const detail = stockQ.data
  const q = detail?.quote
  const levels = (detail?.levels ?? {}) as Record<LevelType, PriceLevel[]>

  return (
    <div className="space-y-4">
      {/* Search + Popular Chips */}
      <div className="rounded-card border border-border/60 bg-surface/50 p-4 space-y-3">
        <div className="flex flex-col sm:flex-row gap-2.5">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted" />
            <input
              type="text"
              value={searchQuery}
              onChange={e => onSearchQueryChange(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' && searchQuery.trim()) {
                  onSelectSymbol(searchQuery.trim().toUpperCase())
                }
              }}
              placeholder={t('global.searchPlaceholder')}
              className="w-full h-9 rounded-btn border border-border bg-base pl-8 pr-8 text-xs text-foreground placeholder:text-muted/60 outline-none focus:border-accent/50"
            />
            {searching && <Loader2 className="absolute right-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 animate-spin text-muted" />}
            {searchQuery.trim().length > 0 && searchItems && searchItems.length > 0 && (
              <div className="absolute left-0 right-0 top-10 z-30 max-h-64 overflow-y-auto rounded-lg border border-border bg-surface shadow-2xl divide-y divide-border/40">
                {searchItems.map(item => (
                  <button
                    key={item.symbol}
                    type="button"
                    onClick={() => onSelectSymbol(item.symbol)}
                    className="w-full flex items-center justify-between gap-2 px-3 py-2 text-left hover:bg-elevated transition-colors cursor-pointer"
                  >
                    <span className="text-xs font-medium text-foreground truncate">
                      {localizedItemName(item, locale)}
                    </span>
                    <span className="text-[10px] font-mono text-muted shrink-0">
                      {item.symbol} {item.exchange ? `· ${item.exchange}` : ''}
                    </span>
                  </button>
                ))}
              </div>
            )}
          </div>

          <input
            type="text"
            value={focus}
            onChange={e => onFocusChange(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') onAnalyze() }}
            placeholder={t('global.stockFocusPlaceholder')}
            className="sm:w-72 h-9 rounded-btn border border-border bg-base px-3 text-xs text-foreground placeholder:text-muted/60 outline-none focus:border-accent/50"
          />

          <button
            type="button"
            onClick={onAnalyze}
            disabled={!symbol || stockQ.isLoading || stockQ.isError}
            className="inline-flex items-center justify-center gap-1.5 h-9 px-4 rounded-btn bg-gradient-to-r from-sky-500/80 to-blue-600/80 text-xs font-medium text-white hover:from-sky-500 hover:to-blue-600 transition-all disabled:opacity-40 cursor-pointer shrink-0"
          >
            <Sparkles className="h-3.5 w-3.5" />
            <span>{t('global.aiStockBtn')}</span>
          </button>
        </div>

        {/* Quick pick chips */}
        {catalogStocks.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] text-muted mr-1">{t('global.popularStocks')}:</span>
            {catalogStocks.map(item => {
              const active = item.symbol === symbol
              return (
                <button
                  key={item.symbol}
                  type="button"
                  onClick={() => onSelectSymbol(item.symbol)}
                  className={cn(
                    'inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] transition-colors cursor-pointer',
                    active
                      ? 'border-sky-400/50 bg-sky-500/15 text-sky-300 font-medium'
                      : 'border-border/60 bg-base/50 text-secondary hover:text-foreground hover:border-border',
                  )}
                >
                  <span>{localizedItemName(item, locale)}</span>
                  <span className="font-mono text-[10px] opacity-70">{item.symbol}</span>
                </button>
              )
            })}
          </div>
        )}
      </div>

      {!symbol ? (
        <EmptyState icon={LineChart} title={t('global.modeStock')} hint={t('global.selectStockHint')} />
      ) : stockQ.isLoading ? (
        <div className="flex flex-col items-center justify-center py-20 gap-2 text-muted">
          <Loader2 className="h-5 w-5 animate-spin" />
          <span className="text-xs">{t('global.loadingStock')}</span>
        </div>
      ) : stockQ.isError || !detail || !q ? (
        <EmptyState
          icon={AlertTriangle}
          title={t('global.stockError')}
          hint={(stockQ.error as Error | undefined)?.message || ''}
        />
      ) : (
        <>
          {/* Stock Summary Banner */}
          <div className="rounded-card border border-border/60 bg-surface/50 p-4">
            <div className="flex flex-wrap items-baseline justify-between gap-4">
              <div>
                <div className="flex items-center gap-2">
                  <span className="rounded bg-accent/15 px-1.5 py-0.5 text-[10px] font-mono font-semibold text-accent">
                    {market}
                  </span>
                  <h2 className="text-base font-semibold text-foreground">
                    {localizedItemName(q, locale)}
                  </h2>
                  <span className="text-xs font-mono text-muted">{detail.symbol}</span>
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted">
                  {q.exchange && <span>{t('global.exchange')}: {q.exchange}</span>}
                  {q.sector && <span>{t('global.sector')}: {q.sector}{q.industry ? ` · ${q.industry}` : ''}</span>}
                  {q.as_of && <span>{t('global.asOf')}: {q.as_of}</span>}
                </div>
              </div>
              <div className="text-right">
                <div className="flex items-baseline justify-end gap-2">
                  <span className={cn('text-2xl font-mono font-bold', pctTone(q.change_pct))}>
                    {fmtQuotePrice(q.price, q.currency)}
                  </span>
                  <span className="text-xs font-mono text-muted">{q.currency}</span>
                </div>
                <div className={cn('text-xs font-mono font-medium', pctTone(q.change_pct))}>
                  {q.change != null ? `${q.change >= 0 ? '+' : ''}${fmtQuotePrice(q.change, q.currency)} ` : ''}
                  ({fmtChangePct(q.change_pct)})
                </div>
              </div>
            </div>

            <div className="mt-3 pt-3 border-t border-border/40 grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
              <div>
                <div className="text-[10px] text-muted">{t('global.prevClose')}</div>
                <div className="font-mono text-foreground">{fmtQuotePrice(q.prev_close, q.currency)}</div>
              </div>
              <div>
                <div className="text-[10px] text-muted">{t('global.price52w')}</div>
                <div className="font-mono text-foreground">
                  {fmtQuotePrice(q.low_52w, q.currency)} – {fmtQuotePrice(q.high_52w, q.currency)}
                </div>
              </div>
              <div>
                <div className="text-[10px] text-muted">{t('global.volume')}</div>
                <div className="font-mono text-foreground">
                  {q.volume != null ? q.volume.toLocaleString() : '—'}
                </div>
              </div>
              <div>
                <div className="text-[10px] text-muted">{t('global.keyLevelsTitle')}</div>
                <div className="font-mono text-foreground">{t('common.daysCount', { n: detail.rows.length })}</div>
              </div>
            </div>
          </div>

          {/* Interactive K-line + Key Levels */}
          <div className="rounded-card border border-border/60 bg-surface/40 overflow-hidden">
            <div className="px-4 py-3 border-b border-border/40 flex items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <LineChart className="h-4 w-4 text-sky-400" />
                <span className="text-sm font-medium text-foreground">{t('global.keyLevelsTitle')}</span>
              </div>
              <span className="text-[11px] font-mono text-muted">
                {t('common.daysCount', { n: detail.rows.length })}
              </span>
            </div>
            <div className="p-3">
              <AnalysisKChart
                rows={detail.rows}
                levels={levels}
                series={detail.series}
                seriesDates={detail.dates}
                defaultLevelTypes={['sr', 'pivot', 'keltner_s']}
                scopeKey={`${market}:${detail.symbol}`}
                height={440}
              />
            </div>
          </div>

          <NewsListCard title={t('global.stockNewsTitle')} news={detail.news} />
        </>
      )}
    </div>
  )
}

function NewsListCard({ title, news }: { title: string; news: { title: string; publisher: string; link: string; published_at: string | null; scope?: 'ticker' | 'market' }[] }) {
  const { locale, t } = useI18n()
  return (
    <section className="rounded-card border border-border/60 bg-surface/50 p-4 space-y-2.5">
      <div className="flex items-center gap-1.5">
        <Newspaper className="h-3.5 w-3.5 text-accent" />
        <h2 className="text-xs font-semibold text-secondary uppercase tracking-wider">{title}</h2>
      </div>
      {news.length === 0 ? (
        <p className="text-xs text-muted py-4">{t('global.noNews')}</p>
      ) : (
        <div className="divide-y divide-border/40">
          {news.map((item, idx) => (
            <div key={idx} className="py-2">
              {item.link ? (
                <a
                  href={item.link}
                  target="_blank"
                  rel="noreferrer"
                  className="text-xs text-foreground hover:text-accent transition-colors leading-relaxed block"
                >
                  {item.title}
                </a>
              ) : (
                <div className="text-xs text-foreground leading-relaxed">{item.title}</div>
              )}
              <div className="mt-0.5 flex items-center gap-2 text-[10px] text-muted">
                {item.scope && (
                  <span
                    className={cn(
                      'rounded px-1 py-0.5 border',
                      item.scope === 'market'
                        ? 'border-border text-muted'
                        : 'border-accent/40 text-accent',
                    )}
                  >
                    {item.scope === 'market' ? t('global.newsScopeMarket') : t('global.newsScopeTicker')}
                  </span>
                )}
                {item.publisher && <span>{item.publisher}</span>}
                {item.published_at && <span>{formatRelativeTime(item.published_at, locale)}</span>}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

function GlobalHistorySidebar({
  filter,
  onFilterChange,
}: {
  filter: 'ALL' | GlobalMarket
  onFilterChange: (f: 'ALL' | GlobalMarket) => void
}) {
  const { locale, t } = useI18n()
  const { reports, loaded } = useGlobalHistory()

  const filtered = useMemo(
    () => (filter === 'ALL' ? reports : reports.filter(r => r.market === filter)),
    [reports, filter],
  )

  return (
    <aside className="self-start xl:sticky xl:top-4">
      <div className="rounded-card border border-border/60 bg-surface/40 overflow-hidden">
        <div className="px-3 py-2.5 border-b border-border/40 flex items-center gap-2">
          <History className="h-3.5 w-3.5 text-sky-400 shrink-0" />
          <span className="text-xs font-medium text-foreground">{t('global.historyTitle')}</span>
          {loaded && (
            <span className="ml-auto text-[10px] text-muted">{filtered.length}</span>
          )}
        </div>

        <div className="px-2.5 pt-2 flex items-center gap-1">
          {(['ALL', 'JP', 'US'] as const).map(f => (
            <button
              key={f}
              type="button"
              onClick={() => onFilterChange(f)}
              className={cn(
                'px-2 py-0.5 rounded text-[10px] font-medium transition-colors cursor-pointer',
                filter === f ? 'bg-accent/15 text-accent' : 'text-muted hover:text-foreground',
              )}
            >
              {f === 'ALL' ? t('global.filterAll') : f}
            </button>
          ))}
        </div>

        {!loaded ? (
          <div className="flex items-center justify-center py-14">
            <Loader2 className="h-4 w-4 animate-spin text-muted" />
          </div>
        ) : filtered.length === 0 ? (
          <div className="px-3 py-10 text-center">
            <p className="text-xs text-muted">{t('global.noReports')}</p>
            <p className="text-[10px] text-muted/60 mt-1 leading-relaxed">{t('global.noReportsHint')}</p>
          </div>
        ) : (
          <div className="max-h-[calc(100vh-250px)] overflow-y-auto p-2 space-y-1.5">
            {filtered.map(r => (
              <div
                key={r.id}
                className="group rounded-lg border border-border/40 bg-elevated/20 p-2.5 hover:border-border hover:bg-elevated/40 transition-colors"
              >
                <div className="flex items-start justify-between gap-2">
                  <button
                    type="button"
                    onClick={() => openGlobalHistoryReport(r.id)}
                    className="flex-1 text-left min-w-0 cursor-pointer"
                  >
                    <div className="flex items-center gap-1.5 min-w-0">
                      <span className="rounded bg-accent/15 px-1 py-0.5 text-[9px] font-mono font-semibold text-accent shrink-0">
                        {r.market}
                      </span>
                      <span className="rounded bg-elevated px-1 py-0.5 text-[9px] text-muted shrink-0">
                        {r.kind === 'market' ? t('global.kindMarket') : t('global.kindStock')}
                      </span>
                      <span className="text-xs font-medium text-foreground truncate">
                        {r.name || r.symbol}
                      </span>
                    </div>
                    <div className="mt-1 flex items-center gap-2 text-[10px] text-muted">
                      <span>{formatRelativeTime(r.created_at, locale)}</span>
                      {r.close != null && (
                        <span className="font-mono">
                          {fmtQuotePrice(r.close, r.currency)} {r.currency || ''}
                        </span>
                      )}
                    </div>
                    {r.focus && (
                      <div className="mt-0.5 text-[10px] text-sky-300/80 truncate">
                        {t('common.focusLabel')}: {r.focus}
                      </div>
                    )}
                    {r.summary && (
                      <div className="mt-0.5 text-[10px] text-muted truncate">{r.summary}</div>
                    )}
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      void deleteGlobalReport(r.id)
                      toast(t('common.deleted'), 'success')
                    }}
                    className="shrink-0 p-1 text-muted/60 hover:text-danger transition-colors opacity-0 group-hover:opacity-100 cursor-pointer"
                    title={t('common.delete')}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </aside>
  )
}

function GlobalReportDialog() {
  const { locale, t } = useI18n()
  const { minimized } = useGlobalDialogState()
  const { task, mode } = useGlobalDialogTask()
  const [copied, setCopied] = useState(false)

  const open = !!task && !minimized
  const phase = task && 'phase' in task ? (task as ActiveGlobalTask).phase : 'done'
  const isWorking = phase === 'loading' || phase === 'streaming'
  const isHistory = mode === 'history'
  const backdrop = useDialogBackdrop(closeGlobalDialog, () => !isWorking)

  if (!open || !task) return null

  const content = task.content ?? ''
  const error = 'error' in task ? (task as ActiveGlobalTask).error : ''
  const summary = 'meta' in task ? (task as ActiveGlobalTask).meta?.summary : (task as GlobalReport).summary
  const title = isHistory
    ? t('global.reportDialogHistory')
    : task.kind === 'market'
      ? t('global.reportDialogMarket')
      : t('global.reportDialogStock')

  const handleCopy = async () => {
    if (!content) return
    const ok = await copyText(content)
    if (!ok) {
      toast(t('common.copyFailed'), 'error')
      return
    }
    setCopied(true)
    toast(t('common.copied'), 'success')
    window.setTimeout(() => setCopied(false), 2000)
  }

  const handleRetry = () => {
    void startGlobalAnalysis({
      market: task.market,
      kind: task.kind,
      symbol: task.symbol,
      name: task.name,
      focus: task.focus,
    })
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 backdrop-blur-sm p-4"
      {...backdrop}
    >
      <div
        onClick={e => e.stopPropagation()}
        className="w-full max-w-3xl max-h-[88vh] bg-surface/95 border border-border rounded-2xl shadow-2xl flex flex-col overflow-hidden"
      >
        <div className="px-5 py-3.5 border-b border-border/60 bg-gradient-to-r from-accent/[0.08] via-sky-500/[0.04] to-transparent flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-accent/15 border border-accent/30 shrink-0">
            <Globe className="h-4.5 w-4.5 text-accent" />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="rounded bg-accent/15 px-1.5 py-0.5 text-[10px] font-mono font-semibold text-accent">
                {task.market}
              </span>
              <span className="text-sm font-semibold text-foreground truncate">{title}</span>
              <span className="text-xs text-secondary truncate">{task.name}</span>
              {task.kind === 'stock' && (
                <span className="text-[10px] font-mono text-muted shrink-0">{task.symbol}</span>
              )}
            </div>
            <div className="flex items-center gap-2 mt-0.5 text-[10px] text-muted">
              {summary && <span className="truncate">{summary}</span>}
              {phase === 'streaming' && (
                <span className="inline-flex items-center gap-1 text-sky-300 shrink-0">
                  <span className="h-1.5 w-1.5 rounded-full bg-sky-400 animate-pulse" />
                  {t('common.generating')}
                </span>
              )}
              {isHistory && 'created_at' in task && (
                <span className="shrink-0">{formatRelativeTime((task as GlobalReport).created_at, locale)}</span>
              )}
            </div>
          </div>
          <div className="flex items-center gap-1 shrink-0">
            {content && !isWorking && (
              <button
                type="button"
                onClick={handleCopy}
                title={t('common.copyFull')}
                className="p-1.5 rounded-lg hover:bg-elevated text-muted hover:text-foreground transition-colors cursor-pointer"
              >
                {copied ? <Check className="h-4 w-4 text-emerald-400" /> : <Copy className="h-4 w-4" />}
              </button>
            )}
            {!isHistory && isWorking && (
              <button
                type="button"
                onClick={minimizeGlobalDialog}
                title={t('common.minimizeBubble')}
                className="p-1.5 rounded-lg hover:bg-elevated text-muted hover:text-foreground transition-colors cursor-pointer"
              >
                <Minimize2 className="h-4 w-4" />
              </button>
            )}
            {(!isWorking || isHistory) && (
              <button
                type="button"
                onClick={closeGlobalDialog}
                title={t('common.close')}
                className="p-1.5 rounded-lg hover:bg-elevated text-muted hover:text-foreground transition-colors cursor-pointer"
              >
                <X className="h-4 w-4" />
              </button>
            )}
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4 min-h-[280px]">
          {phase === 'loading' && !content && (
            <div className="flex flex-col items-center justify-center py-16 gap-3">
              <Loader2 className="h-8 w-8 animate-spin text-accent" />
              <div className="text-xs text-secondary">
                {task.kind === 'market'
                  ? t('global.analyzingMarket', { market: task.market })
                  : t('global.analyzingStock', { symbol: task.symbol })}
              </div>
            </div>
          )}

          {phase === 'error' && (
            <div className="flex flex-col items-center justify-center py-14 gap-3">
              <div className="h-11 w-11 rounded-full bg-danger/10 flex items-center justify-center">
                <AlertTriangle className="h-5 w-5 text-danger" />
              </div>
              <div className="text-sm font-medium text-foreground">{t('common.analysisFailed')}</div>
              <div className="text-xs text-secondary text-center max-w-md px-4">{error}</div>
              <div className="flex items-center gap-2 mt-1">
                {error.toLowerCase().includes('ai') && (
                  <button
                    type="button"
                    onClick={() => { window.location.href = `${import.meta.env.BASE_URL}settings?tab=ai` }}
                    className="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg bg-elevated border border-border text-xs text-secondary hover:text-foreground transition-colors cursor-pointer"
                  >
                    <Settings2 className="h-3.5 w-3.5" />
                    {t('common.goToAiConfig')}
                  </button>
                )}
                <button
                  type="button"
                  onClick={handleRetry}
                  className="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg bg-accent/15 border border-accent/30 text-xs text-accent hover:bg-accent/25 transition-colors cursor-pointer"
                >
                  <RefreshCw className="h-3.5 w-3.5" />
                  {t('common.retry')}
                </button>
              </div>
            </div>
          )}

          {(content || phase === 'streaming') && (
            <div className="relative">
              <MarkdownRenderer content={content} />
              {phase === 'streaming' && (
                <span className="inline-block w-1.5 h-3.5 bg-accent ml-0.5 align-middle animate-pulse rounded-sm" />
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
