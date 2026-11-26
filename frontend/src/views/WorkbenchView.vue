<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="row in rows" :key="String(row.id)">
        <tr :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <span v-if="meta.key === 'crawl_mission'" :data-testid="`toggle-attempt-history-${row.id}`" class="testid-alias">
            <button
              v-if="meta.key === 'crawl_mission'"
              class="link"
              type="button"
              :data-testid="`attempt-history-toggle-${row.id}`"
              aria-label="尝试历史"
              :title="expandedRows[Number(row.id)] ? '收起尝试历史' : '展开尝试历史'"
              @click="toggleRow(Number(row.id))"
            >
              {{ expandedRows[Number(row.id)] ? '收起尝试历史' : '展开尝试历史' }}
            </button>
            </span>
            <span v-if="meta.key === 'scheduler'" :data-testid="`toggle-scheduler-attempts-${row.id}`" class="testid-alias">
            <button
              v-if="meta.key === 'scheduler'"
              class="link"
              type="button"
              :data-testid="`scheduler-attempt-toggle-${row.id}`"
              aria-label="调度待办 登记回报"
              :title="expandedRows[Number(row.id)] ? '收起调度回报' : '进入调度待办并登记回报'"
              @click="toggleRow(Number(row.id))"
            >
              {{ expandedRows[Number(row.id)] ? '收起调度回报' : '调度待办 / 登记回报' }}
            </button>
            </span>
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="expandedRows[Number(row.id)]" :data-testid="`expanded-panel-${row.id}`" class="expanded-row">
          <td :colspan="meta.fields.length + 2">
            <AttemptHistoryPanel
              v-if="meta.key === 'crawl_mission'"
              :mission-id="Number(row.id)"
              :plan-id="planIdOf(row)"
              @changed="reload"
            />
            <SchedulerAttemptPanel
              v-else-if="meta.key === 'scheduler'"
              :plan-id="Number(row.id)"
              :highlight-attempt-id="highlightAttemptId"
              @changed="reload"
            />
          </td>
        </tr>
        </template>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import AttemptHistoryPanel from '@/components/AttemptHistoryPanel.vue'
import SchedulerAttemptPanel from '@/components/SchedulerAttemptPanel.vue'
import {
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const route = useRoute()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const expandedRows = ref<Record<number, boolean>>({})
const highlightAttemptId = ref<string>('')

function syncRouteExpansion() {
  const planValue = route.query.plan ?? route.query.plan_id
  const planId = Number(Array.isArray(planValue) ? planValue[0] : planValue)
  if (meta.key === 'scheduler' && Number.isInteger(planId) && planId > 0) {
    expandedRows.value[planId] = true
  }
  const attemptValue = route.query.attempt ?? route.query.attempt_id
  highlightAttemptId.value = String(Array.isArray(attemptValue) ? attemptValue[0] : attemptValue ?? '')
}

function toggleRow(id: number) {
  expandedRows.value[id] = !expandedRows.value[id]
}

function planIdOf(row: EntryRow): number | string | undefined {
  const value = row._scheduler_plan_id ?? row.scheduler_plan_id ?? row.plan_id
  if (typeof value === 'number' || typeof value === 'string') return value
  return undefined
}
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(() => {
  syncRouteExpansion()
  void reload()
})

watch(() => route.query, () => {
  syncRouteExpansion()
})
</script>
