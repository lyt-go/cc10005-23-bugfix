import { useEffect } from 'react'

import { formatCell, statusBadgeClass, type Row } from './domain'

type DetailField = { label: string; key: string; pre?: boolean }

type DetailDrawerProps = {
  title: string
  row: Row | null
  fields: DetailField[]
  onClose: () => void
  children?: React.ReactNode
}

/**
 * 列表与详情共用同一份行数据：抽屉直接吃列表刷新后的 row，
 * 不再单独发请求，避免“列表一个结论、详情另一个结论”的错位。
 */
export default function DetailDrawer({ title, row, fields, onClose, children }: DetailDrawerProps) {
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  if (row === null) return null
  return (
    <div className="drawer-mask" role="presentation" onClick={onClose}>
      <aside className="drawer" role="dialog" aria-label={title} onClick={(event) => event.stopPropagation()}>
        <header className="drawer-head">
          <div>
            <h3>{title}</h3>
            <span className={statusBadgeClass(String(row.status ?? ''))}>{String(row.status ?? '—')}</span>
          </div>
          <button className="btn ghost" type="button" onClick={onClose}>关闭</button>
        </header>
        <dl className="drawer-body">
          {fields.map((field) => (
            <div className="drawer-field" key={field.key}>
              <dt>{field.label}</dt>
              <dd>
                {field.pre ? (
                  <pre className="readonly-sample">{formatCell(row[field.key])}</pre>
                ) : (
                  formatCell(row[field.key])
                )}
              </dd>
            </div>
          ))}
        </dl>
        {children}
      </aside>
    </div>
  )
}
