/**
 * 设置 → 页面插件: 运行时扩展页面的上传/试载/启停/删除。
 *
 * 生命周期: 上传(后端存 pending) → 前端立即动态 import 试载并做契约校验
 * (packToExtension) → 校验结果回填 manifest(成功即启用) → 刷新页面后在
 * 启动引导窗口正式挂载(main.tsx → loadRuntimePageExtensions)。
 * 启动期失败(路由冲突等)由注册表记入 loadErrors, 这里按 runtime: 前缀
 * 展示。打包方式见 frontend/page-pack-template 与 docs/page-sdk.md。
 */
import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Package, Trash2, Upload, AlertTriangle, CheckCircle2, FileCode, FolderDown } from 'lucide-react'
import { api } from '@/lib/api'
import { useI18n } from '@/lib/i18n'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'
import { packToExtension, runtimeBundleUrl } from '@/extensions/runtime'
import { getFrontendExtensionLoadErrors } from '@/extensions/registry'

function fmtSize(n: number): string {
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)}MB`
  return `${Math.max(1, Math.round(n / 1024))}KB`
}

function fmtTime(ts: number): string {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
}

export function SettingsPagePacksPanel() {
  const { tx } = useI18n()
  const qc = useQueryClient()
  const list = useQuery({ queryKey: QK.extPages, queryFn: api.extPagesList })
  const [idInput, setIdInput] = useState('')
  const [busy, setBusy] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const [busyImport, setBusyImport] = useState(false)
  const [importNs, setImportNs] = useState('')
  const importFileRef = useRef<HTMLInputElement>(null)
  // 本项目可导入的源码扩展清单; Docker/产物部署返回空数组 (无前端源码树)
  const sourceNs = useQuery({ queryKey: QK.extPagesSourceNamespaces, queryFn: api.extPagesSourceNamespaces })

  const invalidate = () => qc.invalidateQueries({ queryKey: QK.extPages })
  const enabledMut = useMutation({
    mutationFn: (v: { id: string; enabled: boolean }) => api.extPagesPutEnabled(v.id, v.enabled),
    onSuccess: (_d, v) => { invalidate(); toast(v.enabled ? tx('已启用, 刷新页面后生效') : tx('已停用, 刷新页面后生效'), 'success') },
  })
  const deleteMut = useMutation({
    mutationFn: (id: string) => api.extPagesDelete(id),
    onSuccess: invalidate,
  })

  /** 试载刚入库的产物并回填 manifest; 校验失败抛错 (调用方负责 toast)。 */
  async function trialLoadAndRegister(id: string): Promise<void> {
    let label = '', path = '', error = ''
    try {
      const m = await import(/* @vite-ignore */ `${runtimeBundleUrl(id)}?trial=${Date.now()}`)
      const ext = packToExtension(m.default)
      label = ext.navigation?.[0]?.label ?? id
      path = ext.routes?.[0]?.path ?? ''
    } catch (e) {
      error = e instanceof Error ? e.message : String(e)
    }
    await api.extPagesPutManifest(id, { label, path, error })
    await invalidate()
    if (error) throw new Error(error)
  }

  async function handleUpload() {
    const file = fileRef.current?.files?.[0]
    const id = idInput.trim()
    if (!file) { toast(tx('请选择页面插件产物 (.js / .mjs)'), 'error'); return }
    if (!/^[a-zA-Z0-9_]{1,64}$/.test(id)) { toast(tx('ID 仅允许字母/数字/下划线 (1-64 位)'), 'error'); return }
    setBusy(true)
    try {
      await api.extPagesUpload(id, file)
      // 试载: 直接 import 刚上传的产物, 契约校验失败则停用并回填错误
      await trialLoadAndRegister(id)
      toast(tx('页面插件已启用, 刷新页面后生效'), 'success')
      setIdInput('')
      if (fileRef.current) fileRef.current.value = ''
    } catch (e) {
      toast(tx('页面插件校验失败: {msg}', { msg: e instanceof Error ? e.message : String(e) }), 'error')
    } finally {
      setBusy(false)
    }
  }

  async function handleImportFile() {
    const file = importFileRef.current?.files?.[0]
    if (!file) { toast(tx('请选择源码文件 (.tsx)'), 'error'); return }
    setBusyImport(true)
    try {
      const meta = await api.extPagesImportSource(file)
      await trialLoadAndRegister(meta.id)
      toast(tx('已导入并启用 {id}, 刷新页面后生效', { id: meta.id }), 'success')
      if (importFileRef.current) importFileRef.current.value = ''
    } catch (e) {
      toast(tx('源码导入失败: {msg}', { msg: e instanceof Error ? e.message : String(e) }), 'error')
    } finally {
      setBusyImport(false)
    }
  }

  async function handleImportFromProject() {
    if (!importNs) { toast(tx('请选择要导入的源码扩展'), 'error'); return }
    setBusyImport(true)
    try {
      const meta = await api.extPagesImportFromProject(importNs)
      await trialLoadAndRegister(meta.id)
      toast(tx('已导入并启用 {id}, 刷新页面后生效', { id: meta.id }), 'success')
    } catch (e) {
      toast(tx('源码导入失败: {msg}', { msg: e instanceof Error ? e.message : String(e) }), 'error')
    } finally {
      setBusyImport(false)
    }
  }

  const packs = list.data ?? []
  const bootErrors = getFrontendExtensionLoadErrors().filter(e => e.source.startsWith('runtime:'))

  return (
    <div className="space-y-4">
      <section className="rounded-2xl border border-border bg-surface p-6">
        <div className="text-[11px] uppercase tracking-[0.2em] text-accent/80">{tx('页面插件')}</div>
        <h2 className="mt-2 text-2xl font-semibold tracking-tight text-foreground">{tx('上传打包页面产物, 免重建挂载')}</h2>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-secondary">
          {tx('用')} <span className="font-mono text-xs text-accent">frontend/page-pack-template</span> {tx('打包页面 (产物为单文件 ES 模块), 在此上传即成为完整页面 — 路由、侧栏入口、菜单管理自动注册, 刷新后生效。契约与安全边界见')} <span className="font-mono text-xs text-accent">docs/page-sdk.md</span>。
        </p>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <input
            value={idInput}
            onChange={(e) => setIdInput(e.target.value)}
            placeholder={tx('页面插件 ID (字母/数字/下划线)')}
            className="h-9 w-56 rounded-btn border border-border bg-elevated px-3 text-sm text-foreground placeholder:text-muted/60 focus:border-accent/50 focus:outline-none"
          />
          <input
            ref={fileRef}
            type="file"
            accept=".js,.mjs"
            className="hidden"
          />
          <button
            type="button"
            onClick={() => fileRef.current?.click()}
            className="inline-flex h-9 items-center gap-1.5 rounded-btn border border-border bg-elevated px-3 text-xs text-secondary hover:bg-elevated/80 hover:text-foreground transition-colors"
          >
            <Package className="h-3.5 w-3.5" />
            {fileRef.current?.files?.[0]?.name ?? tx('选择产物文件')}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={handleUpload}
            className="inline-flex h-9 items-center gap-1.5 rounded-btn bg-accent px-3 text-xs font-medium text-white hover:bg-accent/90 disabled:opacity-50 transition-colors"
          >
            <Upload className="h-3.5 w-3.5" />
            {busy ? tx('上传并校验中…') : tx('上传并校验')}
          </button>
        </div>
      </section>

      <section className="rounded-card border border-border bg-surface p-5">
        <div className="text-[11px] uppercase tracking-[0.2em] text-accent/80">{tx('导入源码')}</div>
        <h3 className="mt-2 text-lg font-semibold tracking-tight text-foreground">{tx('.tsx 源码一键导入, 自动编译挂载')}</h3>
        <p className="mt-2 max-w-3xl text-xs leading-5 text-secondary">
          {tx('上传源码扩展文件 (.tsx) 或从本项目')} <span className="font-mono text-accent">frontend/src/custom/</span> {tx('直接选择, 后端用项目自带的 esbuild 自动编译并适配契约, 免重新构建。产物为标准页面插件, 与上方上传的包同一管理。 边界: 源码里注册的插槽(slot)会被跳过; 页面内路由参数读到空值; 仅源码/桌面部署可发起导入。')}</p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input ref={importFileRef} type="file" accept=".tsx" className="hidden" />
          <button
            type="button"
            onClick={() => importFileRef.current?.click()}
            className="inline-flex h-9 items-center gap-1.5 rounded-btn border border-border bg-elevated px-3 text-xs text-secondary hover:bg-elevated/80 hover:text-foreground transition-colors"
          >
            <FileCode className="h-3.5 w-3.5" />
            {importFileRef.current?.files?.[0]?.name ?? tx('选择 .tsx 源文件')}
          </button>
          <button
            type="button"
            disabled={busyImport}
            onClick={handleImportFile}
            className="inline-flex h-9 items-center gap-1.5 rounded-btn bg-accent px-3 text-xs font-medium text-white hover:bg-accent/90 disabled:opacity-50 transition-colors"
          >
            <Upload className="h-3.5 w-3.5" />
            {busyImport ? tx('编译导入中…') : tx('编译并导入')}
          </button>
          {(sourceNs.data?.length ?? 0) > 0 && (
            <>
              <span className="ml-2 text-[11px] text-muted">{tx('或从本项目导入:')}</span>
              <select
                value={importNs}
                onChange={e => setImportNs(e.target.value)}
                className="h-9 rounded-btn border border-border bg-base px-2 text-xs text-foreground"
              >
                <option value="">{tx('选择扩展…')}</option>
                {sourceNs.data!.map(ns => <option key={ns} value={ns}>{ns}</option>)}
              </select>
              <button
                type="button"
                disabled={busyImport || !importNs}
                onClick={handleImportFromProject}
                className="inline-flex h-9 items-center gap-1.5 rounded-btn border border-border bg-elevated px-3 text-xs text-secondary hover:bg-elevated/80 hover:text-foreground disabled:opacity-50 transition-colors"
              >
                <FolderDown className="h-3.5 w-3.5" />
                {tx('导入')}</button>
            </>
          )}
        </div>
      </section>

      {bootErrors.length > 0 && (
        <section className="rounded-card border border-danger/30 bg-danger/5 p-4 text-xs leading-5 text-secondary">
          <div className="mb-1 flex items-center gap-1.5 font-medium text-danger">
            <AlertTriangle className="h-3.5 w-3.5" /> {tx('本次启动有页面插件挂载失败')}</div>
          {bootErrors.map((e, i) => (
            <div key={i} className="font-mono">{e.source}: {e.error}</div>
          ))}
          <div className="mt-1 text-muted">{tx('多为路由与核心/其他页面冲突或产物损坏; 修正后重新上传, 或停用对应包。')}</div>
        </section>
      )}

      <section className="rounded-card border border-border bg-surface overflow-hidden">
        <div className="grid grid-cols-[1fr_8rem_6rem_5rem_4rem] items-center border-b border-border px-4 py-2 text-[11px] text-muted">
          <div>{tx('页面 / ID')}</div><div>{tx('路径')}</div><div>{tx('大小 / 时间')}</div><div className="text-center">{tx('启用')}</div><div className="text-center">{tx('操作')}</div>
        </div>
        {packs.length === 0 && (
          <div className="px-4 py-10 text-center text-sm text-muted">{tx('暂无页面插件，用上方表单上传第一个。')}</div>
        )}
        {packs.map(p => (
          <div key={p.id} className={cn(
            'grid grid-cols-[1fr_8rem_6rem_5rem_4rem] items-center border-b border-border/60 px-4 py-3 last:border-b-0',
            p.enabled ? '' : 'opacity-70',
          )}>
            <div className="min-w-0">
              <div className="flex items-center gap-1.5 text-sm text-foreground">
                {p.error ? <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-danger" /> : p.enabled ? <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-emerald-500" /> : <Package className="h-3.5 w-3.5 shrink-0 text-muted" />}
                <span className="truncate">{p.label || tx('(未验证)')}</span>
              </div>
              <div className="mt-0.5 truncate font-mono text-[11px] text-muted">{p.id}</div>
              {p.error && <div className="mt-1 text-[11px] leading-4 text-danger">{p.error}</div>}
            </div>
            <div className="truncate font-mono text-[11px] text-muted">{p.path || '—'}</div>
            <div className="text-[11px] leading-4 text-muted">
              <div>{fmtSize(p.size)}</div>
              <div>{fmtTime(p.uploaded_at)}</div>
            </div>
            <div className="flex justify-center">
              <button
                type="button"
                onClick={() => enabledMut.mutate({ id: p.id, enabled: !p.enabled })}
                disabled={!!p.error}
                className={cn(
                  'relative h-5 w-9 rounded-full transition-colors',
                  p.enabled ? 'bg-accent' : 'bg-elevated border border-border',
                  p.error && 'cursor-not-allowed opacity-40',
                )}
                title={p.error ? tx('校验失败的包不可启用') : p.enabled ? tx('停用 (刷新后生效)') : tx('启用 (刷新后生效)')}
              >
                <span className={cn(
                  'absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all',
                  p.enabled ? 'left-[1.125rem]' : 'left-0.5',
                )} />
              </button>
            </div>
            <div className="flex justify-center">
              <button
                type="button"
                onClick={() => { if (confirm(tx('删除页面插件 "{id}"?', { id: p.label || p.id }))) deleteMut.mutate(p.id) }}
                className="rounded p-1.5 text-muted hover:bg-danger/10 hover:text-danger transition-colors"
                title={tx('删除')}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        ))}
      </section>
    </div>
  )
}
