<template>
  <section
    class="attempt-panel"
    data-testid="attempt-history-panel"
    :data-mission-id="missionId"
    :data-current-attempt-id="current?.attempt_id ?? ''"
  >
    <div class="attempt-toolbar">
      <strong data-testid="attempt-history">尝试历史 · 尝试身份与回执</strong>
      <label class="inline-field">
        <span>调度计划ID</span>
        <input
          v-model="planId"
          data-testid="attempt-plan-id-input"
          placeholder="可选"
          inputmode="numeric"
        />
      </label>
      <span :data-testid="`claim-attempt-${missionId}`" class="testid-alias">
      <button class="btn primary" type="button" data-testid="claim-attempt-button" @click="claim">
        领取尝试编号
      </button>
      </span>
      <button class="btn ghost" type="button" data-testid="refresh-attempt-history" @click="load">
        刷新
      </button>
      <RouterLink
        class="link"
        :to="schedulerLink()"
        data-testid="goto-scheduler-link"
      >
        前往调度计划登记回报
      </RouterLink>
    </div>

    <p v-if="loading" class="hint" data-testid="attempt-history-loading">尝试历史读取中…</p>
    <p v-else-if="error" class="error-text" data-testid="attempt-history-error">{{ error }}</p>
    <p v-else-if="!attempts.length" class="hint" data-testid="attempt-history-empty">
      尚无领取记录，点击“领取尝试编号”生成可回报身份。
    </p>

    <table v-if="attempts.length" class="attempt-table">
      <thead>
        <tr>
          <th>后端尝试编号 / 次序</th>
          <th>状态徽标</th>
          <th>回执落点</th>
          <th>重派</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="attempt in attempts"
          :key="String(attempt.attempt_id)"
          :data-testid="`attempt-history-row-${attempt.attempt_id}`"
          :data-attempt-id="String(attempt.attempt_id)"
          :data-attempt-no="String(attempt.attempt_no)"
          :data-attempt-number="String(attempt.attempt_no)"
          :data-current="String(!!attempt.is_current)"
          :data-status="String(attempt.status)"
          class="attempt-row"
          :class="{ current: attempt.is_current }"
        >
          <td>
            <div :data-testid="`attempt-row-${attempt.attempt_id}`">
              <span :data-testid="`attempt-number-${attempt.attempt_id}`" class="testid-alias">
                <strong :data-testid="`attempt-id-${attempt.attempt_id}`">#{{ attempt.attempt_id }}</strong>
              </span>
              <span class="hint">第 {{ attempt.attempt_no }} 次领取</span>
              <code v-if="attempt.attempt_code">{{ attempt.attempt_code }}</code>
              <RouterLink
                v-if="planIdOfAttempt(attempt)"
                class="link scheduler-jump"
                :to="schedulerLink(attempt)"
                :data-testid="`goto-scheduler-attempt-${attempt.attempt_id}`"
              >
                到调度计划登记此尝试
              </RouterLink>
              <em v-if="attempt.is_current" class="current-tag" data-testid="current-attempt-tag">当前尝试</em>
            </div>
          </td>
          <td>
            <span
              class="badge"
              :class="badgeClass(attempt)"
              :data-testid="`attempt-status-badge-${attempt.attempt_id}`"
              :data-result="String(attempt.success)"
            >
              <span v-if="attempt.success === true" data-testid="success-badge">成功</span>
              <span v-else-if="attempt.success === false" data-testid="failure-badge">失败</span>
              <span v-else data-testid="pending-badge">待回报</span>
            </span>
          </td>
          <td>
            <div
              v-if="attempt.receipt"
              class="receipt-box"
              :data-testid="`attempt-receipt-${attempt.attempt_id}`"
            >
              <strong>回执 {{ attempt.receipt.receipt_id }}</strong>
              <span>结论：{{ attempt.receipt.conclusion }}</span>
              <small>{{ attempt.receipt.reported_iso }}</small>
              <small v-if="attempt.duplicate_count">重复送达 {{ attempt.duplicate_count }} 次，仅登记一次</small>
            </div>
            <span v-else class="hint">尚未登记回报</span>
          </td>
          <td :data-testid="attempt.can_redispatch ? `redispatch-button-${missionId}` : undefined">
            <span
              v-if="attempt.can_redispatch"
              class="testid-alias"
              :data-testid="`redispatch-${missionId}`"
              :data-attempt-redispatch="`redispatch-${attempt.attempt_id}`"
            >
            <button
              class="link danger"
              type="button"
              data-testid="redispatch-button"
              data-redispatch="true"
              aria-label="可重派"
              @click="redispatch(attempt)"
            >
              可重派 / 立即重派
            </button>
            </span>
            <button
              v-else-if="attempt.is_current && attempt.success === true"
              class="link"
              type="button"
              disabled
              data-testid="redispatch-succeeded-disabled"
            >
              已成功，不可重派
            </button>
            <span v-else-if="attempt.redispatch_attempt_id" class="hint">
              已重派为 #{{ attempt.redispatch_attempt_id }}
            </span>
            <span v-else class="hint">—</span>
          </td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { attemptService, type AnyRecord } from '@/api/backend-service'

const props = defineProps<{ missionId: number | string; planId?: number | string }>()
const emit = defineEmits<{ changed: [] }>()

const attempts = ref<AnyRecord[]>([])
const loading = ref(false)
const error = ref('')
const planId = ref(String(props.planId ?? ''))
const current = computed(() => attempts.value.find((item) => item.is_current) ?? null)

function planIdOfAttempt(attempt?: AnyRecord): string | number | undefined {
  const fromAttempt = attempt?.plan_id
  if (typeof fromAttempt === 'number' || typeof fromAttempt === 'string') return fromAttempt
  return planId.value || undefined
}

function schedulerLink(attempt?: AnyRecord) {
  const plan = planIdOfAttempt(attempt)
  return {
    path: '/scheduler',
    query: {
      ...(plan ? { plan: String(plan) } : {}),
      mission: String(props.missionId),
      ...(attempt?.attempt_id ? { attempt: String(attempt.attempt_id) } : {}),
    },
  }
}

function badgeClass(attempt: AnyRecord): string {
  if (attempt.success === true) return 'success'
  if (attempt.success === false) return 'danger'
  return 'pending'
}

function sortAttempts(items: AnyRecord[]): AnyRecord[] {
  return [...items].sort((a, b) => Number(b.attempt_no ?? 0) - Number(a.attempt_no ?? 0))
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await attemptService.missionHistory(props.missionId)
    attempts.value = sortAttempts(data.items ?? data.attempts ?? [])
  } catch (e) {
    error.value = e instanceof Error ? e.message : '尝试历史读取失败'
  } finally {
    loading.value = false
  }
}

async function claim() {
  error.value = ''
  try {
    await attemptService.claimMissionAttempt(props.missionId, planId.value || undefined)
    await load()
    emit('changed')
  } catch (e) {
    error.value = e instanceof Error ? e.message : '领取尝试编号失败'
  }
}

async function redispatch(attempt: AnyRecord) {
  error.value = ''
  try {
    await attemptService.redispatchMission(props.missionId, attempt.attempt_id, planId.value || undefined)
    await load()
    emit('changed')
  } catch (e) {
    error.value = e instanceof Error ? e.message : '重派失败'
  }
}

defineExpose({ load })
onMounted(load)
</script>
