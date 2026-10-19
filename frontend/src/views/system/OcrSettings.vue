<template>
  <div class="page">
    <div class="page-head">
      <h2 class="page-title">外部服务</h2>
      <span class="muted">扫描件识别等第三方服务的调用凭据，改完即生效，不必重启</span>
    </div>

    <div class="card">
      <el-form :model="form" label-width="110px" class="ocr-form">
        <el-form-item label="发票识别">
          <el-tag :type="form.token_set ? 'success' : 'info'" size="small">
            {{ form.token_set ? `已配置 ${form.token_hint}` : '未配置，仅支持带文本层的 PDF' }}
          </el-tag>
          <span class="muted form-hint">
            电子发票多为机器生成，自带文本层，不消耗这个服务；只有扫描件才需要。
          </span>
        </el-form-item>

        <el-form-item label="接口地址">
          <el-input v-model="form.api_url" placeholder="https://…/api/v2/ocr/jobs" />
        </el-form-item>

        <el-form-item label="模型">
          <el-input v-model="form.model_name" placeholder="PaddleOCR-VL-1.6" />
        </el-form-item>

        <el-form-item label="访问令牌">
          <el-input
            v-model="tokenInput" type="password" show-password autocomplete="off"
            :placeholder="form.token_set ? '留空表示不修改' : '粘贴 API Token'"
          />
          <span class="muted form-hint">
            出于安全考虑，已配置的令牌不再回显，也找不回来 —— 只能覆盖重填。
          </span>
        </el-form-item>

        <el-form-item label="超时 / 轮询">
          <el-input-number v-model="form.timeout_seconds" :min="10" :max="3600" :step="30" controls-position="right" style="width: 150px" />
          <span class="unit">秒</span>
          <el-input-number v-model="form.poll_seconds" :min="0.5" :max="60" :step="0.5" controls-position="right" style="width: 150px; margin-left: 16px" />
          <span class="unit">秒 / 次</span>
        </el-form-item>

        <el-form-item label="可选参数">
          <el-input v-model="form.optional_payload" placeholder='{"useDocUnwarping":false}' />
          <span class="muted form-hint">原样透传给服务端的 JSON，填错会被对方拒绝，不建议改。</span>
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="saving" @click="save">保存</el-button>
          <el-button v-if="form.token_set" @click="clearToken">清除令牌</el-button>
        </el-form-item>
      </el-form>
    </div>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as api from '@/api/system'

const saving = ref(false)
// 令牌只往这一个变量里流，保存完立刻清空，不留在响应式状态里等着被人翻。
const tokenInput = ref('')

// model 是前端响应式里的保留语义（ref/reactive 之类），表单里换个名存，
// 提交时再换回去 —— 省得为了一个字段去开后端接口的字段名。
const form = reactive({
  api_url: '', model_name: '', optional_payload: '',
  timeout_seconds: 600, poll_seconds: 5, token_set: false, token_hint: '',
})

async function load() {
  Object.assign(form, await api.getOcrConfig())
}

async function save() {
  saving.value = true
  try {
    // token 留空就是「不改」。后端不接受「空串 = 清空」，所以这里
    // 空值干脆不往上传，天然不会误清。
    const payload = {
      api_url: form.api_url,
      model: form.model_name,
      timeout_seconds: form.timeout_seconds,
      poll_seconds: form.poll_seconds,
      optional_payload: form.optional_payload,
    }
    if (tokenInput.value) payload.api_token = tokenInput.value
    Object.assign(form, await api.saveOcrConfig(payload))
    tokenInput.value = ''
    ElMessage.success('外部服务配置已保存')
  } finally { saving.value = false }
}

async function clearToken() {
  // 清令牌会让扫描件这条路直接失效（退回只支持文本层 PDF），所以明确问一次，
  // 而不是在保存时顺带做掉。
  await ElMessageBox.confirm(
    '清除后扫描件将无法识别，只能处理自带文本层的电子发票。确定清除吗？',
    '清除访问令牌', { type: 'warning', confirmButtonText: '清除', cancelButtonText: '取消' },
  )
  saving.value = true
  try {
    Object.assign(form, await api.saveOcrConfig({ clear_token: true }))
    tokenInput.value = ''
    ElMessage.success('访问令牌已清除')
  } finally { saving.value = false }
}

onMounted(load)
</script>

<style scoped>
.ocr-form { max-width: 720px; }
.form-hint { margin-left: 12px; font-size: 12px; }
.unit { margin-left: 6px; font-size: 13px; color: #64748b; }
</style>
