import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'
import DetailDrawer from '@/views/shared/DetailDrawer'
import { FORMATTER_NEXT_ACTION, formatCell, statusBadgeClass, type Row } from '@/views/shared/domain'

const ENDPOINT = '/api/formatter'
const columns = [
  '转换器编号',
  '版本号',
  '输入格式',
  '输出格式',
  '字段映射表',
  '退役时间',
  '后继转换器编号',
]

const DETAIL_FIELDS = [
  { label: '转换器编号', key: '转换器编号' },
  { label: '版本号', key: '版本号' },
  { label: '状态', key: 'status' },
  { label: '输入格式', key: '输入格式' },
  { label: '输出格式', key: '输出格式' },
  { label: '字段映射表', key: '字段映射表', pre: true },
  { label: '日期格式模板', key: '日期格式模板' },
  { label: '数值精度', key: '数值精度' },
  { label: '嵌套展平层数', key: '嵌套展平层数' },
  { label: '空值输出策略', key: '空值输出策略' },
  { label: '当前版本快照（最后一次完整输入输出格式样本）', key: '当前版本快照', pre: true },
  { label: '退役时间', key: '退役时间' },
  { label: '后继转换器编号', key: '后继转换器编号' },
]

type Conclusion = {
  conclusionCode: string
  retiredVersionCode: string
  status: string
  affectedTemplates: string[]
  recommendedVersionCode: string
  lastOutputFormat: string
}

export default function FormatterView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [successMessage, setSuccessMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [busyId, setBusyId] = useState<number | null>(null)
  const [detail, setDetail] = useState<Row | null>(null)
  const [conclusions, setConclusions] = useState<Conclusion[]>([])
  const filterFields = useMemo(() => ['转换器编号', '输入格式', '输出格式'], [])

  const reload = useCallback(async () => {
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) throw new Error('转换器列表读取失败')
      const payload = await response.json()
      const items = (payload.items ?? []) as Row[]
      setRows(items)
      setTotal(payload.total ?? 0)
      // 抽屉若开着，用刷新后的同 id 行替换，杜绝列表/详情两份结论
      setDetail((current) => {
        if (current === null) return current
        return items.find((row) => String(row.id) === String(current.id)) ?? current
      })
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '格式转换列表读取失败')
    }
  }, [filters])

  const reloadConclusions = useCallback(async () => {
    try {
      const response = await request(`${ENDPOINT}/migration-conclusions`)
      if (response.ok) {
        const payload = await response.json()
        setConclusions(payload.items ?? [])
      }
    } catch {
      // 结论区失败不阻断主流程
    }
  }, [])

  useEffect(() => {
    void reload()
    void reloadConclusions()
  }, [reload, reloadConclusions])

  async function runAction(action: string, row: Row) {
    if (busyId !== null) return // 提交中忽略重复点击
    setBusyId(Number(row.id))
    setErrorMessage('')
    setSuccessMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(payload.detail ?? '格式转换动作未生效')
      }
      setSuccessMessage(payload.message ?? '操作已生效')
      await reload()
      await reloadConclusions()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '格式转换操作失败')
    } finally {
      setBusyId(null)
    }
  }

  async function createSuccessor(row: Row) {
    if (busyId !== null) return
    setBusyId(Number(row.id))
    setErrorMessage('')
    setSuccessMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/successor`, { method: 'POST' })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(payload.detail ?? '后继转换器登记失败')
      setSuccessMessage(payload.message ?? '后继转换器已登记')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '后继转换器登记失败')
    } finally {
      setBusyId(null)
    }
  }

  const stats = useMemo(() => ([
    { label: '转换器总数', value: total },
    { label: '已启用转换器', value: rows.filter((row) => row.status === '已启用').length },
    { label: '测试中转换器', value: rows.filter((row) => row.status === '转换测试中').length },
    { label: '已退役（待迁移模板关联）', value: rows.filter((row) => row.status === '已退役').length },
  ]), [rows, total])

  return (
    <section className="page" data-module="formatter">
      <header className="page-head">
        <div>
          <h2>格式转换管理</h2>
          <p className="page-desc">转换器只能按 草稿→转换测试中→已启用→已退役 单向推进；启用冻结版本快照，退役同步挂起引用模板。</p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出格式转换清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      {successMessage && <div className="notice-bar success">{successMessage}</div>}
      {errorMessage && <div className="notice-bar error">{errorMessage}</div>}

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            <th>状态</th>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
            <th>详情</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const nextAction = FORMATTER_NEXT_ACTION[String(row.status)]
            const retired = row.status === '已退役'
            return (
              <tr key={String(row.id)}>
                <td><span className={statusBadgeClass(String(row.status))}>{String(row.status)}</span></td>
                {columns.map((column) => <td key={column}>{formatCell(row[column])}</td>)}
                <td className="row-actions">
                  {nextAction ? (
                    <button
                      className="link"
                      type="button"
                      disabled={busyId !== null}
                      onClick={() => void runAction(nextAction, row)}
                    >
                      {busyId === Number(row.id) ? '提交中…' : nextAction}
                    </button>
                  ) : (
                    <span className="readonly-hint" style={{ margin: 0 }}>终态，无动作</span>
                  )}
                  {retired && (
                    <>
                      <span className="action-sep">|</span>
                      <button
                        className="link"
                        type="button"
                        disabled={busyId !== null || Boolean(row.后继转换器编号)}
                        title={row.后继转换器编号 ? '后继已登记，不能重复创建' : '基于退役快照登记后继草稿'}
                        onClick={() => void createSuccessor(row)}
                      >
                        登记后继
                      </button>
                    </>
                  )}
                </td>
                <td>
                  <button className="link" type="button" onClick={() => setDetail(row)}>查看</button>
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 3}>暂无格式转换数据</td>
            </tr>
          )}
        </tbody>
      </table>

      {conclusions.length > 0 && (
        <section style={{ marginTop: 16 }}>
          <h3>退役迁移结论（每个退役版本唯一一条）</h3>
          <table className="data-table">
            <thead>
              <tr>
                <th>结论编号</th><th>退役版本</th><th>状态</th><th>受影响模板</th><th>建议启用版本</th><th>最后输出格式（只读）</th>
              </tr>
            </thead>
            <tbody>
              {conclusions.map((item) => (
                <tr key={item.conclusionCode}>
                  <td>{item.conclusionCode}</td>
                  <td>{item.retiredVersionCode}</td>
                  <td><span className={statusBadgeClass(item.status === '已完成' ? '已生效' : '待复核')}>{item.status}</span></td>
                  <td>{(item.affectedTemplates ?? []).join('、') || '—'}</td>
                  <td>{item.recommendedVersionCode || '待迁移'}</td>
                  <td>{item.lastOutputFormat || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条格式转换记录</span>
      </footer>

      <DetailDrawer title="转换器详情" row={detail} fields={DETAIL_FIELDS} onClose={() => setDetail(null)}>
        {detail?.status === '已退役' && (
          <p className="readonly-hint">该转换器已退役，快照为最后一次完整输入输出格式样本，仅支持只读查看；重新启用请登记后继版本并重新走测试、启用。</p>
        )}
      </DetailDrawer>
    </section>
  )
}
