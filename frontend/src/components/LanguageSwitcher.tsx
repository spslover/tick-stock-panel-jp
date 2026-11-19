import { Languages } from 'lucide-react'
import { SUPPORTED_LOCALES, useI18n, type Locale } from '@/lib/i18n'
import { cn } from '@/lib/cn'

interface Props {
  variant?: 'compact' | 'segmented' | 'select'
  className?: string
}

/**
 * Multi-language switcher supporting Simplified Chinese, English, and Japanese.
 */
export function LanguageSwitcher({ variant = 'compact', className }: Props) {
  const { locale, setLocale, t } = useI18n()

  if (variant === 'segmented') {
    return (
      <div
        role="group"
        aria-label={t('layout.languageLabel')}
        className={cn(
          'inline-flex items-center rounded-btn border border-border bg-base p-0.5 gap-0.5',
          className,
        )}
      >
        {SUPPORTED_LOCALES.map(item => {
          const active = locale === item.code
          return (
            <button
              key={item.code}
              type="button"
              onClick={() => setLocale(item.code)}
              aria-pressed={active}
              className={cn(
                'px-2.5 py-1 rounded-[5px] text-xs font-medium transition-colors cursor-pointer',
                active
                  ? 'bg-accent text-white shadow-sm'
                  : 'text-secondary hover:text-foreground hover:bg-elevated/60',
              )}
            >
              {item.nativeLabel}
            </button>
          )
        })}
      </div>
    )
  }

  if (variant === 'select') {
    return (
      <label className={cn('inline-flex items-center gap-1.5 text-xs text-secondary', className)}>
        <Languages className="h-3.5 w-3.5 shrink-0 text-muted" />
        <select
          aria-label={t('layout.languageLabel')}
          value={locale}
          onChange={e => setLocale(e.target.value as Locale)}
          className="h-7 rounded-btn border border-border bg-surface px-2 text-xs text-foreground outline-none focus:border-accent/50 cursor-pointer"
        >
          {SUPPORTED_LOCALES.map(item => (
            <option key={item.code} value={item.code}>{item.nativeLabel}</option>
          ))}
        </select>
      </label>
    )
  }

  // compact cycle button (sidebar footer / rail mode)
  const idx = SUPPORTED_LOCALES.findIndex(item => item.code === locale)
  const current = SUPPORTED_LOCALES[idx] ?? SUPPORTED_LOCALES[0]
  const next = SUPPORTED_LOCALES[(idx + 1) % SUPPORTED_LOCALES.length]
  return (
    <button
      type="button"
      onClick={() => setLocale(next.code)}
      aria-label={`${t('layout.languageLabel')}: ${current.nativeLabel}`}
      title={`${t('layout.languageLabel')}: ${current.nativeLabel} → ${next.nativeLabel}`}
      className={cn(
        'flex items-center justify-center gap-1 rounded-btn px-2 py-2 text-[11px] font-medium text-foreground/80 transition-colors duration-150 ease-smooth hover:bg-elevated hover:text-foreground cursor-pointer',
        className,
      )}
    >
      <Languages className="h-3.5 w-3.5 shrink-0" />
      <span className="font-mono text-[10px] leading-none">{current.short}</span>
    </button>
  )
}
