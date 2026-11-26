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

export type AnyRecord = Record<string, any>

async function attemptApi<T = AnyRecord>(path: string, init?: RequestInit): Promise<T> {
  const response = await request(path, init)
  const data = (await response.json().catch(() => ({}))) as T & { message?: string }
  if (!response.ok || (data as AnyRecord).ok === false) {
    throw new Error((data as { message?: string }).message || `接口返回 ${response.status}`)
  }
  return data
}

function postJson(path: string, body: AnyRecord = {}): Promise<AnyRecord> {
  return attemptApi(path, { method: 'POST', body: JSON.stringify(body) })
}

export const attemptService = {
  missionHistory(missionId: number | string): Promise<AnyRecord> {
    return attemptApi(`/api/crawl_mission/${missionId}/attempts`)
  },
  claimMissionAttempt(missionId: number | string, planId?: number | string): Promise<AnyRecord> {
    return postJson(`/api/crawl_mission/${missionId}/attempts/claim`, {
      plan_id: planId || undefined,
      scheduler_plan_id: planId || undefined,
    })
  },
  reportMissionAttempt(
    missionId: number | string,
    attemptId: number | string,
    body: AnyRecord,
  ): Promise<AnyRecord> {
    return postJson(`/api/crawl_mission/${missionId}/attempts/${attemptId}/receipt`, body)
  },
  redispatchMission(missionId: number | string, attemptId?: number | string, planId?: number | string) {
    return postJson(`/api/crawl_mission/${missionId}/redispatch`, {
      attempt_id: attemptId,
      plan_id: planId,
    })
  },
  schedulerTodos(planId: number | string): Promise<AnyRecord> {
    return attemptApi(`/api/scheduler/${planId}/todos`)
  },
  schedulerAttempts(planId: number | string): Promise<AnyRecord> {
    return attemptApi(`/api/scheduler/${planId}/attempts`)
  },
  schedulerRedispatchable(planId: number | string): Promise<AnyRecord> {
    return attemptApi(`/api/scheduler/${planId}/redispatchable`)
  },
  dispatchNext(planId: number | string, missionId?: number | string): Promise<AnyRecord> {
    return postJson(`/api/scheduler/${planId}/dispatch-next`, { mission_id: missionId })
  },
  enqueueMission(planId: number | string, missionId: number | string, priority = 1): Promise<AnyRecord> {
    // 通用动作不包含入队路由；使用调度服务的兼容专用路由。
    return postJson(`/api/scheduler/${planId}/enqueue`, { mission_id: missionId, priority })
  },
  reportSchedulerAttempt(
    planId: number | string,
    attemptId: number | string,
    body: AnyRecord,
  ): Promise<AnyRecord> {
    return postJson(`/api/scheduler/${planId}/attempts/${attemptId}/receipt`, body)
  },
  redispatchNext(planId: number | string, body: AnyRecord = {}): Promise<AnyRecord> {
    return postJson(`/api/scheduler/${planId}/redispatch-next`, body)
  },
  recover(): Promise<AnyRecord> {
    return postJson('/api/attempts/recover')
  },
}
