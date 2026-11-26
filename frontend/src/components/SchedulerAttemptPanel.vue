<template>
  <section
    class="attempt-panel scheduler-panel"
    data-testid="scheduler-attempt-panel"
    :data-plan-id="planId"
  >
    <div class="attempt-toolbar">
      <strong data-testid="scheduler-todos">调度待办 / 同次尝试登记回报</strong>
      <label class="inline-field">
        <span>任务ID</span>
        <input v-model="missionId" data-testid="enqueue-mission-id-input" placeholder="mission id" inputmode="numeric" />
      </label>
      <label class="inline-field small">
        <span>优先级</span>
        <input v-model="priority" data-testid="enqueue-priority-input" inputmode="numeric" />
      </label>
      <button class="btn" type="button" data-testid="enqueue-mission-button" @click="enqueue">加入队列</button>
      <button class="btn primary" type="button" data-testid="dispatch-next-button" @click="dispatchNext">
        派发下一项并生成编号
      </button>
      <button class="btn" type="button" data-testid="redispatch-next-button" @click="redispatchNext">
        查询并重派可重派项
      </button>
      <button class="btn ghost" type="button" data-testid="refresh-scheduler-todos" @click="load">刷新</button>
    </div>

    <p v-if="loading" class="hint">调度待办读取中…</p>
    <p v-else-if="error" class="error-text" data-testid="scheduler-attempt-error">{{ error }}</p>

    <div class="redispatch-strip" data-testid="redispatchable-list">
      <span>当前可重派项：{{ redispatchable.length }}</span>
      <span v-for="item in redispatchable" :key="String(item.attempt_id)" class="redispatch-chip"
            :data-testid="`redispatchable-item-${item.mission_id}`"
            :data-attempt-id="String(item.attempt_id)">
        任务 {{ item.mission_id }} / 尝试 #{{ item.attempt_id }}
      </span>
      <span v-if="!redispatchable.length" class="hint">无（最新尝试成功后，旧回报不会让任务重新出现）</span>
    </div>

    <table class="attempt-table">
      <thead>
        <tr>
          <th>同一次尝试编号</th>
          <th>任务</th>
          <th>状态 / 回执</th>
          <th>值班登记回报</th>
        </tr>
      </thead>
      <tbody>
        <tr v-if="!attempts.length && !queue.length">
          <td colspan="4" class="hint" data-testid="scheduler-todos-empty">队列为空，暂无已领取尝试。</td>
        </tr>
        <tr v-for="item in queue" :key="`q-${item.mission_id}-${item.enqueued_at ?? item.key}`" data-testid="scheduler-queue-row">
          <td>
            <span v-if="item.attempt_id" :data-testid="`scheduler-attempt-id-${item.attempt_id}`">#{{ item.attempt_id }}</span>
            <span v-else>等待派发</span>
          </td>
          <td>{{ item.mission_id }}</td>
          <td>队列中，优先级 {{ item.priority }}</td>
          <td><button class="link" type="button" @click="dispatchNext">派发此项</button></td>
        </tr>
        <tr
          v-for="attempt in attempts"
          :key="String(attempt.attempt_id)"
          :data-testid="`scheduler-attempt-row-${attempt.attempt_id}`"
          :data-attempt-id="String(attempt.attempt_id)"
          :data-attempt-no="String(attempt.attempt_no)"
          :data-attempt-number="String(attempt.attempt_no)"
          :data-mission-id="String(attempt.mission_id)"
          :data-current="String(!!attempt.is_current)"
          :data-highlighted="String(String(attempt.attempt_id) === highlightAttemptId)"
          :class="{ highlighted: String(attempt.attempt_id) === highlightAttemptId }"
        >
          <td>
            <div :data-testid="`attempt-row-${attempt.attempt_id}`">
              <strong :data-testid="`scheduler-attempt-number-${attempt.attempt_id}`">
                #{{ attempt.attempt_id }}
              </strong>
              <span class="hint">第 {{ attempt.attempt_no }} 次</span>
              <em v-if="attempt.is_current" class="current-tag">最新</em>
            </div>
          </td>
          <td>{{ attempt.mission_id }}</td>
          <td>
            <span class="badge" :class="badgeClass(attempt)" :data-testid="`scheduler-badge-${attempt.attempt_id}`">
              <span v-if="attempt.success === true" data-testid="scheduler-success-badge">成功</span>
              <span v-else-if="attempt.success === false" data-testid="scheduler-failure-badge">失败</span>
              <span v-else>待回报</span>
            </span>
            <div v-if="attempt.receipt" class="receipt-box" :data-testid="`scheduler-receipt-${attempt.attempt_id}`">
              <span>{{ attempt.receipt.receipt_id }} · {{ attempt.receipt.conclusion }}</span>
              <small v-if="attempt.duplicate_count">重复回报 {{ attempt.duplicate_count }} 次，仅第一次生效</small>
              <small v-if="attempt.redispatch_attempt_id">已重派为 #{{ attempt.redispatch_attempt_id }}</small>
            </div>
          </td>
          <td class="report-actions">
            <template v-if="!attempt.receipt">
              <select v-model="decisions[attempt.attempt_id]" :data-testid="`report-result-select-${attempt.attempt_id}`">
                <option value="success">成功</option>
                <option value="failure">失败</option>
              </select>
              <button
                class="btn primary"
                type="button"
                :data-testid="`submit-report-${attempt.attempt_id}`"
                @click="submitSelected(attempt)"
              >
                登记回报
              </button>
              <button
                class="link success"
                type="button"
                :data-testid="`report-success-${attempt.attempt_id}`"
                @click="report(attempt, true)"
              >
                登记成功
              </button>
              <button
                class="link danger"
                type="button"
                :data-testid="`report-failure-${attempt.attempt_id}`"
                @click="report(attempt, false)"
              >
                <span :data-testid="`report-fail-${attempt.attempt_id}`" class="testid-alias">登记失败</span>
              </button>
            </template>
            <span v-else class="hint" :data-testid="`report-conclusion-${attempt.attempt_id}`">
              已受理：{{ attempt.receipt.conclusion }}
            </span>
          </td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { attemptService, type AnyRecord } from '@/api/backend-service'

