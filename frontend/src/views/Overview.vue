<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>运营概览</h2>
        <p class="page-desc">汇总各业务模块的关键指标，先看总量再看异常。</p>
      </div>
    </header>
    <div class="stat-row">
      <article v-for="card in cards" :key="card.label" class="stat-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>
    <table class="data-table">
      <thead>
        <tr><th>业务模块</th><th>今日新增</th><th>待处理</th><th>异常量</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in moduleRows" :key="row.name">
          <td>{{ row.name }}</td>
          <td>{{ row.created }}</td>
          <td>{{ row.pending }}</td>
          <td>{{ row.abnormal }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson } from '@/api/client'

type Overview = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

const cards = ref<Overview['cards']>([])
const moduleRows = ref<Overview['modules']>([])

onMounted(async () => {
  try {
    const payload = await fetchJson<Overview>('/api/overview')
    cards.value = payload.cards
    moduleRows.value = payload.modules
  } catch {
    cards.value = [{"label": "业务模块", "value": 0}, {"label": "今日新增", "value": 0}]
    moduleRows.value = [{"name": "采集任务", "created": 0, "pending": 0, "abnormal": 0}, {"name": "代理节点", "created": 0, "pending": 0, "abnormal": 0}, {"name": "频率控制", "created": 0, "pending": 0, "abnormal": 0}, {"name": "设备指纹", "created": 0, "pending": 0, "abnormal": 0}, {"name": "验证码处理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "Cookie管理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "页面渲染", "created": 0, "pending": 0, "abnormal": 0}, {"name": "抽取规则", "created": 0, "pending": 0, "abnormal": 0}, {"name": "种子URL", "created": 0, "pending": 0, "abnormal": 0}, {"name": "内容去重", "created": 0, "pending": 0, "abnormal": 0}, {"name": "结果解析", "created": 0, "pending": 0, "abnormal": 0}, {"name": "Schema匹配", "created": 0, "pending": 0, "abnormal": 0}, {"name": "调度计划", "created": 0, "pending": 0, "abnormal": 0}, {"name": "数据导出", "created": 0, "pending": 0, "abnormal": 0}, {"name": "质量抽检", "created": 0, "pending": 0, "abnormal": 0}, {"name": "机器人校验", "created": 0, "pending": 0, "abnormal": 0}, {"name": "投递管道", "created": 0, "pending": 0, "abnormal": 0}, {"name": "弹性伸缩", "created": 0, "pending": 0, "abnormal": 0}]
  }
})
</script>
