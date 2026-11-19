import { useSyncExternalStore } from 'react'
import { hydrateUiPrefs, setUiPref } from '@/lib/uiPrefs'
import { zhCN } from '@/locales/zh-CN'
import { en } from '@/locales/en'
import { ja } from '@/locales/ja'
import { PHRASES_EN, PHRASES_JA } from '@/locales/phrases'

export type Locale = 'zh-CN' | 'en' | 'ja'

const KEY = 'tf-locale'
const EVENT = 'tf-locale-change'

export const SUPPORTED_LOCALES: readonly {
  code: Locale
  label: string
  nativeLabel: string
  short: string
}[] = [
  { code: 'zh-CN', label: '简体中文', nativeLabel: '简体中文', short: '简' },
  { code: 'en', label: 'English', nativeLabel: 'English', short: 'EN' },
  { code: 'ja', label: '日本語', nativeLabel: '日本語', short: '日' },
]

const DICTS = {
  'zh-CN': zhCN,
  en,
  ja,
} as const

export function normalizeLocale(raw: unknown): Locale {
  if (raw === 'en' || raw === 'ja' || raw === 'zh-CN') return raw
  if (typeof raw === 'string') {
    const low = raw.toLowerCase()
    if (low.startsWith('ja')) return 'ja'
    if (low.startsWith('en')) return 'en'
  }
  return 'zh-CN'
}

export function getLocale(): Locale {
  try {
    return normalizeLocale(localStorage.getItem(KEY))
  } catch {
    return 'zh-CN'
  }
}

export function applyLocale(locale: Locale): void {
  const norm = normalizeLocale(locale)
  if (typeof document !== 'undefined') {
    document.documentElement.lang = norm
  }
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent(EVENT, { detail: norm }))
  }
}

export function setLocale(locale: Locale): void {
  const norm = normalizeLocale(locale)
  setUiPref(KEY, norm)
  applyLocale(norm)
}

function subscribeLocale(cb: () => void): () => void {
  if (typeof window === 'undefined') return () => {}
  let active = true
  const onChange = () => {
    if (!active) return
    const val = getLocale()
    if (typeof document !== 'undefined') {
      document.documentElement.lang = val
    }
    cb()
  }
  window.addEventListener(EVENT, onChange)
  window.addEventListener('storage', onChange)
  void hydrateUiPrefs().then(onChange)
  return () => {
    active = false
    window.removeEventListener(EVENT, onChange)
    window.removeEventListener('storage', onChange)
  }
}

export function useLocale(): Locale {
  return useSyncExternalStore(subscribeLocale, getLocale, () => 'zh-CN')
}

function resolveKey(obj: unknown, key: string): string | undefined {
  let cur: unknown = obj
  for (const seg of key.split('.')) {
    if (!cur || typeof cur !== 'object') return undefined
    cur = (cur as Record<string, unknown>)[seg]
  }
  return typeof cur === 'string' ? cur : undefined
}

function interpolate(text: string, vars?: Record<string, string | number>): string {
  if (!vars) return text
  return text.replace(/\{([a-zA-Z0-9_]+)\}/g, (_, k) =>
    vars[k] !== undefined ? String(vars[k]) : `{${k}}`,
  )
}

/** Lookup a structured i18n key for the active (or specified) locale. */
export function t(
  key: string,
  vars?: Record<string, string | number>,
  localeOverride?: Locale,
): string {
  const loc = localeOverride ?? getLocale()
  const raw = resolveKey(DICTS[loc], key) ?? resolveKey(zhCN, key) ?? key
  return interpolate(raw, vars)
}

/** Translate a Simplified Chinese UI phrase into the active locale. */
export function tx(
  zhText: string,
  vars?: Record<string, string | number>,
  localeOverride?: Locale,
): string {
  const loc = localeOverride ?? getLocale()
  let raw = zhText
  if (loc === 'en') raw = PHRASES_EN[zhText] ?? zhText
  else if (loc === 'ja') raw = PHRASES_JA[zhText] ?? zhText
  return interpolate(raw, vars)
}

/** Reactive translation hook for React components. */
export function useI18n() {
  const locale = useLocale()
  return {
    locale,
    setLocale,
    t: (key: string, vars?: Record<string, string | number>) => t(key, vars, locale),
    tx: (zhText: string, vars?: Record<string, string | number>) => tx(zhText, vars, locale),
  }
}

export function formatRelativeTime(iso: string, localeOverride?: Locale): string {
  const loc = localeOverride ?? getLocale()
  try {
    const ms = new Date(iso).getTime()
    if (!Number.isFinite(ms)) return iso
    const diff = Date.now() - ms
    if (diff < 60_000) return t('common.justNow', undefined, loc)
    if (diff < 3600_000) return t('common.minutesAgo', { n: Math.floor(diff / 60_000) }, loc)
    if (diff < 86400_000) return t('common.hoursAgo', { n: Math.floor(diff / 3600_000) }, loc)
    if (diff < 7 * 86400_000) return t('common.daysAgo', { n: Math.floor(diff / 86400_000) }, loc)
    return new Date(iso).toLocaleDateString(loc === 'zh-CN' ? 'zh-CN' : loc === 'ja' ? 'ja-JP' : 'en-US')
  } catch {
    return iso
  }
}

export { DICTS }