const props = defineProps<{ planId: number | string; highlightAttemptId?: string }>()
const emit = defineEmits<{ changed: [] }>()

const attempts = ref<AnyRecord[]>([])
const queue = ref<AnyRecord[]>([])
const redispatchable = ref<AnyRecord[]>([])
const loading = ref(false)
const error = ref('')
const missionId = ref('')
const priority = ref('1')
const decisions = reactive<Record<string, string>>({})

function badgeClass(attempt: AnyRecord): string {
  if (attempt.success === true) return 'success'
  if (attempt.success === false) return 'danger'
  return 'pending'
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await attemptService.schedulerTodos(props.planId)
    attempts.value = [...(data.attempts ?? [])].sort(
      (a, b) => Number(b.attempt_no ?? 0) - Number(a.attempt_no ?? 0),
    )
    queue.value = data.queue ?? []
    redispatchable.value = data.redispatchable ?? data.redispatchable_items ?? []
    for (const attempt of attempts.value) {
      if (decisions[String(attempt.attempt_id)] === undefined) {
        decisions[String(attempt.attempt_id)] = 'success'
      }
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : '调度待办读取失败'
  } finally {
    loading.value = false
  }
}

async function enqueue() {
  if (!missionId.value) {
    error.value = '请先填写任务ID'
    return
  }
  error.value = ''
  try {
    await attemptService.enqueueMission(props.planId, missionId.value, Number(priority.value || 1))
    missionId.value = ''
    await load()
    emit('changed')
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加入队列失败'
  }
}

async function dispatchNext() {
  error.value = ''
  try {
    await attemptService.dispatchNext(props.planId)
    await load()
    emit('changed')
  } catch (e) {
    error.value = e instanceof Error ? e.message : '派发失败'
  }
}

async function report(attempt: AnyRecord, success: boolean) {
  error.value = ''
  try {
    await attemptService.reportSchedulerAttempt(props.planId, attempt.attempt_id, {
      mission_id: attempt.mission_id,
      success,
    })
    await load()
    emit('changed')
  } catch (e) {
    error.value = e instanceof Error ? e.message : '回报登记失败'
  }
}

async function submitSelected(attempt: AnyRecord) {
  await report(attempt, decisions[String(attempt.attempt_id)] !== 'failure')
}

async function redispatchNext() {
  error.value = ''
  try {
    await attemptService.redispatchNext(props.planId)
    await load()
    emit('changed')
  } catch (e) {
    error.value = e instanceof Error ? e.message : '查询可重派项失败'
    await load()
  }
}

defineExpose({ load })
onMounted(load)
</script>
