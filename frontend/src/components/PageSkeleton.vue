<template>
  <div class="cwq-page-skeleton" aria-hidden="true">
    <div class="cwq-skeleton-head">
      <span class="cwq-skeleton-line cwq-skeleton-title" />
      <span class="cwq-skeleton-line cwq-skeleton-action" />
    </div>
    <div class="cwq-skeleton-toolbar">
      <span v-for="n in 4" :key="n" class="cwq-skeleton-block" />
    </div>
    <div class="cwq-skeleton-table">
      <span v-for="n in 7" :key="n" class="cwq-skeleton-row">
        <i v-for="m in 5" :key="m" class="cwq-skeleton-cell" />
      </span>
    </div>
  </div>
</template>
