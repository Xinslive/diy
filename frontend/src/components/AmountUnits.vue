<template>
  <!-- 亿千百十万千百十元角分 —— 挂在借/贷金额栏下面，两边格子数一样，
       竖线跟下面每一行的金额格自然对齐。 -->
  <div class="amt-units">
    <span v-for="(u, i) in AMOUNT_UNITS" :key="i" class="amt-unit" :class="AMOUNT_SEPS[i]">{{ u }}</span>
  </div>
</template>

<script setup>
import { AMOUNT_SEPS, AMOUNT_UNITS } from '@/utils/amountGrid'
</script>

<style scoped>
.amt-units { display: flex; width: 100%; height: 100%; }

.amt-unit {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  color: #606266;
}

.plain { border-left: 1px solid #ededed; }
.blue { border-left: 1px solid #bde3f7; }
.red { border-left: 1px solid #f2cbca; }
</style>
