import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'
import DetailDrawer from '@/views/shared/DetailDrawer'
import MappingForm from '@/views/shared/MappingForm'
import {
  PARSER_NEXT_ACTION,
  formatCell,
  statusBadgeClass,
  type EnabledVersion,
  type MappingRow,
  type Row,
} from '@/views/shared/domain'

const ENDPOINT = '/api/parser'
const VERSIONS_ENDPOINT = '/api/formatter/versions'
const columns = [
  '模板编号',
  '目标字段数',
  '引用转换器编号',
  '引用版本号',
  '目标输出格式',
  '迁移结论',
]

const DETAIL_FIELDS = [
  { label: '模板编号', key: '模板编号' },
  { label: '状态', key: 'status' },
  { label: '目标字段数', key: '目标字段数' },
  { label: 'XPath表达式', key: 'XPath表达式' },
  { label: '正则匹配式', key: '正则匹配式' },
  { label: '类型转换规则', key: '类型转换规则' },
  { label: '默认填充值', key: '默认填充值' },
  { label: '多值分隔符', key: '多值分隔符' },
  { label: '嵌套结构深度', key: '嵌套结构深度' },
  { label: '引用转换器编号', key: '引用转换器编号' },
  { label: '引用版本号', key: '引用版本号' },
  { label: '目标输出格式', key: '目标输出格式' },
  { label: '最后输出格式样本（只读）', key: '最后输出格式样本', pre: true },
  { label: '迁移结论编号', key: '迁移结论' },
]

type FormState =
  | { mode: 'activate'; row: Row }
  | { mode: 'migrate'; row: Row }
  | null

