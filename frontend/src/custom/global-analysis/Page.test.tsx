// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

vi.mock('@/components/stock-analysis/AnalysisKChart', () => ({
  AnalysisKChart: ({ rows }: { rows: unknown[] }) => <div>MockChart({rows.length})</div>,
}))
vi.mock('@/components/financials/MarkdownRenderer', () => ({
  MarkdownRenderer: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))

let host: HTMLDivElement
let root: Root | undefined
let fetchCalls: { url: string; init?: RequestInit }[] = []

function makeOverviewResponse(market: 'JP' | 'US') {
  if (market === 'JP') {
    return {
      market: 'JP',
      as_of: '2026-11-17',
      benchmarks: [
        {
          symbol: '^N225',
          name_en: 'Nikkei 225',
          name_zh: '日经225',
          name_ja: '日経平均株価',
          category: 'benchmark',
          exchange: 'OSA',
          price: 39250.5,
          prev_close: 38900.0,
          change: 350.5,
          change_pct: 0.9,
          currency: 'JPY',
          as_of: '2026-11-17',
        },
      ],
      sectors: [
        {
          symbol: '1306.T',
          name_en: 'TOPIX ETF',
          name_zh: 'TOPIX ETF',
          name_ja: 'TOPIX連動ETF',
          category: 'sector',
          exchange: 'TYO',
          price: 2750,
          prev_close: 2720,
          change: 30,
          change_pct: 1.1,
          currency: 'JPY',
          as_of: '2026-11-17',
        },
      ],
      leaders: [
        {
          symbol: '7203.T',
          name_en: 'Toyota Motor',
          name_zh: '丰田汽车',
          name_ja: 'トヨタ自動車',
          category: 'stock',
          exchange: 'TYO',
          price: 2680,
          prev_close: 2630,
          change: 50,
          change_pct: 1.9,
          currency: 'JPY',
          as_of: '2026-11-17',
        },
      ],
      news: [],
      catalog_stocks: [
        { symbol: '7203.T', name_en: 'Toyota Motor', name_zh: '丰田汽车', name_ja: 'トヨタ自動車', category: 'stock', exchange: 'TYO' },
      ],
    }
  }
  return {
    market: 'US',
    as_of: '2026-11-16',
    benchmarks: [
      {
        symbol: '^GSPC',
        name_en: 'S&P 500',
        name_zh: '标普500',
        name_ja: 'S&P 500',
        category: 'benchmark',
        exchange: 'SNP',
        price: 5950.25,
        prev_close: 5900.0,
        change: 50.25,
        change_pct: 0.85,
        currency: 'USD',
        as_of: '2026-11-16',
      },
    ],
    sectors: [
      {
        symbol: 'XLK',
        name_en: 'Technology Select',
        name_zh: '科技板块 XLK',
        name_ja: 'テクノロジー XLK',
        category: 'sector',
        exchange: 'PCX',
        price: 235.4,
        prev_close: 232.1,
        change: 3.3,
        change_pct: 1.42,
        currency: 'USD',
        as_of: '2026-11-16',
      },
    ],
    leaders: [
      {
        symbol: 'NVDA',
        name_en: 'NVIDIA Corp',
        name_zh: '英伟达',
        name_ja: 'エヌビディア',
        category: 'stock',
        exchange: 'NMS',
        price: 148.5,
        prev_close: 145.0,
        change: 3.5,
        change_pct: 2.41,
        currency: 'USD',
        as_of: '2026-11-16',
      },
    ],
    news: [],
    catalog_stocks: [
      { symbol: 'NVDA', name_en: 'NVIDIA Corp', name_zh: '英伟达', name_ja: 'エヌビディア', category: 'stock', exchange: 'NMS' },
    ],
  }
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetModules()
  localStorage.clear()
  document.documentElement.lang = 'zh-CN'
  fetchCalls = []

  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString()
    fetchCalls.push({ url, init })

    if (url.includes('/api/ui-prefs')) {
      return Response.json({ ui_prefs: {} })
    }
    if (url.includes('/api/global-analysis/overview')) {
      const market = url.includes('market=US') ? 'US' : 'JP'
      return Response.json(makeOverviewResponse(market))
    }
    if (url.includes('/api/global-analysis/reports')) {
      return Response.json({ reports: [] })
    }
    if (url.includes('/api/global-analysis/stock')) {
      return Response.json({
        market: 'US',
        symbol: 'NVDA',
        as_of: '2026-11-16',
        quote: {
          ...makeOverviewResponse('US').leaders[0],
          name: 'NVIDIA Corp',
          long_name: 'NVIDIA Corporation',
          high_52w: 152,
          low_52w: 48,
          day_high: 149,
          day_low: 144.5,
          volume: 1200000,
          sector: 'Technology',
          industry: 'Semiconductors',
        },
        rows: [
          {
            date: '2026-11-15',
            open: 144,
            high: 146,
            low: 143,
            close: 145,
            volume: 1000000,
          },
          {
            date: '2026-11-16',
            open: 145,
            high: 149,
            low: 144.5,
            close: 148.5,
            volume: 1200000,
          },
        ],
        levels: { sr: [], pivot: [] },
        series: {},
        series_dates: ['2026-11-15', '2026-11-16'],
      })
    }
    if (url.includes('/api/global-analysis/analyze/market')) {
      const encoder = new TextEncoder()
      const ndjson = [
        JSON.stringify({ type: 'meta', market: 'US', kind: 'market', symbol: 'US', name: 'US Market', as_of: '2026-11-16', model: 'mock-gpt', elapsed_s: 0.2 }),
        JSON.stringify({ type: 'delta', content: '## US Market Summary\nStrong breadth in mega-cap tech.' }),
        JSON.stringify({ type: 'done', id: 'gar-1', created_at: '2026-11-17T00:00:00Z' }),
      ].join('\n') + '\n'
      return new Response(new ReadableStream({
        start(controller) {
          controller.enqueue(encoder.encode(ndjson))
          controller.close()
        },
      }), { status: 200, headers: { 'Content-Type': 'application/x-ndjson' } })
    }
    return Response.json({})
  }))

  host = document.createElement('div')
  document.body.appendChild(host)
})

