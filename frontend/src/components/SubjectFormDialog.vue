<template>
  <el-dialog v-model="visible" title="新增科目" width="640px" :close-on-click-modal="false">
    <el-form :model="form" label-width="110px" label-position="right" class="sf">
      <el-form-item label="上级科目" required>
        <el-tree-select
          v-model="form.parent_id"
          :data="selectTree"
          node-key="id"
          check-strictly
          filterable
          default-expand-all
          clearable
          placeholder="请选择（留空则新建一级科目）"
          style="width: 100%"
        />
      </el-form-item>

      <el-form-item label="科目编码" required>
        <el-input v-model="form.code" placeholder="选好上级科目会自动接号" />
      </el-form-item>

      <el-form-item label="科目名称" required>
        <el-input v-model="form.name" placeholder="如：银行存款-中国银行" @keyup.enter="submit" />
      </el-form-item>

      <el-form-item label="科目类别" required>
        <!-- 选了上级科目，类别就跟着上级走：同一个大类里再分小类，
             否则「资产」下面挂一个「负债」科目，报表口径当场就乱了 -->
        <el-select v-model="form.category" :disabled="!!form.parent_id" style="width: 100%">
          <el-option v-for="(label, value) in CATEGORY_LABEL" :key="value" :label="label" :value="value" />
        </el-select>
      </el-form-item>

      <el-form-item label="余额方向" required>
        <el-radio-group v-model="form.direction">
          <el-radio value="DEBIT">借</el-radio>
          <el-radio value="CREDIT">贷</el-radio>
        </el-radio-group>
      </el-form-item>

      <!-- 编码规则提示贴在左下角：它是「说明」不是「一项」，压在表单右边最显眼的位置
           反而让人以为是必填项 -->
      <p class="sf-note"><em>注意：</em>科目编码规则：{{ rule }}。</p>
    </el-form>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="submit">确定</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import * as api from '@/api/subject'
import { useSessionStore } from '@/stores/session'
import { CATEGORY_DEFAULT_DIRECTION, CATEGORY_LABEL, nextCode, segmentOf } from '@/utils/subjectCode'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  // 整棵科目树（前端要靠它算「父编码 + 下一个序号」）
  tree: { type: Array, default: () => [] },
  // 打开时就选好的上级科目：科目余额表的「下级」走这个
  parentId: { type: [Number, String], default: null },
  // 凭证里填了个查不到的编码再点「+ 点击添加」，人敲的那串字。
  // 整份表单照它铺：编码留他敲的，上级按编码的最长前缀判，类别方向跟着上级走
  preset: { type: String, default: '' }
})
const emit = defineEmits(['update:modelValue', 'saved'])

const store = useSessionStore()
const saving = ref(false)
const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v)
})

const form = reactive({
  parent_id: null, code: '', name: '',
  category: 'ASSET', direction: 'DEBIT'
})

const rule = computed(() => store.currentBook?.subject_code_rule || '4-2-2-2')

/** 下拉里显示「编码 名称」：科目一多，光看名字根本分不清是哪一张 */
const selectTree = computed(() => props.tree.map(n => ({
  id: n.id,
  label: `${n.code} ${n.name}`,
  children: (n.children || []).map(c => ({
    id: c.id,
    label: `${c.code} ${c.name}`,
    children: (c.children || []).map(g => ({ id: g.id, label: `${g.code} ${g.name}` }))
  }))
})))

/** 摊平整棵树：算序号要看所有兄弟编码，不只是叶子 */
const allSubjects = computed(() => {
  const out = []
  const walk = (nodes) => nodes.forEach((n) => {
    out.push(n)
    if (n.children?.length) walk(n.children)
  })
  walk(props.tree)
  return out
})

const parentOf = (id) => allSubjects.value.find((s) => s.id === id) || null

/**
 * 编码能判出挂在哪一级：找**最长的那一段已有编码**，它既是这个号的前缀、又比它短。
 *   敲 100204 → 1002 银行存款
 *   敲 10021  → 1002 银行存款（尾巴只有两位，照样挂得上去）
 * 判不出来（'b00104' 这种压根不是数字编码）就不硬套 —— 宁可留一级让人自己选，
 *  猜错了挂到不相干的科目底下，比没有上级还难查。
 */
function parentForCode(code) {
  const c = String(code || '').trim()
  if (!c || !/^\d+$/.test(c)) return null
  let best = null
  for (const s of allSubjects.value) {
    const k = String(s.code)
    if (k.length >= c.length || !c.startsWith(k)) continue
    if (!best || k.length > String(best.code).length) best = s
  }
  return best
}

