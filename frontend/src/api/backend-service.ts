import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---- 质量事件归属 ----
export type AttributionItem = {
  归属编号: string
  事件编号: string
  关联规则?: string
  表名: string
  字段名: string
  业务域: string
  处置项?: string
  影响行数?: number
  复核原因?: string
}

export type DispatchResult = {
  责任组: string
  可见字段: {
    表名: string
    字段名: string
    字段键: string
    业务域: string
    责任组: string
    可写回: boolean
  }[]
  可认领处置项: AttributionItem[]
  已认领处置项: AttributionItem[]
  事件数: number
  待复核积压: number
}

export type GroupReport = {
  责任组: string
  计入事件数: number
  可认领处置项: number
  已认领处置项: number
  日报事件总数: number
  日报按组事件数: number
}

export async function backfillAttribution(): Promise<Record<string, number>> {
  const response = await request('/api/dq_incident_attribution/backfill', { method: 'POST' })
  if (!response.ok) throw new Error('归属回填失败')
  return await response.json()
}

export async function loadDispatch(group: string): Promise<DispatchResult> {
  const response = await request(
    `/api/dq_incident_attribution/dispatch?group=${encodeURIComponent(group)}`,
  )
  if (!response.ok) throw new Error('主数据校验派发读取失败')
  return await response.json()
}

export async function loadGroupReport(group: string): Promise<GroupReport> {
  const response = await request(
    `/api/dq_incident_attribution/report?group=${encodeURIComponent(group)}`,
  )
  if (!response.ok) throw new Error('归属日报读取失败')
  return await response.json()
}

export async function claimAttribution(
  attributionNo: string,
  group: string,
): Promise<Record<string, unknown>> {
  const id = Number(String(attributionNo).replace(/^ATTR-/, '')) || 0
  const response = await request(`/api/dq_incident_attribution/${id}/claim`, {
    method: 'POST',
    body: JSON.stringify({ group, incident_no: '' }),
  })
  if (!response.ok) throw new Error('认领失败')
  return await response.json()
}

export async function writeBackAttribution(
  attributionNo: string,
  group: string,
  conclusion: string,
): Promise<Record<string, unknown>> {
  const id = Number(String(attributionNo).replace(/^ATTR-/, '')) || 0
  const response = await request(`/api/dq_incident_attribution/${id}/write-back`, {
    method: 'POST',
    body: JSON.stringify({ group, values: { 处置结论: conclusion } }),
  })
  if (!response.ok) throw new Error('写回失败')
  return await response.json()
}

export async function loadReviewQueue(): Promise<{ items: AttributionItem[]; total: number }> {
  const response = await request('/api/dq_incident_attribution/review-queue')
  if (!response.ok) throw new Error('待复核队列读取失败')
  return await response.json()
}
