import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { DndContext, closestCenter, KeyboardSensor, PointerSensor, useDroppable, useSensor, useSensors, type DragEndEvent } from '@dnd-kit/core'
import { arrayMove, SortableContext, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { Eye, EyeOff, ExternalLink, GripVertical, Bell, Trash2, Folder, FolderMinus, LockKeyhole, Plus } from 'lucide-react'
import { Link } from 'react-router-dom'
import { api, type Preferences } from '@/lib/api'
import { setUiPref } from '@/lib/uiPrefs'
import { toast } from '@/components/Toast'
import { QK } from '@/lib/queryKeys'
import { usePreferences } from '@/lib/useSharedQueries'
import { getNavigationEntries, type NavigationEntry } from '@/lib/navigation'
import { canonicalNavId, dissolveNavGroup, moveNavPage, navPageIds, resolveNavLayout, type NavGroup, type NavLayout, type NavNode } from '@/lib/navLayout'
import { cn } from '@/lib/cn'
import { useI18n } from '@/lib/i18n'

function SortableMenuRow({ node, entry, parentId, groups, hidden, busy, focusNew, onMove, onHide, onRename, onDissolve,
  badgeEnabled, onToggleBadge, onRemove }: {
  node: NavNode; entry?: NavigationEntry; parentId: string | null; groups: NavGroup[]; hidden: boolean; busy: boolean; focusNew: boolean
  onMove: (id: string, parent: string | null) => void; onHide: (id: string) => void
  onRename: (id: string, label: string) => void; onDissolve: (id: string) => void
  badgeEnabled: boolean; onToggleBadge: () => void; onRemove: (entry: NavigationEntry) => void
}) {
  const { tx } = useI18n()
  const home = node.id === '/'
  const { attributes, listeners, setNodeRef, transform, transition, isDragging, isOver } = useSortable({
    id: node.id, disabled: home || busy, data: { parentId, type: node.type },
  })
  const label = node.type === 'group' ? node.label : entry?.label ?? node.id
  const Icon = node.type === 'group' ? Folder : entry?.icon ?? ExternalLink
  return <div ref={setNodeRef} style={{ transform: CSS.Transform.toString(transform), transition }}
    className={cn('relative flex flex-wrap items-center gap-2 border-b border-border/60 px-3 py-3 last:border-b-0 sm:px-4',
      parentId && 'pl-9 sm:pl-10', isDragging && 'z-10 bg-elevated shadow-lg', isOver && 'bg-accent/10', hidden && 'opacity-60')}
    data-nav-id={node.id}>
    <button type="button" {...attributes} {...listeners} disabled={home || busy} aria-label={tx('拖动{label}', { label })}
      className="touch-none rounded p-1 text-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-accent/50 disabled:opacity-30">
      {home ? <LockKeyhole className="h-4 w-4" /> : <GripVertical className="h-4 w-4" />}
    </button>
    <Icon className="h-4 w-4 shrink-0 text-muted" />
    <div className="min-w-0 flex-1 basis-28">
      {node.type === 'group' ? <input aria-label={tx('分类名称：{label}', { label })} maxLength={32} value={node.label} disabled={busy} autoFocus={focusNew}
        onChange={e => onRename(node.id, e.target.value)} className="h-8 w-full rounded border border-transparent bg-transparent px-0 text-sm font-medium text-foreground hover:border-border focus:border-accent focus:outline-none" />
        : <div className="truncate text-sm font-medium text-foreground">{label}{home && <span className="ml-2 text-xs font-normal text-muted">{tx('固定置顶')}</span>}</div>}
      <div className="truncate text-[11px] text-muted">
        {node.type === 'group' ? tx('{n} 个菜单 · 拖到分类名称上可移入', { n: node.children.length }) : !entry ? tx('页面暂不可用，归属已保留')
          : !entry.visible ? tx('页面已停用，启用后在原分类恢复') : entry.type === 'builtin' ? tx('内置页面') : entry.type === 'analysis' ? tx('自定义分析页面') : tx('扩展页面')}
      </div>
    </div>
    {node.type === 'page' && !home && <select aria-label={tx('移动{label}到', { label })} value={parentId ?? ''} disabled={busy}
      onChange={e => onMove(node.id, e.target.value || null)}
      className="h-8 w-32 max-w-full rounded-lg border border-border bg-base px-2 text-xs text-foreground focus:outline-none focus:ring-2 focus:ring-accent/30">
      <option value="">{tx('顶层菜单')}</option>{groups.map(group => <option key={group.id} value={group.id}>{group.label || tx('未命名分类')}</option>)}
    </select>}
    <div className="flex items-center gap-1">
      {node.type === 'group' ? <button type="button" disabled={busy} onClick={() => onDissolve(node.id)} aria-label={tx('解散{label}', { label })}
        title={tx('解散分类，子菜单移回顶层')} className="rounded p-2 text-muted hover:bg-elevated hover:text-foreground disabled:opacity-40"><FolderMinus className="h-4 w-4" /></button>
        : <>
          <button type="button" disabled={home || busy} onClick={() => onHide(node.id)} aria-label={tx('{action}{label}', { action: hidden ? tx('显示') : tx('隐藏'), label })}
            className="rounded p-2 text-muted hover:bg-elevated hover:text-accent disabled:opacity-30">{hidden ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}</button>
          {entry && <Link to={entry.type === 'analysis' ? '/settings?tab=pages&section=ext-pages' : entry.to}
            aria-label={tx('打开{label}', { label })} className="rounded p-2 text-muted hover:bg-elevated hover:text-accent"><ExternalLink className="h-4 w-4" /></Link>}
          {node.id === '/monitor' && <button type="button" aria-label={badgeEnabled ? tx('关闭监控数字提示') : tx('开启监控数字提示')} onClick={onToggleBadge}
            className="rounded p-2 text-muted hover:bg-elevated hover:text-accent"><Bell className="h-4 w-4" /></button>}
          {entry?.extensionId && <button type="button" disabled={busy} onClick={() => onRemove(entry)} aria-label={tx('删除源码扩展页{label}', { label })}
            className="rounded p-2 text-muted hover:bg-danger/10 hover:text-danger disabled:opacity-30"><Trash2 className="h-4 w-4" /></button>}
        </>}
    </div>
  </div>
}

function RootDropTarget({ disabled }: { disabled: boolean }) {
  const { tx } = useI18n()
  const { setNodeRef, isOver } = useDroppable({ id: 'nav:root', disabled })
  return <div ref={setNodeRef} className={cn('p-4 text-center text-xs text-muted', isOver && 'bg-accent/10 text-accent')}>{tx('拖到这里移回顶层，也可使用每行的归属选择框')}</div>
}

export function SettingsMenuSettingsPanel() {
  const { tx } = useI18n()
  const qc = useQueryClient()
  const { data: prefs } = usePreferences()
  const menus = useQuery({ queryKey: QK.analysisMenus, queryFn: api.analysisMenus })
  const [removedNs, setRemovedNs] = useState<Set<string>>(new Set())
  const entries = getNavigationEntries(menus.data?.items ?? []).filter(x => !x.extensionId || !removedNs.has(x.extensionId))
  const entryMap = new Map(entries.map(x => [x.id, x]))
  const [draft, setDraft] = useState<NavLayout | null>(null)
  const [hiddenDraft, setHiddenDraft] = useState<string[] | null>(null)
  const [newGroupName, setNewGroupName] = useState('')
  const [newGroupId, setNewGroupId] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)
  const layout = resolveNavLayout(draft ?? prefs?.nav_layout, entries, prefs?.nav_order)
  const hidden = hiddenDraft ?? (prefs?.nav_hidden ?? []).map(canonicalNavId).filter(id => id !== '/' && id !== '/settings')
  const dirty = draft !== null || hiddenDraft !== null
  const groups = layout.items.filter((x): x is NavGroup => x.type === 'group')
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }))
  const save = useMutation({
    mutationFn: () => api.saveNavLayout(layout, hidden),
    onSuccess: result => {
      qc.setQueryData<Preferences>(QK.preferences, previous => previous ? { ...previous, ...result } : previous)
      setDraft(null); setHiddenDraft(null); setSaved(true); setError('')
      void qc.invalidateQueries({ queryKey: QK.preferences })
      toast(tx('菜单布局已保存'), 'success')
    },
    onError: (e: Error) => setError(e.message || tx('保存失败，修改已保留，请重试')),
  })
  const busy = save.isPending || !prefs || menus.isLoading || menus.isError
  const change = (value: NavLayout) => { setDraft(value); setSaved(false); setError('') }
  const onMove = (id: string, parent: string | null) => change(moveNavPage(layout, id, parent))
  const onHide = (id: string) => {
    if (id === '/') return
    setHiddenDraft(hidden.includes(id) ? hidden.filter(x => x !== id) : [...hidden, id]); setSaved(false)
  }
  const handleDragEnd = ({ active, over }: DragEndEvent) => {
    if (!over || active.id === over.id || active.id === '/' || busy) return
    const id = String(active.id)
    const root = layout.items.find(x => x.id === id)
    if (root?.type === 'group') {
      const target = layout.items.find(x => x.id === over.id || navPageIds(x).includes(String(over.id)))
      const from = layout.items.findIndex(x => x.id === id)
      const to = over.id === 'nav:root' ? layout.items.length - 1 : layout.items.findIndex(x => x.id === target?.id)
      if (to > 0) change({ ...layout, items: arrayMove(layout.items, from, to) })
      return
    }
    if (over.id === 'nav:root') return onMove(id, null)
    const group = groups.find(x => x.id === over.id || x.children.some(page => page.id === over.id))
    const oldGroup = groups.find(x => x.children.some(page => page.id === id))
    if (group?.id === oldGroup?.id && over.id !== group?.id) {
      const siblings = group ? group.children : layout.items
      const from = siblings.findIndex(x => x.id === id)
      const to = siblings.findIndex(x => x.id === over.id)
      if (from >= 0 && to >= (group ? 0 : 1)) {
        if (group) change({ ...layout, items: layout.items.map(x => x.id === group.id
          ? { ...group, children: arrayMove(group.children, from, to) } : x) })
        else change({ ...layout, items: arrayMove(layout.items, from, to) })
      }
      return
    }
    change(moveNavPage(layout, id, group?.id ?? null, group?.id === over.id ? undefined : String(over.id)))
  }
  const addGroup = () => {
    const label = newGroupName.trim()
    if (!label) return
    const id = globalThis.crypto?.randomUUID?.() ?? `${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`
    const group: NavGroup = { type: 'group', id: `group:${id}`, label, children: [] }
    change({ ...layout, items: [layout.items[0], group, ...layout.items.slice(1)] })
    setNewGroupId(group.id)
    setNewGroupName('')
  }
  const [badgeEnabled, setBadgeEnabled] = useState(() => {
    try { return localStorage.getItem('monitor_badge_enabled') !== '0' } catch { return true }
  })
  const toggleBadge = () => {
    setBadgeEnabled(!badgeEnabled); setUiPref('monitor_badge_enabled', badgeEnabled ? '0' : '1')
  }
  const [deletedBackup, setDeletedBackup] = useState<{ ns: string; backup: string } | null>(null)
  const deleteExtension = useMutation({
    mutationFn: (ns: string) => api.pageAdminDeleteSourceExtension(ns),
    onSuccess: r => {
      setRemovedNs(previous => new Set(previous).add(r.deleted))
      setDeletedBackup({ ns: r.deleted, backup: r.backup })
      toast(tx('已删除 {ns}（已备份），刷新后移除页面', { ns: r.deleted }), 'success')
    },
    onError: (e: Error) => toast(e.message || tx('删除失败'), 'error'),
  })
  const remove = (entry: NavigationEntry) => {
    if (dirty) { setError(tx('请先保存或撤销布局修改，再删除扩展页')); return }
    const ns = entry.extensionId
    if (!ns || !confirm(tx('删除源码扩展页「{label}」？将先备份再物理删除 frontend/src/custom/{ns}/。', { label: entry.label, ns }))) return
    if (confirm(tx('再次确认删除 {ns}？页面入口将于刷新后移除。', { ns }))) deleteExtension.mutate(ns)
  }
  const row = (node: NavNode, parentId: string | null = null) => <SortableMenuRow key={node.id} node={node} entry={entryMap.get(node.id)}
    parentId={parentId} groups={groups} hidden={hidden.includes(node.id)} busy={busy || deleteExtension.isPending} focusNew={node.id === newGroupId}
    onMove={onMove} onHide={onHide} onRename={(id, label) => change({ ...layout, items: layout.items.map(x => x.type === 'group' && x.id === id ? { ...x, label } : x) })}
    onDissolve={id => change(dissolveNavGroup(layout, id))} badgeEnabled={badgeEnabled} onToggleBadge={toggleBadge} onRemove={remove} />
  return <div className="max-w-5xl space-y-5">
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div><h2 className="text-xl font-semibold text-foreground">{tx('组织左侧菜单')}</h2>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-secondary">{tx('把常用页面归入分类，缩短侧栏。看板固定置顶，设置固定在侧栏底部；其他菜单可拖动或选择归属，自选保留原有分组。')}</p></div>
      <Link to="/settings?tab=pages&section=ext-pages" className="rounded-lg border border-border px-3 py-2 text-xs text-secondary hover:bg-elevated">{tx('管理扩展页面')}</Link>
    </div>
    <div className="flex flex-wrap items-center gap-2">
      <input aria-label={tx('新分类名称')} placeholder={tx('分类名称，例如：研究分析')} maxLength={32} value={newGroupName} disabled={busy}
        onChange={e => setNewGroupName(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') addGroup() }}
        className="h-9 min-w-0 flex-1 basis-48 rounded-lg border border-border bg-base px-3 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-accent/30" />
      <button type="button" onClick={addGroup} disabled={busy || !newGroupName.trim()} className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-border px-3 text-xs text-foreground hover:bg-elevated disabled:opacity-40"><Plus className="h-4 w-4" />{tx('新建分类')}</button>
    </div>
    {menus.isError && <p role="alert" className="text-sm text-danger">{tx('菜单加载失败，请刷新后重试。现有布局保持不变。')}</p>}
    <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
      <div className="rounded-xl border border-border bg-surface">
        <SortableContext items={layout.items.flatMap(node => [node.id, ...(node.type === 'group' ? node.children.map(x => x.id) : [])])} strategy={verticalListSortingStrategy}>
          {layout.items.map(node => <div key={node.id}>{row(node)}{node.type === 'group' && <div className="bg-elevated/20">
            {node.children.length ? node.children.map(page => row(page, node.id)) : <p className="px-12 py-3 text-xs text-muted">{tx('暂无子菜单，可拖入页面或通过归属选择框移入；保存后分类显示在侧栏。')}</p>}
          </div>}</div>)}
        </SortableContext>
        <RootDropTarget disabled={busy} />
      </div>
    </DndContext>
    {error && <p role="alert" className="rounded-lg bg-danger/10 p-3 text-sm text-danger">{error}</p>}
    <div className="sticky bottom-0 flex flex-wrap items-center gap-2 border-t border-border bg-base py-3">
      <span role="status" className="mr-auto text-xs text-muted">{!prefs || menus.isLoading ? tx('正在读取菜单…') : save.isPending ? tx('正在保存…') : dirty ? tx('有未保存的修改') : saved ? tx('布局已保存') : tx('修改后点击保存，侧栏立即更新')}</span>
      <button type="button" disabled={busy} onClick={() => change({ v: 1, items: layout.items.flatMap<NavNode>(node => node.type === 'group' ? node.children : [node]) })}
        className="rounded-lg px-3 py-2 text-xs text-secondary hover:bg-elevated disabled:opacity-40">{tx('恢复平铺')}</button>
      <button type="button" disabled={busy || !dirty} onClick={() => { setDraft(null); setHiddenDraft(null); setError('') }}
        className="rounded-lg border border-border px-3 py-2 text-xs text-secondary hover:bg-elevated disabled:opacity-40">{tx('撤销修改')}</button>
      <button type="button" disabled={busy || !dirty} onClick={() => save.mutate()} className="rounded-lg bg-accent px-4 py-2 text-xs font-medium text-white hover:bg-accent/90 disabled:opacity-40">{tx('保存布局')}</button>
    </div>
    {deletedBackup && <div className="text-sm text-secondary">{tx('已备份 {ns}：', { ns: deletedBackup.ns })}：<a className="text-accent underline" href={api.pageAdminBackupUrl(deletedBackup.backup)} download>{tx('下载备份')}</a></div>}
  </div>
}
