// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Indices } from './Indices'

// FE-01: 指数页切换指数后不得展示上一只指数的日K (CONTRIBUTING §7)。
// 旧实现的 placeholderData 无条件复用上一份数据, 切换瞬间会把上一只指数的
// K 线挂在新指数标题下 —— 标题正确、数据错误。

type DailyResponse = {
  symbol: string
  rows: { date: string; open: number; high: number; low: number; close: number; volume: number }[]
}

const fixtures = vi.hoisted(() => ({
  pending: [] as { symbol: string; resolve: (value: DailyResponse) => void }[],
}))

vi.mock('@/lib/api', () => ({
  api: {
    indexQuotes: async () => ({ rows: [] }),
    indexDaily: (symbol: string) => new Promise<DailyResponse>(resolve => {
      fixtures.pending.push({ symbol, resolve })
    }),
    indexMinute: async () => ({ symbol: '', date: '', rows: [] }),
  },
}))
vi.mock('@/lib/useSharedQueries', () => ({
  useCapabilities: () => ({ data: { capabilities: { 'kline.minute.batch': false } } }),
}))
vi.mock('@/components/charts/LightweightDaily', () => ({
  LightweightDaily: ({ data }: { data: { date: string }[] }) => (
    <div data-daily>{data.map(row => row.date).join(',')}</div>
  ),
}))
vi.mock('@/components/charts/LightweightIntraday', () => ({
  LightweightIntraday: () => <div data-chart />,
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  fixtures.pending.length = 0
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: {
    retry: false, staleTime: Infinity, gcTime: Infinity,
  } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function settle() {
  for (let i = 0; i < 5; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

function dailyPanel() {
  return host.querySelector<HTMLElement>('[data-daily]')
}

async function resolveDaily(symbol: string, dates: string[]) {
  const index = fixtures.pending.findIndex(call => call.symbol === symbol)
  expect(index).toBeGreaterThanOrEqual(0)
  const [call] = fixtures.pending.splice(index, 1)
  await act(async () => call.resolve({
    symbol,
    rows: dates.map(date => ({ date, open: 10, high: 12, low: 9, close: 11, volume: 1 })),
  }))
  await settle()
}

async function clickSymbol(symbol: string) {
  const button = Array.from(host.querySelectorAll('button'))
    .find(node => node.textContent?.includes(symbol))
  expect(button).toBeTruthy()
  await act(async () => (button as HTMLButtonElement).click())
  await settle()
}

it('切换指数后不得展示上一只指数的日K', async () => {
  await act(async () => root.render(
    <MemoryRouter><QueryClientProvider client={client}><Indices /></QueryClientProvider></MemoryRouter>,
  ))
  await settle()

  // 核心四只中的第一只 (上证指数) 的日K 先返回
  await resolveDaily('000001.SH', ['2026-09-09', '2026-09-10'])
  expect(dailyPanel()?.textContent).toBe('2026-09-09,2026-09-10')

  // 切换到深证成指 —— 其日K 请求尚未返回
  await clickSymbol('399001.SZ')

  // 关键断言: 不得残留上一只指数的日K
  expect(dailyPanel()?.textContent ?? '').not.toContain('2026-09-09')
  expect(dailyPanel()?.textContent ?? '').not.toContain('2026-09-10')

  // 新指数的日K 返回后才展示
  await resolveDaily('399001.SZ', ['2026-09-11'])
  expect(dailyPanel()?.textContent).toBe('2026-09-11')
})