/**
 * 连上级都判不出来时，退一步按编码**开头几位**看它跟已有科目是不是一挂的，
 *  好定下大类：敲 1124 要知道它跟 1122/1123 一样是负债，而不是一律默认成资产。
 *
 *  从编码规则的头一截开始（'4-2-2-2' → 4 位），一段段往回收 ——
 *  1124 底下没有科目，可 112、11 这一挂全是应付账款，一样能定大类。
 *  收回来的前缀必须**是已有编码的开头**，否则就是凭空认亲了。
 */
function kindForCode(code) {
  const c = String(code || '').trim()
  if (!c || !/^\d+$/.test(c)) return null
  for (let n = Math.min(segmentOf(rule.value, 1), c.length); n > 0; n -= 1) {
    const head = c.slice(0, n)
    const same = allSubjects.value.find((s) => /^\d+$/.test(String(s.code))
      && String(s.code).startsWith(head))
    if (same) return same
  }
  return null
}

const dirOf = (cat) => CATEGORY_DEFAULT_DIRECTION[cat] || 'DEBIT'

/**
 * 上一次自动接出来的编码。
 *
 *  留着它，是为了分清「这个号是系统接的」还是「这个号是人自己敲的」——
 *  换上级科目时只重算前者。人特意敲出来的号被自动接号悄悄顶掉，
 *  下一行凭证挂到别的科目上去了，人还以为自己填的是刚才那个。
 */
const autoCode = ref('')
function setAutoCode() {
  form.code = nextCode(rule.value, parentOf(form.parent_id), allSubjects.value)
  autoCode.value = form.code
}

// 打开时重置，并把上级科目带的属性一次性铺好
watch(() => props.modelValue, (open) => {
  if (!open) return
  const typed = String(props.preset || '').trim()
  // 人敲的是编码还是名字：带中文的一律当名字（「工商银行」），
  // 其余当编码（「100204」「b00104」）。落到哪个框，别让人自己再判断一遍。
  const asName = typed && /[\u4e00-\u9fa5]/.test(typed)
  // 上级先按「人敲的编码」判（100204 → 1002），判不出来才用打开时带着的那个
  const guess = typed && !asName ? parentForCode(typed) : null
  const parent = parentOf(guess ? guess.id : props.parentId)
  form.parent_id = parent ? parent.id : null
  form.name = asName ? typed : ''
  if (parent) {
    form.category = parent.category
    form.direction = parent.direction || dirOf(parent.category)
  } else {
    // 没有上级可挂：先看编码头一段属于哪个大类（「根据填写的信息自动判定」，
    // 判不出来才落到默认的资产/借
    const kind = typed && !asName ? kindForCode(typed) : null
    form.category = kind ? kind.category : 'ASSET'
    form.direction = kind ? (kind.direction || dirOf(kind.category)) : dirOf(form.category)
  }
  autoCode.value = ''
  form.code = typed && !asName ? typed : ''
  if (!form.code) setAutoCode()
})

// 换上级科目 → 类别、余额方向跟着重算；编码只在「本来就是自动接的」时重算
watch(() => form.parent_id, (id) => {
  const parent = parentOf(id)
  form.category = parent ? parent.category : form.category
  form.direction = parent ? (parent.direction || dirOf(parent.category)) : dirOf(form.category)
  if (!form.code.trim() || form.code.trim() === autoCode.value) setAutoCode()
})

async function submit() {
  // 新增科目是个**写**动作。凭证页的科目格在只读时是关着的，但这个弹窗还有
  // 别的入口（科目余额表的「下级」），VIEWER 照样能走到这里 ——
  // 挡在发请求之前，别让人白等一趟 403。
  // 认的是 store 那份写权限白名单，与后端的 ROLES 是同一份名单。
  if (!store.canWrite) return ElMessage.warning('这个账套是只读的，不能新增科目')
  if (!form.code.trim()) return ElMessage.warning('科目编码不能为空')
  if (!form.name.trim()) return ElMessage.warning('科目名称不能为空')
  saving.value = true
  try {
    const created = await api.createSubject(store.bookId, {
      parent_id: form.parent_id || null,
      code: form.code.trim(), name: form.name.trim(),
      category: form.category, direction: form.direction,
      // is_leaf 由服务端按「有没有下级」算（ARCHITECTURE §9.6），客户端提交的值不读。
      // 这里仍然带上，只是让老接口也能正常收下 —— 别把它当一道防线。
      is_leaf: true, carry_forward: false, sort_no: 0
    })
    ElMessage.success(`已新增科目 ${created.code} ${created.name}`)
    visible.value = false
    emit('saved', created)
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.sf :deep(.el-form-item) { margin-bottom: 18px; }
/* 贴着弹窗左下角，不跟着 label 缩进 —— 原来往右顶了 110px，看着像表单的一部分 */
.sf-note { margin: 4px 0 0; font-size: 13px; color: #606266; }
.sf-note em { color: #f56c6c; font-style: normal; }
</style>
