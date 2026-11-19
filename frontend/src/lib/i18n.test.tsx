// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { zhCN } from '@/locales/zh-CN'
import { en } from '@/locales/en'
import { ja } from '@/locales/ja'
import { PHRASES_EN, PHRASES_JA } from '@/locales/phrases'

let host: HTMLDivElement
let root: Root | undefined
let remote: Record<string, unknown>

function flattenKeys(obj: Record<string, unknown>, prefix = ''): string[] {
  const keys: string[] = []
  for (const [k, v] of Object.entries(obj)) {
    const full = prefix ? `${prefix}.${k}` : k
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      keys.push(...flattenKeys(v as Record<string, unknown>, full))
    } else {
      keys.push(full)
    }
  }
  return keys.sort()
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetModules()
  vi.useFakeTimers()
  localStorage.clear()
  document.documentElement.lang = 'zh-CN'
  remote = {}
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === 'PUT') {
      Object.assign(remote, JSON.parse(init.body as string))
      return Response.json({ ui_prefs: remote })
    }
    return Response.json({ ui_prefs: remote })
  }))
  host = document.createElement('div')
  document.body.appendChild(host)
})

afterEach(async () => {
  if (root) await act(async () => root!.unmount())
  root = undefined
  host.remove()
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

it('defaults to zh-CN when unset and switches across zh-CN / en / ja with persistence', async () => {
  const i18n = await import('./i18n')
  function Probe() {
    const { locale, t, tx } = i18n.useI18n()
    return (
      <div data-locale={locale}>
        {locale}|{t('nav.globalAnalysis')}|{tx('自选')}|{tx('已同步 {done}/{total} 张表…', { done: 2, total: 5 })}
      </div>
    )
  }
  root = createRoot(host)
  await act(async () => root!.render(<Probe />))

  expect(host.textContent).toBe('zh-CN|日美股市 AI|自选|已同步 2/5 张表…')
  expect(document.documentElement.lang).toBe('zh-CN')

  await act(async () => i18n.setLocale('en'))
  await act(async () => { await vi.advanceTimersByTimeAsync(300) })
  expect(localStorage.getItem('tf-locale')).toBe('en')
  expect(document.documentElement.lang).toBe('en')
  expect(host.textContent).toBe('en|JP & US AI|Watchlist|Synced 2/5 tables...')
  expect(remote.locale).toBe('en')

  await act(async () => i18n.setLocale('ja'))
  await act(async () => { await vi.advanceTimersByTimeAsync(300) })
  expect(localStorage.getItem('tf-locale')).toBe('ja')
  expect(document.documentElement.lang).toBe('ja')
  expect(host.textContent).toBe('ja|日米株式 AI|ウォッチリスト|2/5 テーブル同期完了…')
  expect(remote.locale).toBe('ja')
})

it('normalizes language tags and hydrates remote ui_prefs locale', async () => {
  remote = { locale: 'ja' }
  const { normalizeLocale, getLocale, applyLocale } = await import('./i18n')
  const { hydrateUiPrefs } = await import('./uiPrefs')

  expect(normalizeLocale('ja-JP')).toBe('ja')
  expect(normalizeLocale('en-US')).toBe('en')
  expect(normalizeLocale('invalid')).toBe('zh-CN')

  await act(async () => {
    await hydrateUiPrefs()
    applyLocale(getLocale())
  })
  expect(getLocale()).toBe('ja')
  expect(document.documentElement.lang).toBe('ja')
}, 30_000)

it('renders LanguageSwitcher across all 3 variants and updates active locale on click', async () => {
  const { LanguageSwitcher } = await import('@/components/LanguageSwitcher')
  const i18n = await import('./i18n')
  root = createRoot(host)
  await act(async () => root!.render(<LanguageSwitcher variant="segmented" />))

  const buttons = host.querySelectorAll('button')
  expect(buttons).toHaveLength(3)
  await act(async () => { buttons[2].click() })
  expect(i18n.getLocale()).toBe('ja')

  await act(async () => root!.render(<LanguageSwitcher variant="select" />))
  const select = host.querySelector('select') as HTMLSelectElement
  expect(select).toBeTruthy()
  expect(select.value).toBe('ja')
}, 30_000)

it('maintains 100% key parity across zh-CN, en, and ja dictionaries and phrases', () => {
  const zhKeys = flattenKeys(zhCN as unknown as Record<string, unknown>)
  const enKeys = flattenKeys(en as unknown as Record<string, unknown>)
  const jaKeys = flattenKeys(ja as unknown as Record<string, unknown>)
  expect(enKeys).toEqual(zhKeys)
  expect(jaKeys).toEqual(zhKeys)

  const phraseKeysEn = Object.keys(PHRASES_EN).sort()
  const phraseKeysJa = Object.keys(PHRASES_JA).sort()
  expect(phraseKeysEn).toEqual(phraseKeysJa)
  expect(phraseKeysEn.length).toBeGreaterThan(300)
})