export default function ParserView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [successMessage, setSuccessMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [busyId, setBusyId] = useState<number | null>(null)
  const [detail, setDetail] = useState<Row | null>(null)
  const [formState, setFormState] = useState<FormState>(null)
  const [versions, setVersions] = useState<EnabledVersion[]>([])
  const filterFields = useMemo(() => ['模板编号', '引用转换器编号', '目标输出格式'], [])

  const reload = useCallback(async () => {
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) throw new Error('解析模板列表读取失败')
      const payload = await response.json()
      const items = (payload.items ?? []) as Row[]
      setRows(items)
      setTotal(payload.total ?? 0)
      setDetail((current) => {
        if (current === null) return current
        return items.find((row) => String(row.id) === String(current.id)) ?? current
      })
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '字段解析器列表读取失败')
    }
  }, [filters])

  const loadVersions = useCallback(async () => {
    try {
      const response = await request(`${VERSIONS_ENDPOINT}?status=${encodeURIComponent('已启用')}`)
      if (response.ok) {
        const payload = await response.json()
        setVersions(payload.items ?? [])
      }
    } catch {
      setVersions([])
    }
  }, [])

  useEffect(() => {
    void reload()
    void loadVersions()
  }, [reload, loadVersions])

  async function runSimpleAction(action: string, row: Row) {
    if (busyId !== null) return
    setBusyId(Number(row.id))
    setErrorMessage('')
    setSuccessMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(payload.detail ?? '字段解析器动作未生效')
      setSuccessMessage(payload.message ?? '操作已生效')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '字段解析器操作失败')
    } finally {
      setBusyId(null)
    }
  }

  async function submitMapping(payload: {
    action: string
    formatterCode: string
    versionNo: string
    targetOutputFormat: string
    mappings: MappingRow[]
  }) {
    if (formState === null || busyId !== null) return
    setBusyId(Number(formState.row.id))
    setErrorMessage('')
    setSuccessMessage('')
    try {
      const response = await request(`${ENDPOINT}/${formState.row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: payload }),
      })
      const body = await response.json().catch(() => ({}))
      if (!response.ok) {
        // 兼容性/状态机冲突：后端已回滚，前端保留表单内容供修正，不丢用户输入
        throw new Error(body.detail ?? '提交被拦截，字段映射与模板状态均未改动')
      }
      setSuccessMessage(body.message ?? '字段映射与模板状态已在同一事务提交')
      setFormState(null)
      await reload()
      await loadVersions()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '字段解析器操作失败')
    } finally {
      setBusyId(null)
    }
  }

  function openForm(mode: 'activate' | 'migrate', row: Row) {
    setErrorMessage('')
    setFormState({ mode, row })
  }

  const stats = useMemo(() => ([
    { label: '解析模板总数', value: total },
    { label: '已生效模板', value: rows.filter((row) => row.status === '已生效').length },
    { label: '验证中模板', value: rows.filter((row) => row.status === '模板验证中').length },
    { label: '待复核模板', value: rows.filter((row) => row.status === '待复核').length },
  ]), [rows, total])

  const formRow = formState?.row ?? null

  return (
    <section className="page" data-module="parser">
      <header className="page-head">
        <div>
          <h2>字段解析器管理</h2>
          <p className="page-desc">模板只能引用「已启用」转换器版本；引用版本退役后模板自动进入待复核，保留最后输出格式样本并给出唯一迁移结论。</p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出字段解析器清单
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
            const status = String(row.status)
            const nextAction = PARSER_NEXT_ACTION[status]
            const needsMappingForm = nextAction === '生效模板' || nextAction === '迁移转换器引用'
            return (
              <tr key={String(row.id)}>
                <td><span className={statusBadgeClass(status)}>{status}</span></td>
                {columns.map((column) => <td key={column}>{formatCell(row[column])}</td>)}
                <td className="row-actions">
                  {nextAction ? (
                    <button
                      className="link"
                      type="button"
                      disabled={busyId !== null}
                      onClick={() => {
                        if (needsMappingForm) {
                          openForm(nextAction === '生效模板' ? 'activate' : 'migrate', row)
                        } else {
                          void runSimpleAction(nextAction, row)
                        }
                      }}
                    >
                      {busyId === Number(row.id) ? '提交中…' : nextAction}
                    </button>
                  ) : (
                    <span className="readonly-hint" style={{ margin: 0 }}>终态，无动作</span>
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
              <td className="empty-state" colSpan={columns.length + 3}>暂无字段解析器数据</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条字段解析器记录</span>
      </footer>

      <DetailDrawer title="解析模板详情" row={detail} fields={DETAIL_FIELDS} onClose={() => setDetail(null)}>
        {detail?.status === '待复核' && (
          <div>
            <p className="readonly-hint">
              引用的转换器版本已退役。上方「最后输出格式样本」为退役前最后一次完整输出，仅支持只读查看；
              迁移结论 {formatCell(detail.迁移结论)} 对所有引用同一版本的模板是同一条，全部迁移完成后结论自动关闭。
            </p>
            <button
              className="btn primary"
              type="button"
              disabled={busyId !== null}
              onClick={() => openForm('migrate', detail)}
            >
              迁移转换器引用
            </button>
          </div>
        )}
      </DetailDrawer>

      {formState && formRow && (
        <div className="drawer-mask" role="presentation" onClick={() => (busyId === null ? setFormState(null) : undefined)}>
          <aside className="drawer" role="dialog" aria-label="字段映射提交" onClick={(event) => event.stopPropagation()}>
            <header className="drawer-head">
              <h3>{String(formRow.模板编号)} · {formState.mode === 'activate' ? '生效模板' : '迁移引用'}</h3>
              <button className="btn ghost" type="button" disabled={busyId !== null} onClick={() => setFormState(null)}>关闭</button>
            </header>
            <MappingForm
              mode={formState.mode}
              versions={versions}
              defaultFormatterCode={formState.mode === 'migrate' ? undefined : String(formRow.引用转换器编号 || '')}
              defaultVersionNo={formState.mode === 'migrate' ? undefined : String(formRow.引用版本号 || 'v1')}
              defaultTargetOutput={String(formRow.目标输出格式 || '')}
              defaultMappings={undefined}
              submitting={busyId !== null}
              onSubmit={(payload) => void submitMapping(payload)}
              onCancel={() => setFormState(null)}
            />
          </aside>
        </div>
      )}
    </section>
  )
}
