import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/ods'
const columns = [
  "表编号",
  "表名称",
  "来源系统",
  "关联批次编号",
  "snapshot_id",
  "可用性判断",
  "状态"
]

type Availability = { available: boolean; snapshot_id: string | null; reason: string }
type Row = Record<string, string | number | null> & {
  id: number
  status: string
  snapshot_id?: string | null
  关联批次编号?: string | null
  可用性判断?: Availability
}

type ReadableBatch = {
  批次编号: string
  来源系统: string
  落地路径: string
  文件格式: string
  原始行数: number
  snapshot_id: string
  completed_at: string
  批次状态: string
}

export default function OdsView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [readable, setReadable] = useState<ReadableBatch[]>([])
  const [admitTarget, setAdmitTarget] = useState<Row | null>(null)
  const [batchNo, setBatchNo] = useState('')
  const filterFields = useMemo(() => ['表编号', '表名称', '来源系统'], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const [listResponse, readableResponse] = await Promise.all([
        request(`${ENDPOINT}?${query}`),
        request(`${ENDPOINT}/readable-batches`),
      ])
      if (!listResponse.ok) {
        throw new Error('贴源表列表读取失败')
      }
      const payload = await listResponse.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      if (readableResponse.ok) {
        const readablePayload = await readableResponse.json()
        setReadable(readablePayload.items ?? [])
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '贴源层列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '贴源层动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '贴源层操作失败')
    }
  }

  function openAdmit(row: Row) {
    setAdmitTarget(row)
    setBatchNo(row['关联批次编号'] ? String(row['关联批次编号']) : '')
  }

  async function submitAdmit() {
    if (!admitTarget) return
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${admitTarget.id}/admit`, {
        method: 'POST',
        body: JSON.stringify({ values: { 批次编号: batchNo } }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        const codes = (payload.codes ?? []).join('、')
        throw new Error(codes ? `${payload.message}（${codes}）` : payload.message)
      }
      setAdmitTarget(null)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '贴源准入失败')
    }
  }

  function renderCell(row: Row, column: string): string {
    if (column === '状态') return row.status
    if (column === '可用性判断') {
      const judge = row['可用性判断']
      if (!judge) return '—'
      return judge.available ? `可用（${judge.snapshot_id ?? ''}）` : `不可用：${judge.reason}`
    }
    const value = row[column]
    return value === null || value === undefined || value === '' ? '—' : String(value)
  }

  return (
    <section className="page" data-module="ods">
      <header className="page-head">
        <div>
          <h2>贴源层管理</h2>
          <p className="page-desc">贴源只读取校验通过（PASSED 快照）的落地批次；可用性判断与落地列表同源，冲突以已提交完整快照为准。</p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出贴源层清单
          </button>
        </div>
      </header>

      <h3>贴源可读取的落地版本（仅校验通过批次）</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>批次编号</th><th>来源系统</th><th>文件格式</th><th>原始行数</th>
            <th>snapshot_id</th><th>完成时刻</th><th>落地批次状态</th>
          </tr>
        </thead>
        <tbody>
          {readable.map((batch) => (
            <tr key={batch.snapshot_id}>
              <td>{batch['批次编号']}</td>
              <td>{batch['来源系统']}</td>
              <td>{batch['文件格式']}</td>
              <td>{batch['原始行数']}</td>
              <td>{batch.snapshot_id}</td>
              <td>{batch.completed_at}</td>
              <td>{batch['批次状态']}</td>
            </tr>
          ))}
          {readable.length === 0 && (
            <tr><td className="empty-state" colSpan={7}>暂无校验通过的落地批次</td></tr>
          )}
        </tbody>
      </table>

      <h3 style={{ marginTop: 20 }}>贴源表登记</h3>
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
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{renderCell(row, column)}</td>)}
              <td className="row-actions">
                <button className="link" type="button" onClick={() => openAdmit(row)}>
                  贴源准入（选择校验通过批次）
                </button>
                <button
                  className="link" type="button"
                  disabled={row.status !== '建表审批中'}
                  onClick={() => void runAction('确认上线', row)}
                >
                  确认上线
                </button>
                <button
                  className="link" type="button"
                  disabled={row.status === '已下线'}
                  onClick={() => void runAction('下线表', row)}
                >
                  下线表
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无贴源层数据</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条贴源层记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      {admitTarget && (
        <div className="modal-mask" role="dialog" aria-modal="true">
          <div className="modal">
            <h3>贴源准入：{admitTarget['表编号']}</h3>
            <p className="page-desc">只有带 PASSED 校验快照的批次可准入；未通过校验的批次会被拒绝且不留半套审计。</p>
            <label className="modal-field">
              <span>落地批次编号</span>
              <input value={batchNo} placeholder="如 LAND-20261113-001" onChange={(event) => setBatchNo(event.target.value)} />
            </label>
            <div className="modal-actions">
              <button className="btn ghost" type="button" onClick={() => setAdmitTarget(null)}>取消</button>
              <button className="btn primary" type="button" onClick={() => void submitAdmit()}>确认准入</button>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
