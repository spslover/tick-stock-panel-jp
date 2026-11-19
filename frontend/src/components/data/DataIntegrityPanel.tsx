import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { CheckCircle2, Loader2, SearchCheck, Wrench, AlertTriangle } from 'lucide-react'
import { api, type IntegrityAsset, type PipelineJob } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useI18n } from '@/lib/i18n'

const ASSETS: { id: IntegrityAsset; label: string }[] = [
  { id: 'stock', label: '股票' }, { id: 'etf', label: 'ETF' }, { id: 'index', label: '指数' },
]

export function DataIntegrityPanel({ hasCap, isRunning, job, defaultAssets, onStart }: {
  hasCap: boolean
  isRunning: boolean
  job?: PipelineJob | null
  defaultAssets: IntegrityAsset[]
  onStart: (jobId: string) => void
}) {
  const { tx, locale } = useI18n()
  const [days, setDays] = useState(30)
  const [assets, setAssets] = useState<IntegrityAsset[]>(defaultAssets)
  const [confirm, setConfirm] = useState(false)
  const [limit, setLimit] = useState(50)
  const [ownJobId, setOwnJobId] = useState<string | null>(null)
  const latest = useQuery({ queryKey: QK.dataIntegrity, queryFn: api.dataIntegrity })
  const report = latest.data?.report
  const busy = isRunning

  const scan = useMutation({
    mutationFn: () => api.scanDataIntegrity(days, assets),
    onSuccess: ({ job_id }) => { setConfirm(false); setLimit(50); setOwnJobId(job_id); onStart(job_id) },
  })
  const repair = useMutation({
    mutationFn: () => api.repairDataIntegrity(report!.id),
    onSuccess: ({ job_id }) => { setConfirm(false); setOwnJobId(job_id); onStart(job_id) },
  })
  const pending = busy || scan.isPending || repair.isPending
  const matches = !!report && report.lookback_days === days
    && [...assets].sort().join(',') === [...report.asset_types].sort().join(',')
  const canRepair = matches && !!report.repairable_count && hasCap && !pending
  const message = scan.error || repair.error || latest.error
    || (job?.id === ownJobId && job?.status === 'failed' ? job.error : null)
  const lastLog = job?.log?.at(-1)?.msg

  return (
    <section className="space-y-4" aria-label={tx('日K数据自检')}>
      <p className="text-xs text-secondary leading-relaxed">
        {tx('检查历史日K的日期缺失、盘中快照、文件损坏和异常价格。当天尚在更新的数据不参与检查。')}</p>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-4">
          {ASSETS.map(a => (
            <label key={a.id} className="inline-flex items-center gap-1.5 text-xs text-secondary">
              <input type="checkbox" checked={assets.includes(a.id)} disabled={pending}
                className="accent-accent"
                onChange={e => { setConfirm(false); setAssets(old => e.target.checked ? [...old, a.id] : old.filter(x => x !== a.id)) }} />
              {tx(a.label)}
            </label>
          ))}
          <select aria-label={tx('自检范围')} value={days} disabled={pending}
            onChange={e => { setDays(Number(e.target.value)); setConfirm(false) }}
            className="rounded-btn border border-border bg-surface px-2 py-1.5 text-xs text-foreground focus:outline-none focus:ring-2 focus:ring-accent/40">
            {[7, 30, 90, 365].map(n => <option key={n} value={n}>{tx('最近 {range}', { range: n === 365 ? tx('一年') : tx('{n} 天', { n }) })}</option>)}
          </select>
        </div>
        <button onClick={() => scan.mutate()} disabled={pending || !assets.length}
          className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-3 py-2 text-xs font-medium text-white hover:bg-accent/90 disabled:opacity-40 focus:outline-none focus:ring-2 focus:ring-accent/40">
          {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <SearchCheck className="h-3.5 w-3.5" />}
          {pending ? tx('任务进行中…') : tx('开始自检')}
        </button>
      </div>

      {pending && <div role="status" className="rounded-btn border border-accent/25 bg-accent/5 p-3 text-xs text-secondary">
        {lastLog || tx('任务正在准备，请稍候…')}
        {job && <span className="ml-2 font-mono text-accent">{job.progress}%</span>}
      </div>}
      {message && <p role="alert" className="text-xs text-danger">{message instanceof Error ? message.message : String(message)}</p>}

      {!report && !latest.isLoading && !latest.isError && <div className="rounded-card border border-dashed border-border p-5 text-center text-xs text-muted">
        {tx('选择资产和时间范围，开始第一次自检。')}</div>}
      {latest.isLoading && <p role="status" className="text-xs text-muted">{tx('读取上次自检结果…')}</p>}
      {report && <div className="overflow-hidden rounded-card border border-border">
        <div className="flex flex-wrap items-center justify-between gap-2 bg-elevated/40 px-4 py-3">
          <div className="flex items-center gap-2 text-sm font-medium">
            {report.status === 'clean' ? <CheckCircle2 className="h-4 w-4 text-bear" /> : <AlertTriangle className="h-4 w-4 text-warning" />}
            {report.status === 'clean' ? tx('已有日K未发现问题') : report.status === 'empty' ? tx('暂无可检查的历史日K') : tx('发现 {n} 个待处理分区', { n: report.issues.length })}
          </div>
          <span className="text-[11px] text-muted">{report.start_date} {tx('至')} {report.end_date}</span>
        </div>
        {report.uncertain_count > 0 && <p className="border-b border-border bg-warning/5 px-4 py-3 text-xs text-warning">
          {tx('{n} 个缺失日期尚不能确认是否开市。交易日历不可用时会保留为待核实，不自动修补这些日期。', { n: report.uncertain_count })}
        </p>}
        {report.no_data_assets.length > 0 && <p className="px-4 py-2 text-xs text-muted">
          {tx('{assets}暂无历史日K，可先在数据页同步。', { assets: report.no_data_assets.map(a => ASSETS.find(x => x.id === a)?.label).join('、') })}
        </p>}
        {!!report.issues.length && <div className="max-h-72 overflow-auto">
          <table className="w-full text-left text-xs">
            <thead className="sticky top-0 bg-surface text-muted"><tr>
              <th className="px-4 py-2 font-normal">{tx('日期 / 资产')}</th>
              <th className="px-4 py-2 font-normal">{tx('检查结果')}</th>
            </tr></thead>
            <tbody>{report.issues.slice(0, limit).map((item, i) => <tr key={`${item.asset_type}-${item.date}-${i}`} className="border-t border-border/60 align-top">
              <td className="whitespace-nowrap px-4 py-3"><div className="font-mono">{item.date}</div><div className="mt-1 text-muted">{item.asset_label}</div></td>
              <td className="px-4 py-3"><div className={item.repairable ? 'text-danger' : 'text-warning'}>{item.kind_label}</div>
                <div className="mt-1 break-words text-muted">{item.reason}</div>
                {!!item.symbol_count && <div className="mt-1 break-all font-mono text-[10px] text-secondary">{item.symbols.join('、')}{item.symbol_count > item.symbols.length ? tx('等 {n} 只', { n: item.symbol_count }) : ''}</div>}
                {item.repairable && <div className="mt-1 text-[11px] text-secondary">
                  {item.kind === 'recompute' ? tx('日K已校验，仅继续重算依赖数据') : item.symbol_count
                    ? tx('仅回补这 {n} 只股票的异常日期', { n: item.symbol_count })
                    : tx('无法定位个股，仅对该日期做整日回补')}
                </div>}
              </td>
            </tr>)}</tbody>
          </table>
          {report.issues.length > limit && <button className="w-full py-2 text-xs text-accent" onClick={() => setLimit(n => n + 50)}>{tx('继续显示问题（已显示 {n} / {total}）', { n: limit, total: report.issues.length })}</button>}
        </div>}
        <div className="space-y-2 border-t border-border px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <span className="text-[11px] text-muted">{tx('自检于 {time}', { time: new Date(report.checked_at).toLocaleString(locale) })}{report.repairable_count > 0 ? tx(' · 可自动修补 {n} 个', { n: report.repairable_count }) : ''}</span>
            {report.repairable_count > 0 && <button disabled={!canRepair} onClick={() => setConfirm(true)}
              className="inline-flex items-center gap-1.5 rounded-btn border border-accent/30 px-3 py-1.5 text-xs text-accent hover:bg-accent/10 disabled:opacity-40 focus:outline-none focus:ring-2 focus:ring-accent/40">
              <Wrench className="h-3 w-3" />{tx('修补已确认问题')}</button>}
          </div>
          {!matches && <p className="text-xs text-warning">{tx('检查范围已变化，请重新自检后再修补。')}</p>}
          {!hasCap && report.repairable_count > 0 && <p className="text-xs text-warning">{tx('修补需要可用的日K数据源，请先检查数据源配置。')}</p>}
          {report.repair_error && <p role="alert" className="text-xs text-danger">{tx('上次修补未完成：{msg}', { msg: report.repair_error })}</p>}
          {report.backup_dir && <p className="break-all text-[11px] text-muted">{tx('原异常文件备份：数据目录/{dir}', { dir: report.backup_dir })}</p>}
          {confirm && <div className="rounded-btn border border-accent/25 bg-accent/5 p-3 space-y-3">
            <p className="text-xs leading-relaxed text-secondary">{tx('按异常股票和日期回补日K；无法定位个股时，仅回补对应整日。原文件先备份，日K校验通过后，使用完整历史重算受影响股票从异常日起的指标，并更新依赖结果。已有回测需重新运行验证。')}</p>
            <div className="flex justify-end gap-2">
              <button onClick={() => setConfirm(false)} className="px-3 py-1.5 text-xs text-secondary">{tx('取消')}</button>
              <button onClick={() => repair.mutate()} disabled={!canRepair} className="rounded-btn bg-accent px-3 py-1.5 text-xs text-white disabled:opacity-40">{tx('确认修补并复检')}</button>
            </div>
          </div>}
        </div>
      </div>}
    </section>
  )
}
