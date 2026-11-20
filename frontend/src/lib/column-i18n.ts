import { t, type Locale } from './i18n'
import type { ColumnConfig, ColumnGroup } from './list-columns'

/**
 * 内置列表列的本地化映射。
 *
 * 列标签的中文口径来自后端/用户保存的列配置（自定义列名不可翻译），
 * 展示层按 builtin key 覆盖成当前语言，查不到时回退原标签。
 */
export const COLUMN_LABEL_KEYS: Record<string, string> = {
  abnormal: 'col.abnormal',
  added: 'col.added',
  added_at: 'col.added_at',
  amount: 'col.amount',
  amplitude: 'col.amplitude',
  annual_vol: 'col.annual_vol',
  boll_lower: 'col.boll_lower',
  boll_upper: 'col.boll_upper',
  candle: 'col.candle',
  change_amount: 'col.change_amount',
  core: 'col.core',
  debt_ratio: 'col.debt_ratio',
  deviate_10d: 'col.deviate_10d',
  deviate_30d: 'col.deviate_30d',
  deviate_3d: 'col.deviate_3d',
  float_val: 'col.float_val',
  gross_margin: 'col.gross_margin',
  high: 'col.high',
  high_60d: 'col.high_60d',
  intraday: 'col.intraday',
  limit: 'col.limit',
  limit_down_price: 'col.limit_down_price',
  limit_downs: 'col.limit_downs',
  limit_up_price: 'col.limit_up_price',
  limit_ups: 'col.limit_ups',
  low: 'col.low',
  low_60d: 'col.low_60d',
  macd_hist: 'col.macd_hist',
  momentum: 'col.momentum',
  momentum_10d: 'col.momentum_10d',
  momentum_20d: 'col.momentum_20d',
  momentum_30d: 'col.momentum_30d',
  momentum_5d: 'col.momentum_5d',
  momentum_60d: 'col.momentum_60d',
  net_income_yoy: 'col.net_income_yoy',
  net_margin: 'col.net_margin',
  open: 'col.open',
  pct: 'col.pct',
  pct_since_added: 'col.pct_since_added',
  prev_close: 'col.prev_close',
  price: 'col.price',
  range: 'col.range',
  revenue_yoy: 'col.revenue_yoy',
  score: 'col.score',
  signal: 'col.signal',
  signals: 'col.signals',
  strategies: 'col.strategies',
  symbol: 'col.symbol',
  tech: 'col.tech',
  turnover: 'col.turnover',
  vol_ma10: 'col.vol_ma10',
  vol_ma5: 'col.vol_ma5',
  vol_ratio: 'col.vol_ratio',
  volume: 'col.volume',
}

export const COLUMN_GROUP_KEYS: Record<string, string> = {
  abnormal: 'colGroup.abnormal',
  added: 'colGroup.added',
  core: 'colGroup.core',
  finance: 'colGroup.finance',
  limit: 'colGroup.limit',
  ma: 'colGroup.ma',
  momentum: 'colGroup.momentum',
  price: 'colGroup.price',
  quote: 'colGroup.quote',
  range: 'colGroup.range',
  scale: 'colGroup.scale',
  signal: 'colGroup.signal',
  tech: 'colGroup.tech',
  volume: 'colGroup.volume',
}

/** 列头文案：内置列按 key 本地化，扩展列沿用用户配置的名称。 */
export function columnLabel(col: ColumnConfig, locale?: Locale): string {
  if (col.source.type === 'builtin') {
    const key = COLUMN_LABEL_KEYS[col.source.key]
    if (key) return t(key, undefined, locale)
  }
  return col.label
}

/** 列分组标题。 */
export function columnGroupLabel(group: ColumnGroup, locale?: Locale): string {
  const key = COLUMN_GROUP_KEYS[group.id]
  if (key) return t(key, undefined, locale)
  return group.label
}