afterEach(async () => {
  if (root) await act(async () => root!.unmount())
  root = undefined
  host.remove()
  vi.unstubAllGlobals()
})

async function flush() {
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, 25))
  })
}

it('renders JP & US markets, switches languages cleanly, and streams AI market analysis', async () => {
  const { GlobalAnalysisPage } = await import('./Page')
  const { setLocale } = await import('@/lib/i18n')
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })

  root = createRoot(host)
  await act(async () => {
    root!.render(
      <QueryClientProvider client={qc}>
        <GlobalAnalysisPage />
      </QueryClientProvider>,
    )
  })
  await flush()

  // Initial state: JP overview in default zh-CN
  expect(host.textContent).toContain('日经225')
  expect(host.textContent).toContain('丰田汽车')

  // Switch locale to Japanese
  await act(async () => setLocale('ja'))
  await flush()
  expect(host.textContent).toContain('日米株式 AI 分析')
  expect(host.textContent).toContain('日経平均株価')
  expect(host.textContent).toContain('トヨタ自動車')

  // Switch locale to English & switch market to US
  await act(async () => setLocale('en'))
  await flush()
  expect(host.textContent).toContain('JP & US Market AI Analysis')

  const usBtn = Array.from(host.querySelectorAll('button')).find(b => b.textContent?.includes('US Market'))
  expect(usBtn).toBeTruthy()
  await act(async () => { usBtn!.click() })
  await flush()

  expect(host.textContent).toContain('S&P 500')
  expect(host.textContent).toContain('NVIDIA Corp')

  // Trigger AI Market Analysis stream with locale='en'
  const analyzeBtn = Array.from(host.querySelectorAll('button')).find(b => b.textContent?.includes('AI Market Analysis'))
  expect(analyzeBtn).toBeTruthy()
  await act(async () => { analyzeBtn!.click() })
  await flush()

  const analyzeCall = fetchCalls.find(c => c.url.includes('/api/global-analysis/analyze/market'))
  expect(analyzeCall).toBeTruthy()
  const body = JSON.parse(String(analyzeCall!.init?.body || '{}'))
  expect(body.market).toBe('US')
  expect(body.locale).toBe('en')
  expect(host.textContent).toContain('Strong breadth in mega-cap tech.')
}, 60_000)
