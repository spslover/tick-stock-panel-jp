import type { ReactNode } from 'react'
import { BarChart3, Tags } from 'lucide-react'
import {
  IconDashboard, IconWatchlist, IconStrategy, IconFactors, IconBacktest,
  IconRegime, IconIndices, IconData, type BrandIconProps,
} from '@/components/BrandIcons'
import type { AnalysisMenu } from './api'
import { t, type Locale } from './i18n'
import { getFrontendExtensionNavigation } from '@/extensions/registry'

export type NavigationEntry = {
  id: string; to: string; label: string; icon: (props: BrandIconProps) => ReactNode
  type: 'builtin' | 'analysis' | 'extension'
  visible: boolean; arch?: 'core' | 'ext'; badge?: string; extensionId?: string
}

const NAV_I18N_KEY_BY_ID: Record<string, string> = {
  '/': 'nav.dashboard',
  '/watchlist': 'nav.watchlist',
  '/screener': 'nav.screener',
  '/factors': 'nav.factors',
  '/backtest': 'nav.backtest',
  '/regime': 'nav.regime',
  '/indices': 'nav.indices',
  '/data': 'nav.data',
  '/stock-analysis': 'nav.stockAnalysis',
  '/limit-ladder': 'nav.limitLadder',
  '/concept-analysis': 'nav.conceptAnalysis',
  '/industry-analysis': 'nav.industryAnalysis',
  '/financials': 'nav.financials',
  '/monitor': 'nav.monitor',
  '/abnormal': 'nav.abnormal',
  '/lots': 'nav.lots',
  '/paper': 'nav.paper',
  '/signals': 'nav.signals',
  '/review': 'nav.review',
  '/global-analysis': 'nav.globalAnalysis',
}

export const CORE_NAVIGATION: NavigationEntry[] = [
  { id: '/', label: '看板', icon: IconDashboard },
  { id: '/watchlist', label: '自选', icon: IconWatchlist },
  { id: '/screener', label: '策略', icon: IconStrategy },
  { id: '/factors', label: '因子', icon: IconFactors },
  { id: '/backtest', label: '回测', icon: IconBacktest },
  { id: '/regime', label: '市场环境', icon: IconRegime },
  { id: '/indices', label: '指数', icon: IconIndices },
  { id: '/data', label: '数据', icon: IconData },
].map(x => ({ ...x, to: x.id, type: 'builtin', visible: true, arch: 'core' }))

/** 宿主与管理页共用来源及 ID，分析页历史裸 ID 只在兼容读取时转换。 */
export function getNavigationEntries(menus: readonly AnalysisMenu[] = [], locale?: Locale): NavigationEntry[] {
  const entries: NavigationEntry[] = [
    ...CORE_NAVIGATION.map(item => ({
      ...item,
      label: NAV_I18N_KEY_BY_ID[item.id] ? t(NAV_I18N_KEY_BY_ID[item.id], undefined, locale) : item.label,
    })),
    ...menus.map(m => ({
      id: `/analysis/${m.id}`, to: `/analysis/${m.id}`, label: m.label,
      type: 'analysis' as const, visible: m.visible,
      icon: m.icon === 'tags' ? Tags : BarChart3, arch: 'ext' as const,
    })),
    ...getFrontendExtensionNavigation().map(item => ({
      id: item.route.path, to: item.route.path,
      label: NAV_I18N_KEY_BY_ID[item.route.path]
        ? t(NAV_I18N_KEY_BY_ID[item.route.path], undefined, locale)
        : item.label,
      icon: item.icon,
      type: 'extension' as const, visible: true, arch: 'ext' as const,
      badge: item.badge, extensionId: item.extensionId,
    })),
  ]
  return [...new Map(entries.map(x => [x.id, x])).values()]
}
