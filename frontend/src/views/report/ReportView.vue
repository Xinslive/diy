<template>
  <div class="page report-page" :class="{ 'is-designing': designMode }">
    <div class="toolbar report-toolbar no-print">
      <div class="report-toolbar-copy">
        <span class="toolbar-kicker">报表中心</span>
        <strong>{{ template?.name || '报表' }}</strong>
        <span class="toolbar-context">{{ store.currentBook?.name }} · {{ data.period || '未选择期间' }}</span>
      </div>
      <div class="report-toolbar-actions">
        <el-select v-model="templateId" class="template-select" @change="onTemplateChange">
          <el-option v-for="t in allTemplates" :key="t.id" :label="t.name" :value="t.id" />
        </el-select>
        <el-button class="toolbar-button" :type="designMode ? 'primary' : 'default'" @click="toggleDesignMode">
          <el-icon><Tools /></el-icon>{{ designMode ? '退出设计' : '设计报表' }}
        </el-button>
        <el-button class="toolbar-button" :disabled="!store.canWrite" @click="renameTemplate">
          <el-icon><EditPen /></el-icon>重命名
        </el-button>
        <el-button class="toolbar-button" type="danger" plain
                   :disabled="!store.canWrite || template?.is_builtin"
                   :title="template?.is_builtin ? '内置报表不能删除' : '删除报表'"
                   @click="deleteCurrentTemplate">
          <el-icon><Delete /></el-icon>删除
        </el-button>
        <el-button v-if="designMode" type="success" class="toolbar-button save-button" :disabled="!dirtyCount" @click="saveAll">
          <el-icon><CircleCheckFilled /></el-icon>保存<template v-if="dirtyCount"> {{ dirtyCount }}</template>
        </el-button>
        <el-button class="toolbar-button" @click="previewHtml"><el-icon><Reading /></el-icon>预览打印</el-button>
        <el-button class="toolbar-button" @click="exportXlsx"><el-icon><Download /></el-icon>导出 Excel</el-button>
      </div>
    </div>

    <div v-if="designMode" class="design-strip no-print">
      <div class="design-strip-mark"><el-icon><MagicStick /></el-icon></div>
      <div class="design-strip-copy">
        <strong>可视化公式设计</strong>
        <span>点击金额单元格，在右侧用选择框配置取数、合计和计算关系</span>
      </div>
      <el-tag v-if="hiddenRowCount" type="info" effect="plain">{{ hiddenRowCount }} 行已隐藏</el-tag>
    </div>

    <div class="card">
      <div class="report-head">
        <h2 class="report-title">{{ template?.name }}</h2>
        <div class="report-sub">
          {{ store.currentBook?.name }} · 期间 {{ data.period }}
          <span v-if="hiddenRowCount" class="muted">（{{ hiddenRowCount }} 行已隐藏，仅设计模式可见）</span>
        </div>
      </div>

      <!-- 口径提示（后端 notices，唯一的数据契约见 reports/evaluate.py）：
           本期真的结转过才可能出现。「这个数和总账为什么不一样」——
           用户自己能看懂这句话，就不必来问「是不是算错了」。
           README 开头「不存在某个报表和总账对不上」靠的就是**可对账**，不是靠它们相等。 -->
      <el-alert v-for="(n, i) in data.notices || []" :key="i"
                :type="noticeType(n)" :closable="false" class="mb no-print">
        <div>{{ n.text }}</div>
        <div v-if="n.amount" class="notice-sub">
          本期结转凭证借方合计 <b class="money">{{ fmt(n.amount) }}</b>
          <template v-if="n.cols?.length">
            ；下列{{ colNameOf(n.cols) }}列按**排除结转凭证**的口径取数
          </template>
        </div>
      </el-alert>

      <el-alert v-if="data.errors?.length" type="error" :closable="false" class="mb">
        <div v-for="(e, i) in data.errors" :key="i">{{ e }}</div>
      </el-alert>

      <div class="report-workspace" :class="{ 'has-inspector': designMode && inspectorOpen }">
        <div class="report-canvas">
          <div class="canvas-toolbar no-print">
            <span class="canvas-title"><el-icon><TrendCharts /></el-icon>报表数据</span>
            <span v-if="designMode" class="canvas-hint">点击金额单元格配置取数</span>
            <span v-else class="canvas-hint">{{ visibleRows.length }} 行 · {{ data.cols.length }} 列</span>
            <!-- 缩放：报表动辄几十列几百行，设计模式还要在表头塞列名输入框和口径
                 下拉，挤在卡片里只能横着拖着看。做法与「打印模板」的舞台一致 ——
                 表格按自然尺寸排版，外层整体缩放：想看全就缩到适应宽度，
                 想读数字就放大。 -->
            <div class="zoom-tools">
              <el-button-group size="small">
                <el-button :disabled="scale <= MIN_SCALE" title="缩小" @click="zoomBy(-0.1)">
                  <el-icon><ZoomOut /></el-icon>
                </el-button>
                <el-button class="zoom-label"
                           :title="fitMode ? '适应宽度中，点一下回到 100%' : '点一下回到 100%'"
                           @click="resetZoom">{{ Math.round(scale * 100) }}%</el-button>
                <el-button :disabled="scale >= MAX_SCALE" title="放大" @click="zoomBy(0.1)">
                  <el-icon><ZoomIn /></el-icon>
                </el-button>
              </el-button-group>
              <el-tooltip content="适应宽度：整张表缩到一屏看全" placement="bottom">
                <el-button size="small" :type="fitMode ? 'primary' : ''" @click="fitToWidth">
                  <el-icon><FullScreen /></el-icon>&nbsp;适应宽度
                </el-button>
              </el-tooltip>
            </div>
          </div>
          <!-- 唯一的滚动容器。内层为什么还要再套两层，见脚本里的注释：
               transform 不改变布局盒，只缩放的话滚动范围会按未缩放的尺寸算 ——
               缩到 50% 右边空一大片，放到 200% 底部被截断。 -->
          <div ref="scrollHost" class="table-scroll" @wheel="onWheel">
            <div class="canvas-box" :style="canvasBoxStyle">
              <div ref="zoomLayer" class="canvas-zoom" :style="zoomLayerStyle">
                <table ref="tableEl" class="report-table" v-loading="loading"
                       :style="{ minWidth: `${tableMinWidth}px` }">
        <thead>
          <tr>
            <th class="c-gutter no-print" v-if="designMode">行</th>
            <!-- 列头整体由 data.cols 驱动，不再「一个项目列 + 若干数值列」——
                 会小企01表是左右双栏 8 列，两个 STATIC（资产 / 负债和所有者权益）
                 加两个 LINE_NO。表头和表体必须是同一份顺序，
                 否则两边各数各的，整列数字错位。
                 STATIC 的表头就是列自己的 label，所以这里不再写死「项目」。 -->
            <th v-for="c in data.cols" :key="c.col_no" class="c-head" :style="{ width: `${c.width}px` }"
                :title="colTitle(c)">
              <!-- 下面三样（插删列按钮 / 列字母 / 取数口径下拉）都只在设计模式出现。
                   `no-print` 只管**打印时**不输出，屏幕上照样显示 —— 于是查看模式下
                   一张干干净净的报表，表头上却浮着八个 ⊕ ✕ 和 A~H，点错了还会真的
                   去改模板。它们是设计工具，不是报表的一部分，所以要看它们
                   就得先开设计模式。 -->
              <div v-if="designMode" class="head-edit no-print">
                <el-button link size="small" :title="`在 ${c.letter} 列左侧插入`" @click="insertCol(c.col_no)">⊕</el-button>
                <el-button link size="small" :title="`删除 ${c.letter} 列`" @click="removeCol(c)">✕</el-button>
              </div>
              <!-- 必须是 v-model，不能退成 :model-value。el-input 每次 input
                   之后都会用 props.modelValue 回填输入框（Element Plus
                   handleInput 末尾的 setNativeInputValue），modelValue 不跟着
                   手走的话，刚打进去的字会被立刻还原成旧值 —— 表现就是「打不进
                   字」。而 v-model 会先把新值写回 c.label，所以 renameCol 那边
                   不能再拿入参跟 c.label 比相等。 -->
              <el-input v-if="designMode" v-model="c.label" size="small" class="col-label"
                        @change="renameCol(c, $event)" />
              <template v-else>{{ c.label }}</template>
              <!-- 口径角标：这一列走的是**报表口径**（损益取数，排除结转凭证），
                   与总账 / 科目余额表那个「含结转」的账簿口径差的就是结转凭证。
                   不标出来，用户只会觉得这个数算错了。这是给**看表的人**看的，
                   所以不受设计模式开关影响。 -->
              <span v-if="c.excludes_carry_forward" class="caliber no-print"
                    :title="COL_CALIBER_HINT">不含结转</span>
              <!-- 列字母 A~H：公式里写的就是它（「合计(C2:C10)」），学公式时要看，
                   所以设计模式下留着。 -->
              <div v-if="designMode" class="addr no-print">{{ c.letter }}</div>
              <div v-if="designMode" class="col-meta no-print">
                <el-select v-if="designMode" :model-value="c.kind" size="small" :title="colKindHint(c)"
                           @change="changeColKind(c, $event)">
                  <el-option v-for="o in colKindOptions(c)" :key="o.k" :label="o.v" :value="o.k"
                             :disabled="o.disabled" />
                </el-select>
              </div>
            </th>
            <th class="c-gutter no-print" v-if="designMode">
              <el-button link size="small" title="在末尾加一行" @click="insertRow(lastRowNo + 1)">＋行</el-button>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in visibleRows" :key="row.row_no"
              :class="rowClass(row)">
            <td v-if="designMode" class="c-gutter no-print">
              <div class="gutter">
                <span class="addr">{{ row.row_no }}</span>
                <el-button link size="small" title="在上面插入一行" @click="insertRow(row.row_no)">⊕</el-button>
                <el-button link size="small" title="删除本行" @click="removeRow(row)">✕</el-button>
                <el-button link size="small" :title="row.bold ? '取消加粗' : '加粗'"
                           :type="row.bold ? 'primary' : ''" @click="toggleRow(row, 'bold')">B</el-button>
                <el-button link size="small" :title="row.hidden ? '取消隐藏' : '隐藏（不参与打印）'"
                           :type="row.hidden ? 'warning' : ''" @click="toggleRow(row, 'hidden')">◉</el-button>
                <el-button link size="small" title="减少缩进" :disabled="row.indent === 0"
                           @click="toggleRow(row, 'indent', -1)">⇤</el-button>
                <el-button link size="small" title="增加缩进" @click="toggleRow(row, 'indent', +1)">⇥</el-button>
              </div>
            </td>
            <template v-for="it in rowCells(row)" :key="it.col.col_no">
              <!-- STATIC：项目名。左右各一个 STATIC，靠出现次序分左右：
                   第 1 个读 row.label/indent/bold，第 2 个读 row.style 里的
                   label_right/indent_right/bold_right（后端没有右栏专用的列）。 -->
              <td v-if="it.col.kind === 'STATIC'" class="c-label"
                  :class="[cellClass(row, it), { b: labelBold(row, it.side) }]"
                  :style="{ width: `${it.col.width}px`, paddingLeft: `${8 + labelIndent(row, it.side) * 16}px` }">
                <!-- 左右两栏的项目名存在两个地方：左栏是 row.label，右栏塞在
                     row.style.label_right（后端没有「右栏标题」这个独立字段）。
                     v-model 只能绑一条固定路径，而这里两栏共用同一个输入框，
                     所以拆成「input 时写回本地、change 时落库」两步 —— 少了
                     第一步，el-input 会用旧 modelValue 把新字还原掉。 -->
                <el-input v-if="designMode" :model-value="labelText(row, it.side)"
                          size="small" class="row-label"
                          @update:model-value="draftSideLabel(row, it.side, $event)"
                          @change="saveSideLabel(row, it.side, $event)" />
                <!-- 右栏标题存在 row.style 里，保存时只更新 label_right，保留行次、缩进、加粗等配置。 -->
                <template v-else>{{ labelText(row, it.side) }}</template>
              </td>
              <!-- LINE_NO：法定行次。STATIC / LINE_NO 属于后端的 NON_FORMULA_KINDS，
                   evaluate() 给它们的 value 恒为 null，内容不在 cells 里，全在 row.style 上。 -->
              <td v-else-if="it.col.kind === 'LINE_NO'" class="c-line"
                  :class="cellClass(row, it)"
                  :style="{ width: `${it.col.width}px` }">
                {{ lineNoOf(row, it.side) }}
              </td>
              <td v-else
                  class="c-num"
                  :class="[cellClass(row, it), { editable: designMode, selected: isEditingCell(row, it.cell), error: it.cell.error, dirty: isDirty(row.row_no, it.cell.col_no) }]"
                  :style="{ width: `${it.col.width}px` }"
                  :title="designMode ? (it.cell.formula || '点击配置公式') : (it.cell.formula || '')"
                  @click="designMode && onCellClick(row, it.cell, it)">
                <span v-if="it.cell.error" class="tag-bad">!</span>
                <!-- 单元格地址（C1 / D2…）：点格子改公式时要知道自己在哪一格。
                   同样只在设计模式出现 —— 一屏几十个 C1 C2 压在数字上方，
                   看表的人根本不需要，还会把金额读错。 -->
                <span v-if="designMode && it.cell.addr" class="addr no-print">{{ it.cell.addr }}</span>
                {{ it.cell.value !== null && it.cell.value !== undefined ? fmt(it.cell.value) : '' }}
              </td>
            </template>
          </tr>
        </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>

        <aside v-if="designMode && inspectorOpen" class="formula-inspector no-print">
          <div v-if="editing" class="inspector-content">
            <div class="inspector-head">
              <div>
                <span class="inspector-kicker">公式编辑器</span>
                <h3><span class="cell-badge">{{ editing.addr }}</span>{{ editing.rowLabel || '当前行' }}</h3>
                <p>{{ editing.colLabel }} · 当前值 {{ editing.value !== null && editing.value !== undefined ? fmt(editing.value) : '（空）' }}</p>
              </div>
              <el-tag v-if="isDirty(editing.rowNo, editing.colNo)" type="warning" effect="plain">待保存</el-tag>
            </div>

            <div class="builder-modes">
              <button v-for="mode in BUILDER_TYPES" :key="mode.key" type="button" class="builder-mode"
                      :class="{ active: builderType === mode.key }" @click="builderType = mode.key">
                <el-icon><component :is="mode.icon" /></el-icon>
                <span>{{ mode.label }}</span>
              </button>
            </div>

            <div class="builder-body">
              <div v-if="builderType === 'subject'" class="builder-form">
                <div class="builder-intro">读取一个科目的金额；选上级科目会把它下面所有末级一起加总</div>
                <label class="builder-label">科目</label>
                <el-select v-model="builder.subjectCode" filterable clearable class="builder-input" placeholder="搜索科目编码或名称">
                  <el-option v-for="s in subjectOptions" :key="s.code" :label="subjectLabel(s)" :value="s.code" />
                </el-select>
                <label class="builder-label">取数项目</label>
                <el-select v-model="builder.measure" class="builder-input">
                  <el-option v-for="m in MEASURE_OPTIONS" :key="m.value" :label="m.label" :value="m.value" />
                </el-select>
              </div>

              <div v-else-if="builderType === 'sum'" class="builder-form">
                <div class="builder-intro">把一段金额自动相加</div>
                <label class="builder-label">开始单元格</label>
                <el-select v-model="builder.rangeStart" filterable class="builder-input" placeholder="如 C2">
                  <el-option v-for="a in addressOptions" :key="`start-${a.addr}`" :label="addressLabel(a)" :value="a.addr" />
                </el-select>
                <label class="builder-label">结束单元格</label>
                <el-select v-model="builder.rangeEnd" filterable class="builder-input" placeholder="如 C15">
                  <el-option v-for="a in addressOptions" :key="`end-${a.addr}`" :label="addressLabel(a)" :value="a.addr" />
                </el-select>
              </div>

              <div v-else-if="builderType === 'reference'" class="builder-form">
                <div class="builder-intro">直接引用表内某个金额</div>
                <label class="builder-label">引用单元格</label>
                <el-select v-model="builder.reference" filterable class="builder-input" placeholder="搜索单元格地址或项目名">
                  <el-option v-for="a in addressOptions" :key="`ref-${a.addr}`" :label="addressLabel(a)" :value="a.addr" />
                </el-select>
              </div>

              <div v-else-if="builderType === 'math'" class="builder-form">
                <div class="builder-intro">对两个单元格做简单计算</div>
                <div class="math-row">
                  <el-select v-model="builder.leftRef" filterable placeholder="左边">
                    <el-option v-for="a in addressOptions" :key="`left-${a.addr}`" :label="addressLabel(a)" :value="a.addr" />
                  </el-select>
                  <el-select v-model="builder.operator" class="operator-select">
                    <el-option v-for="op in OPERATOR_OPTIONS" :key="op.value" :label="op.label" :value="op.value" />
                  </el-select>
                  <el-select v-model="builder.rightRef" filterable placeholder="右边">
                    <el-option v-for="a in addressOptions" :key="`right-${a.addr}`" :label="addressLabel(a)" :value="a.addr" />
                  </el-select>
                </div>
              </div>

              <div v-else-if="builderType === 'condition'" class="builder-form">
                <div class="builder-intro">根据金额是否满足条件，返回不同结果</div>
                <label class="builder-label">判断单元格</label>
                <el-select v-model="builder.conditionRef" filterable class="builder-input" placeholder="如 C20">
                  <el-option v-for="a in addressOptions" :key="`condition-${a.addr}`" :label="addressLabel(a)" :value="a.addr" />
                </el-select>
                <div class="condition-row">
                  <el-select v-model="builder.conditionOp" class="condition-op">
                    <el-option v-for="op in CONDITION_OPTIONS" :key="op.value" :label="op.label" :value="op.value" />
                  </el-select>
                  <el-input v-model="builder.conditionValue" placeholder="比较值" />
                </div>
                <div class="condition-row"><span class="condition-label">满足时</span><el-input v-model="builder.whenTrue" placeholder="单元格地址或数字" /></div>
                <div class="condition-row"><span class="condition-label">不满足时</span><el-input v-model="builder.whenFalse" placeholder="单元格地址或数字" /></div>
              </div>

              <div v-else class="builder-form">
                <div class="builder-intro">保留完整公式能力，适合复杂规则</div>
                <el-input v-model="builder.advancedFormula" type="textarea" :rows="5" placeholder="例如：四舍五入(C20,2)" />
                <div class="fn-list compact">
                  <div class="fn-title">常用写法</div>
                  <div v-for="f in FN_HELP" :key="f.sig" class="fn"><code>{{ f.sig }}</code><span>{{ f.desc }}</span></div>
                </div>
              </div>

              <div class="formula-preview">
                <div class="preview-label">预览</div>
                <div class="preview-copy">{{ builderSummary }}</div>
                <code>{{ builtFormula || '请补全上面的选项' }}</code>
              </div>
            </div>

            <div class="inspector-actions">
              <!-- 收起面板是「关于这块面板本身」的操作，和「改这一格」不是一回事，
                   所以摆在左边；右边两个按钮继续管当前格子的改动。
                   收起之后画布就铺满整行了（见 report-workspace 的列宽），
                   想调回来点任意金额单元格即可，面板会自己出来。 -->
              <el-button text class="inspector-toggle" @click="inspectorOpen = false">
                <el-icon><ArrowRight /></el-icon>收起面板
              </el-button>
              <span class="spacer" />
              <el-button text @click="clearFormula" :disabled="!editing?.formula && !isDirty(editing?.rowNo, editing?.colNo)">清空公式</el-button>
              <el-button type="primary" :disabled="!builtFormula" @click="applyFormula">应用修改</el-button>
            </div>
          </div>
          <div v-else class="inspector-empty">
            <div class="empty-icon"><el-icon><EditPen /></el-icon></div>
            <strong>选择一个金额单元格</strong>
            <span>公式会在这里变成可视化配置</span>
            <el-button text class="inspector-toggle" @click="inspectorOpen = false">
              <el-icon><ArrowRight /></el-icon>收起面板
            </el-button>
          </div>
        </aside>
      </div>
    </div>

    <!-- 打印不直接出纸：先弹窗选模板和纸型、看预览，确认了才打 -->
    <PrintDialog
      v-model="printVisible"
      :book-id="store.bookId"
      :path="printPath"
      scope="REPORT"
      :params="printParams"
      title="打印财务报表"
    />
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ArrowRight, CircleCheckFilled, DataAnalysis, Delete, Download,
  EditPen, FullScreen, Link, MagicStick, Operation, Reading, Tools,
  TrendCharts, ZoomIn, ZoomOut,
} from '@element-plus/icons-vue'
import * as api from '@/api/report'
import * as subjectApi from '@/api/subject'
import { downloadFile } from '@/api/print'
import PrintDialog from '@/components/PrintDialog.vue'
import { useLatestLoad } from '@/composables/useLatestLoad'
import { useSessionStore } from '@/stores/session'
import { fmt } from '@/utils/format'
import { visibleTemplates } from '@/utils/reportTemplates'
import { MAX_SCALE, MIN_SCALE, clampScale, fitScale, naturalWidth } from '@/utils/canvasZoom'

const store = useSessionStore()
const route = useRoute()
const router = useRouter()

const allTemplates = ref([])
const templateId = ref(Number(route.params.templateId || 0))
const template = ref(null)
const data = ref({ rows: [], cols: [], errors: [] })
/** 只认最新一趟：期间/模板切换时，旧响应不许覆盖新数据（见 composables/useLatestLoad）。 */
const { loading, run, isStale } = useLatestLoad('report/render')
const designMode = ref(false)
/** 公式面板默认展开（收起是为了给画布让宽度），点金额单元格会自动展开回来。 */
const inspectorOpen = ref(true)
const editing = ref(null)
const dirty = reactive({})
const subjectOptions = ref([])
/** 上级科目取的是它下面所有末级的和，和末级不是一回事，标出来免得选错。 */
const subjectLabel = (s) => (s.is_leaf === false ? `${s.code} · ${s.name} · 上级` : `${s.code} · ${s.name}`)

const MEASURE_OPTIONS = [
  { value: '期末余额', label: '期末余额' },
  { value: '期初余额', label: '期初余额' },
  { value: '本期金额', label: '本期金额' },
  { value: '本年累计', label: '本年累计' },
  { value: '本期借方', label: '本期借方' },
  { value: '本期贷方', label: '本期贷方' },
  { value: '借方累计', label: '借方累计' },
  { value: '贷方累计', label: '贷方累计' },
]
const OPERATOR_OPTIONS = [
  { value: '+', label: '+' }, { value: '-', label: '−' },
  { value: '*', label: '×' }, { value: '/', label: '÷' },
]
const CONDITION_OPTIONS = [
  { value: '>', label: '大于' }, { value: '>=', label: '大于等于' },
  { value: '=', label: '等于' }, { value: '<=', label: '小于等于' },
  { value: '<', label: '小于' }, { value: '<>', label: '不等于' },
]
const BUILDER_TYPES = [
  { key: 'subject', label: '取科目', icon: DataAnalysis },
  { key: 'sum', label: '合计范围', icon: Operation },
  { key: 'reference', label: '引用单元格', icon: Link },
  { key: 'math', label: '两格计算', icon: MagicStick },
  { key: 'condition', label: '条件判断', icon: CircleCheckFilled },
  { key: 'advanced', label: '高级公式', icon: EditPen },
]
const builderType = ref('subject')
const builder = reactive({
  subjectCode: '', measure: '期末余额',
  rangeStart: '', rangeEnd: '', reference: '',
  leftRef: '', operator: '+', rightRef: '',
  conditionRef: '', conditionOp: '>', conditionValue: '0', whenTrue: '', whenFalse: '',
  advancedFormula: '',
})

/* ── 口径标注（审计 P1-15 的前端一半）─────────────────────────────
 * 账簿引擎有两个**刻意不同**的取数口径：for_ledger（含结转凭证，
 * 总账 / 科目余额表 / 打印导出用它）与 for_report（损益取数排除结转凭证，
 * 否则月度结转之后损益类本期净额恒为 0）。差额**恰好是结转凭证金额**。
 *
 * 这不是 bug，但必须让人看得见：后端已经把信号发过来了
 * （cols[].excludes_carry_forward / notices / carry_forward_period_amount），
 * 这一段只负责把它们**说出来**。没有它，README 开头那句
 * 「不存在某个报表和总账对不上」在界面上是查证的 —— 用户只会得出
 * 「这个报表算错了」的结论。 */
const COL_CALIBER_HINT = '本期金额（不含结转损益）：这一列按损益口径取数，'
  + '已排除本期结转凭证。与总账 / 科目余额表的差额 = 本期结转凭证金额，不是算错。'

const noticeType = (n) => (n.level === 'error' ? 'error' : n.level === 'warning' ? 'warning' : 'info')
/** 「本期金额 / 本年累计」这种列名，提示里好让人一眼对上号 */
const colNameOf = (colNos = []) =>
  data.value.cols.filter((c) => colNos.includes(c.col_no)).map((c) => `「${c.label}」`).join('、')
/** 列头 title：带角标的那几列，说清楚它跟账簿差在哪 */
const colTitle = (c) => (c.excludes_carry_forward ? `${c.label}｜${COL_CALIBER_HINT}` : '')

const lastRowNo = computed(() => Math.max(0, ...data.value.rows.map((r) => r.row_no)))

/** 列号 -> 字母（A=1, B=2 …）。公式里写的就是这个，界面上必须看得见。 */
function letterOf(colNo) {
  let n = colNo, s = ''
  while (n > 0) { const r = (n - 1) % 26; s = String.fromCharCode(65 + r) + s; n = Math.floor((n - 1) / 26) }
  return s
}

const COL_KINDS = {
  FORMULA: '按单元格公式',
  BALANCE_CLOSING: '期末余额',
  BALANCE_OPENING: '年初余额',
  PERIOD_AMOUNT: '本期金额',
  YTD_AMOUNT: '本年累计',
  PERIOD_DEBIT: '本期借方',
  PERIOD_CREDIT: '本期贷方'
}
/** STATIC / LINE_NO 是报表形态的一部分、不是取数口径：后端 update_col 的
 *  COL_KINDS 里没有它们，改成这两种 kind 会被 400 挡掉。
 *  但列已经是这两种时，下拉必须显示得出中文名，否则 el-select 只能显示
 *  英文原文。所以补一个**禁用**选项：看得见、选不动。 */
const COL_KINDS_FIXED = { STATIC: '项目名（固定）', LINE_NO: '行次（固定）' }
function colKindOptions(col) {
  const opts = Object.entries(COL_KINDS).map(([k, v]) => ({ k, v, disabled: false }))
  if (COL_KINDS_FIXED[col.kind]) opts.push({ k: col.kind, v: COL_KINDS_FIXED[col.kind], disabled: true })
  return opts
}

const KIND_STATIC = 'STATIC'
const KIND_LINE_NO = 'LINE_NO'

/**
 * 口径下拉旁边的说明。
 *
 *  「项目名（固定）」这个措辞会被读成「整列锁死」，而用户想改的恰恰是这一列
 *  的名字 —— 于是去找哪里能改、没找到，就以为不能改。与其让人自己试出来，
 *  不如把「哪部分能改」直接说清楚：列名在上面那个输入框、每一行的项目名在
 *  表体里，两处都能改。
 */
function colKindHint(col) {
  if (col.kind === KIND_STATIC) {
    return '项目名列是报表的「形态」而不是取数口径，所以口径改不了它。列名在上面那个输入框、每一行的项目名在表体的输入框里，都能改。'
  }
  if (col.kind === KIND_LINE_NO) {
    return '行次列是报表的「形态」，填法定行次，口径改不了。列名在上面那个输入框里能改。'
  }
  return '这一列取数用的口径。项目名与行次不在其中，它们是报表形态，填项目与法定行次。'
}

/** 表头和表体共用的列清单，顺序与 data.cols 完全一致。
 * side 表示「这是左栏还是右栏」：STATIC / LINE_NO 在双栏表里各出现两次，
 * 靠**出现次序**分左右而不是靠 col_no —— 设计模式里插删列会改 col_no，
 * 写死列号迟早把「负债和所有者权益」画到「资产」那一侧去。 */
const colLayout = computed(() => {
  let s = 0, l = 0
  return data.value.cols.map((col) => {
    if (col.kind === KIND_STATIC) return { col, side: s++ }
    if (col.kind === KIND_LINE_NO) return { col, side: l++ }
    return { col, side: 0 }
  })
})

// 设计模式要看得到隐藏行（否则没法取消隐藏），查看模式则不显示
const hiddenRowCount = computed(() => data.value.rows.filter((r) => r.hidden).length)
const visibleRows = computed(() =>
  designMode.value ? data.value.rows : data.value.rows.filter((r) => !r.hidden))

/** 一行按列顺序摊平：每个列位置都带 { col, side, cell }，
 * 数值格的 cell 额外挂上地址（B5）和脏键，供点开改公式与「已保存」角标用。 */
function rowCells(row) {
  const byNo = new Map(row.cells.map((c) => [c.col_no, c]))
  return colLayout.value.map(({ col, side }) => {
    const src = byNo.get(col.col_no)
    const numeric = col.kind !== KIND_STATIC && col.kind !== KIND_LINE_NO
    return {
      col,
      side,
      cell: src
        ? { ...src, addr: numeric ? letterOf(col.col_no) + row.row_no : '',
            dirtyKey: `${row.row_no}:${col.col_no}` }
        : { col_no: col.col_no, label: col.label, value: null, formula: '', error: '',
            addr: numeric ? letterOf(col.col_no) + row.row_no : '',
            dirtyKey: `${row.row_no}:${col.col_no}` }
    }
  })
}

// 右栏（负债和所有者权益那半）的项目名、行次、缩进、加粗都只存在于 row.style
const labelText = (row, side) => (side ? row.style?.label_right : row.label) ?? ''
const labelIndent = (row, side) => (side ? row.style?.indent_right || 0 : row.indent || 0)
const labelBold = (row, side) => (side ? !!row.style?.bold_right : !!row.bold)
const lineNoOf = (row, side) => (side ? row.style?.line_right : row.style?.line_left) ?? ''

/** 这一「半」的行性质。资产负债表的同一行两侧常常不同性质：第 16 行左边是
 * 「流动资产合计」（要加粗压灰），右边是「递延收益」（普通项目）。
 * 后端因此把 kind 拆成了 kind_left / kind_right，只在两半都是同一个性质时才
 * 沿用行级的 style.kind（利润表那类单栏模板就是这种）。 */
const sideKind = (row, side) => row.style?.[side ? 'kind_right' : 'kind_left'] || row.style?.kind || ''

/** 一整行空掉的半边不参与行性质的着色 —— 资产负债表右半到「负债和所有者权益
 * 总计」就结束了，下面几行右半全是空格子，不挡一下就会跟着左半一起压灰。 */
const sideFilled = (row, side) => (side
  ? !!(row.style?.label_right) || row.style?.line_right != null
  : !!row.label || row.style?.line_left != null)

/** 逐格着色，而不是整行一个类。段标题 / 合计的观感只覆盖它自己那一半，
 * 与打印渲染器（printing/template.py 的 td.section / td.total）同一个口径。 */
const sideClass = (row, side) => {
  if (!sideFilled(row, side)) return ''
  const kind = sideKind(row, side)
  // 官方模板只有 7 个灰格（四个段标题 + 负债合计 / 资产总计 /
  // 负债和所有者权益（或股东权益）总计），表体通篇没有粗体。
  return kind === 'SHADE' ? 'is-shade' : kind === 'CHECK' ? 'is-check' : ''
}

const rowClass = (row) => ({
  bold: !!row.bold,
  'is-hidden': !!row.hidden
})

/** 该格所属那一半的行性质类。gutter（行号/操作按钮）不属于任何一半，不着色。 */
const cellClass = (row, it) => sideClass(row, it.side)

const isDirty = (rowNo, colNo) => dirty[`${rowNo}:${colNo}`] !== undefined
const dirtyCount = computed(() => Object.keys(dirty).length)

const FN_HELP = [
  { sig: '("1001","期末余额")', desc: '取科目余额/发生额，上级科目会加总它下面所有末级；口径：期初、期末余额、借方累计、贷方累计、本期借方、本期贷方、本期金额、本年累计' },
  { sig: '合计(B2:B8)', desc: '单元格区域求和' },
  { sig: 'IF(B10>0,B10,0)', desc: '条件判断' },
  { sig: '四舍五入(B10,2)', desc: '四舍五入到指定位数' },
  { sig: 'B9+B16', desc: '单元格加减；支持 + - * / 与比较运算' },
]

/** 当前各格的取值，做成可点击的速查表，省得靠猜。
 *  只收数值格：STATIC / LINE_NO 恒为 null，混进来全是空条目。 */
const recentAddrs = computed(() => {
  const out = []
  for (const row of data.value.rows) {
    for (const it of rowCells(row)) {
      const c = it.cell
      if (c.addr && c.value !== null && c.value !== undefined && c.value !== '') {
        out.push({ addr: c.addr, value: c.value })
      }
    }
  }
  return out.slice(0, 60)
})

const addressOptions = computed(() => {
  const out = []
  for (const row of data.value.rows) {
    for (const it of rowCells(row)) {
      if (!it.cell.addr) continue
      out.push({ addr: it.cell.addr, value: it.cell.value, rowLabel: labelText(row, it.side), colLabel: it.col.label })
    }
  }
  return out
})

const addressLabel = (a) => `${a.addr} · ${a.rowLabel || '未命名'}${a.value !== null && a.value !== undefined ? ` · ${fmt(a.value)}` : ''}`
const builderValue = (value) => {
  const text = String(value ?? '').trim()
  if (!text) return ''
  return /^-?(?:\d+(?:\.\d+)?|\.\d+)$/.test(text) || /^[A-Z]+\d+$/.test(text.toUpperCase())
    ? text.toUpperCase() : text
}
const builtFormula = computed(() => {
  if (builderType.value === 'subject') {
    if (!builder.subjectCode) return ''
    return `("${builder.subjectCode}","${builder.measure}")`
  }
  if (builderType.value === 'sum') {
    if (!builder.rangeStart || !builder.rangeEnd) return ''
    return `合计(${builder.rangeStart}:${builder.rangeEnd})`
  }
  if (builderType.value === 'reference') return builder.reference || ''
  if (builderType.value === 'math') {
    if (!builder.leftRef || !builder.rightRef) return ''
    return `${builder.leftRef}${builder.operator}${builder.rightRef}`
  }
  if (builderType.value === 'condition') {
    if (!builder.conditionRef || !builder.conditionValue || !builder.whenTrue || !builder.whenFalse) return ''
    return `IF(${builder.conditionRef}${builder.conditionOp}${builderValue(builder.conditionValue)},${builderValue(builder.whenTrue)},${builderValue(builder.whenFalse)})`
  }
  return builder.advancedFormula.trim()
})
const builderSummary = computed(() => ({
  subject: '从指定科目读取金额', sum: '合计一个单元格范围', reference: '引用表内另一格金额',
  math: '对两个单元格进行计算', condition: '满足条件时返回指定结果', advanced: '使用完整公式表达式',
}[builderType.value]))

function onDesignToggle() {
  if (designMode.value) load()
}

/**
 * 进设计模式前，把「要改的那张表」变成本账套自己的一份。
 *
 * 系统内置的资产负债表/利润表是**所有账套共用的原版**（后端 `book_id` 为
 * 空），直接改会立刻扩散到别的账套 —— 那正是「报表应该跟账套绑定」要避免的。
 * 所以点「设计报表」时先调 fork：要改内置表就复制一份到本账套，改那份。
 * 已经在本账套的报表后端原样返回同一个 id，不会重复复制。
 *
 * **URL 必须跟着换**。`/reports/:id` 是同一个路由，`router-view` 不会重新
 * 挂载，只改 `templateId` 的话地址栏还指着原版；用户刷新一下就回到没改过
 * 的原版上，看上去就是「改动没保存」。换掉地址栏，刷新才是同一个东西。
 */
async function ensureOwnCopy() {
  if (!templateId.value) return
  const forked = await api.forkTemplate(templateId.value, store.bookId)
  if (forked.id === templateId.value) return
  templateId.value = forked.id
  router.replace(`/reports/${forked.id}`)
  // 列表里那条也要换成副本：基线和副本同 code 并存时，列表按「副本优先」
  // 去重，不重取的话下拉里那一项会变回基线。
  allTemplates.value = visibleTemplates(await api.templates(store.bookId), forked.id)
}

async function toggleDesignMode() {
  const entering = !designMode.value
  designMode.value = entering
  if (!entering) {
    editing.value = null
    onDesignToggle()
    return
  }
  try {
    await ensureOwnCopy()
  } catch {
    // fork 失败就不进设计模式：进去了改的每一格都会被后端拒（基线只读），
    // 用户看到的是「打得进去、存不住」。不如一开始就不开。
    designMode.value = false
    return
  }
  onDesignToggle()
}

// ------------------------------------------------------------------ 画布缩放

/* 报表能到几十列几百行，设计模式还要在表头塞列名输入框和口径下拉。塞在卡片
 * 里就只能横着拖着看，一半内容在屏幕外 —— 也就是用户说的「挤压」。
 *
 * 做法照搬「打印模板」的舞台：表格按**自然尺寸**排版，外层整体缩放。
 *
 * 关键在两层结构。transform 不改变布局盒 —— 只给表格加 transform 的话，
 * 滚动容器仍按**未缩放**的尺寸算可滚范围，缩到 50% 时右边空出一大片，
 * 放大到 200% 时底部会被截断。所以：
 *
 *   .table-scroll   唯一的滚动容器
 *     .canvas-box   按「缩放后」的尺寸占位（宽度和高度都乘 scale）
 *       .canvas-zoom  绝对定位，固定按自然宽度排版，transform 在这里
 *         table.report-table
 *
 * .canvas-zoom 的宽度必须是**未缩放的可用宽度**，表格才能维持今天的排版
 * 结果 —— 不然缩放一次表格就重排一次，字会跟着折行，量出来的高度也跟着跳。
 *
 * 而 .canvas-box 的宽高取自 table 自己的 offsetWidth/offsetHeight：表格
 * 内容比容器宽时（设计模式就是如此）offsetWidth 才是真实的内容宽度，用容器
 * 宽度占位会把溢出部分裁掉，横向滚动就没了。offsetWidth 不受 transform
 * 影响，所以缩放中读到的一直是自然尺寸。 */
const scrollHost = ref(null)
const zoomLayer = ref(null)
const tableEl = ref(null)
const scale = ref(1)
/** 适应宽度：跟着容器尺寸走，窗口或面板一变就重新铺满 */
const fitMode = ref(false)
/** 滚动容器的可用宽度（扣掉内边距），也就是表格按 100% 排版时的宽度 */
const availW = ref(0)
/** 表格未缩放时的真实尺寸，用来做缩放后的占位 */
const tableBox = reactive({ w: 0, h: 0 })

/**
 * 表格的自然宽度下限：各列模板自带的列宽之和。
 * 规则和「为什么非有不可」都写在 canvasZoom.js 的 naturalWidth 上。
 */
const tableMinWidth = computed(() => naturalWidth(data.value.cols))

const canvasBoxStyle = computed(() => {
  // 还没量到就交回给常规流：宁可一帧没缩放，也不要先塌成 0 高度
  if (!tableBox.w) return {}
  // 宽度和高度都向上取整并留 1px 余量：canvas-box 是 overflow:hidden，
  // 而缩放后的边界常常落在半个像素上，四舍五入到下界会啃掉表格最后一根线。
  return {
    width: `${Math.ceil(tableBox.w * scale.value)}px`,
    height: `${Math.ceil(tableBox.h * scale.value) + 1}px`,
  }
})
const zoomLayerStyle = computed(() => ({
  width: `${availW.value}px`,
  transform: `scale(${scale.value})`,
}))

/** 滚动容器扣掉自身内边距后的可用宽度。 */
function hostAvailWidth() {
  const h = scrollHost.value
  if (!h) return 0
  const cs = getComputedStyle(h)
  return Math.max(0, h.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0))
}

/**
 * 量一次画布。
 *
 * 宽度变了要**等 DOM 真的更新完再量第二遍**。层宽是由 availW 决定的，而 Vue
 * 更新 DOM 是异步的：改完 availW 就直接量表，量到的还是上一版布局 —— 收起
 * 公式面板后画布变宽，按旧宽度算出的缩放比会大到 130% 还不止，而且因为
 * availW 之后不再变化，永远不会再被纠正回来。
 */
function measure() {
  const table = tableEl.value
  if (!table) return
  const w = hostAvailWidth()
  if (w !== availW.value) {
    availW.value = w
    nextTick(measure)
    return
  }
  if (table.offsetWidth !== tableBox.w) tableBox.w = table.offsetWidth
  if (table.offsetHeight !== tableBox.h) tableBox.h = table.offsetHeight
  // 适应宽度是「一直铺满」而不是「量一次就定死」：窗口缩放、面板收起、
  // 甚至某一列内容变长导致自然宽度变了，都得跟着重算。
  if (fitMode.value) fitToWidth()
}

function fitToWidth() {
  // 分母是表格自己的内容宽。用容器宽当分母的话，表格一铺满比值就是 1，
  // 这个按钮点了永远停在 100%，看着「能用」其实什么也没做。
  const next = fitScale(hostAvailWidth(), tableEl.value?.offsetWidth || 0)
  if (next === null) return          // 量不出来就保持原档，别写个 1 进去
  fitMode.value = true
  scale.value = next
}

function zoomBy(d) {
  fitMode.value = false
  scale.value = clampScale(scale.value + d)
}

function resetZoom() {
  fitMode.value = false
  scale.value = 1
}

/** Ctrl/⌘ + 滚轮：和打印预览一个手势习惯，捏着滚轮直接缩放画布。 */
function onWheel(e) {
  if (!e.ctrlKey && !e.metaKey) return
  e.preventDefault()
  fitMode.value = false
  scale.value = clampScale(scale.value + (e.deltaY < 0 ? 0.1 : -0.1))
}

let canvasRO = null
onMounted(() => {
  measure()
  if (typeof ResizeObserver === 'undefined') return
  canvasRO = new ResizeObserver(() => measure())
  // 容器宽度变了要重算；表格高度变了要重算占位。少观察一个都会留下
  // 「窗口拉大了表还是那么宽」「加了行下面被截掉」这种半截状态。
  if (scrollHost.value) canvasRO.observe(scrollHost.value)
  if (zoomLayer.value) canvasRO.observe(zoomLayer.value)
})
onBeforeUnmount(() => {
  canvasRO?.disconnect()
  canvasRO = null
})

/** 换模板、切设计模式、行高变化都会改表格尺寸，量一次。
 *  flush:'post' 保证量的时候 DOM 已经更新完 —— 否则量到的还是上一版的表。 */
watch([data, designMode], () => measure(), { flush: 'post' })

// ------------------------------------------------------------------ 单元格

function onCellClick(row, cell, item) {
  // 面板收着的时候点单元格，等于明确表示「我要改这一格」，顺手把它带出来 ——
  // 不然用户会以为点不动。
  inspectorOpen.value = true
  editing.value = {
    row, rowNo: row.row_no, colNo: cell.col_no, rowLabel: labelText(row, item.side),
    colLabel: item.col.label, label: cell.label, addr: cell.addr,
    value: cell.value, formula: dirty[cell.dirtyKey] ?? cell.formula ?? '',
  }
  loadBuilderFromFormula(editing.value.formula)
}

function applyFormula() {
  if (!editing.value || !builtFormula.value) return
  dirty[editing.value.rowNo + ':' + editing.value.colNo] = builtFormula.value
  editing.value.formula = builtFormula.value
  load(true)
}

function clearFormula() {
  if (!editing.value) return
  dirty[editing.value.rowNo + ':' + editing.value.colNo] = ''
  editing.value.formula = ''
  load(true)
}

function isEditingCell(row, cell) {
  return !!editing.value && editing.value.rowNo === row.row_no && editing.value.colNo === cell.col_no
}

function resetBuilder() {
  Object.assign(builder, {
    subjectCode: '', measure: '期末余额', rangeStart: '', rangeEnd: '', reference: '',
    leftRef: '', operator: '+', rightRef: '', conditionRef: '', conditionOp: '>', conditionValue: '0',
    whenTrue: '', whenFalse: '', advancedFormula: '',
  })
}

function loadBuilderFromFormula(formula) {
  resetBuilder()
  const src = String(formula || '').trim()
  if (!src) { builderType.value = 'subject'; return }
  // 只把「单个科目」识别为取科目；含有多个科目相加的复杂公式要留在高级模式，
  // 避免贪婪匹配把后半段当成取数参数。期间参数只有 {{本期}} 等价于留空，
  // 其它期间留给高级公式原样编辑，免得存回去时被悄悄改成本期。
  const subject = src.match(/^(?:科目)?\("([^"]+)","([^"]+)"(?:,([^()]+))?\)$/)
  const sum = src.match(/^(?:合计|SUM)\(([A-Z]+\d+):([A-Z]+\d+)\)$/i)
  const condition = src.match(/^IF\(([A-Z]+\d+)([<>=!]+)(-?(?:\d+(?:\.\d+)?|\.\d+)),(.+),(.+)\)$/i)
  const math = src.match(/^([A-Z]+\d+)([+\-*/])([A-Z]+\d+)$/i)
  if (subject && (!subject[3] || subject[3] === '{{本期}}')) {
    builderType.value = 'subject'; builder.subjectCode = subject[1]; builder.measure = subject[2]; return
  }
  if (sum) { builderType.value = 'sum'; builder.rangeStart = sum[1]; builder.rangeEnd = sum[2]; return }
  if (/^[A-Z]+\d+$/i.test(src)) { builderType.value = 'reference'; builder.reference = src.toUpperCase(); return }
  if (condition) {
    builderType.value = 'condition'; builder.conditionRef = condition[1].toUpperCase(); builder.conditionOp = condition[2]
    builder.conditionValue = condition[3]; builder.whenTrue = condition[4]; builder.whenFalse = condition[5]; return
  }
  if (math) {
    builderType.value = 'math'; builder.leftRef = math[1].toUpperCase(); builder.operator = math[2]; builder.rightRef = math[3].toUpperCase(); return
  }
  builderType.value = 'advanced'; builder.advancedFormula = src
}

async function saveAll() {
  const cells = Object.entries(dirty).map(([k, formula]) => {
    const [row_no, col_no] = k.split(':').map(Number)
    return { row_no, col_no, formula }
  })
  if (!cells.length) return ElMessage.info('没有改动')
  try {
    await api.saveCells(templateId.value, cells, store.bookId)
  } catch (e) {
    // **公式语法错现在后端当场 400**（以前是 200 存进去、等渲染时某一格蹦红字）。
    // 这里必须把 detail 摆到人眼前：整批被拒了，一格都没存，
    // 而「没存」只有一句话说不清 —— 到底是哪个格子、哪一处写法不对。
    // 改动一律保留（不 reset），人改完再点一次保存即可。
    const detail = e?.response?.data?.detail
    const lines = Array.isArray(detail)
      ? detail.map((d) => d.msg || String(d))
      : [typeof detail === 'string' ? detail : '保存失败（未改动数据库）']
    await ElMessageBox.alert(
      lines.join('\n'), '公式没存进去', { type: 'error', confirmButtonText: '知道了' }
    ).catch(() => {})
    return
  }
  ElMessage.success('公式已保存')
  dirtyKeysReset()
  load()
}

function dirtyKeysReset() {
  Object.keys(dirty).forEach((k) => delete dirty[k])
}

// ------------------------------------------------------------------ 行操作

async function insertRow(at) {
  await api.addRow(templateId.value, { row_no: at, label: '新项目' }, store.bookId)
  await load()
  ElMessage.success(`已在第 ${at} 行插入，下方公式引用已自动顺延`)
}

async function removeRow(row) {
  await ElMessageBox.confirm(
    `删除「${row.label}」这一行？下方行的公式引用会自动上收，相关单元格公式也会一起删除。`,
    '删除行', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
  ).catch(() => Promise.reject())
  const r = await api.deleteRow(templateId.value, row.row_no, store.bookId)
  await load()
  ElMessage.success(`已删除，${r.formulas_shifted} 处引用已同步修正`)
}

// 项目名输入框的两步：打字时先写回本地，失焦时再落库。
//
// 为什么不能合成一步（v-model + change 里直接存）：v-model 已经把新值写进了
// row.label，change 里再拿入参跟 labelText(row, side) 比「变了没有」，两个值
// 永远相等，请求就一次也发不出去 —— 看上去改了，一刷新又变回去。落库这一层
// 因此不再做相等判断：原生 change 事件本身就是「用户确实改过」的判据，改了
// 又改回来多发一次幂等 PATCH 无所谓。

/** 打字途中：把新值写回 row 本体，让 el-input 的 modelValue 跟得上手。 */
function draftSideLabel(row, side, value) {
  if (side === 0) { row.label = value; return }
  row.style = { ...(row.style || {}), label_right: value }
}

/** 失焦：落库。右栏整份 style 一起提交（draftSideLabel 已经在它上面改好了
 *  label_right），行次、缩进、加粗这些同行样式原样带回，不会被抹掉。 */
async function saveSideLabel(row, side, label) {
  if (side === 0) await api.updateRow(templateId.value, row.row_no, { label }, store.bookId)
  else await api.updateRow(templateId.value, row.row_no, { style: row.style }, store.bookId)
}

async function toggleRow(row, field, delta = null) {
  const payload = {}
  if (field === 'indent') payload.indent = Math.max(0, (row.indent || 0) + delta)
  else payload[field] = !row[field]
  await api.updateRow(templateId.value, row.row_no, payload, store.bookId)
  await load()
}

// ------------------------------------------------------------------ 列操作

async function insertCol(at) {
  const r = await api.addCol(templateId.value, { col_no: at, label: '新列' }, store.bookId)
  await load()
  ElMessage.success(`已插入 ${r.letter} 列，${r.formulas_shifted} 处引用已自动改写`)
}

async function removeCol(col) {
  await ElMessageBox.confirm(
    `删除「${col.label}」(${col.letter}) 整列？本列所有单元格公式会一并删除，引用它的公式会自动前移。`,
    '删除列', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
  ).catch(() => Promise.reject())
  const r = await api.deleteCol(templateId.value, col.col_no, store.bookId)
  await load()
  ElMessage.success(`已删除，${r.formulas_shifted} 处引用已同步修正`)
}

// 同 draftSideLabel 上方那段：v-model 已经把新值写进 c.label，这里再跟入参比
// 相等就永远成立，所以直接存。列名是 STATIC 列唯一可改的东西，这一路堵住
// 的话，用户在表头输入框里怎么打都存不下来。
async function renameCol(col, label) {
  await api.updateCol(templateId.value, col.col_no, { label }, store.bookId)
}

async function changeColKind(col, kind) {
  await api.updateCol(templateId.value, col.col_no, { kind }, store.bookId)
  col.kind = kind
  await load()
}

async function renameTemplate() {
  if (!template.value || !store.canWrite) return
  const result = await ElMessageBox.prompt('输入新的报表名称', '重命名报表', {
    inputValue: template.value.name,
    confirmButtonText: '保存',
    cancelButtonText: '取消',
    inputValidator: (value) => value.trim() ? true : '报表名称不能为空',
  }).catch(() => null)
  const name = result?.value?.trim()
  if (!name || name === template.value.name) return
  await api.updateTemplate(templateId.value, { name }, store.bookId)
  template.value.name = name
  const item = allTemplates.value.find((t) => t.id === templateId.value)
  if (item) item.name = name
  ElMessage.success('报表名称已更新')
}

async function deleteCurrentTemplate() {
  if (!template.value || !store.canWrite) return
  if (template.value.is_builtin) {
    ElMessage.warning('内置报表不能删除')
    return
  }
  const confirmed = await ElMessageBox.confirm(
    `确定删除「${template.value.name}」？删除后该报表的行、列和公式都会一并删除。`,
    '删除报表', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
  ).then(() => true).catch(() => false)
  if (!confirmed) return
  await api.deleteTemplate(templateId.value, store.bookId)
  // 重新算可见性：删掉的若是该类型唯一一张自有模板，内置模板会重新露出来，
  // 直接拿接口原样跳转就可能跳到一张已经被藏起来的表。
  allTemplates.value = visibleTemplates(await api.templates(store.bookId))
  ElMessage.success('报表已删除')
  if (!allTemplates.value.length) {
    router.replace('/reports')
    return
  }
  // 光换 URL 不够：/reports/:id 是同一个路由，router-view 不会重新挂载，
  // templateId 会停在已删掉的那张上，页面一直显示一张后端已经删了的报表。
  templateId.value = allTemplates.value[0].id
  router.replace(`/reports/${templateId.value}`)
  dirtyKeysReset()
  load()
}

// ------------------------------------------------------------------ 数据

async function load(keepDirty = false) {
  if (!templateId.value || !store.bookId || !store.periodId) return
  const tpl = allTemplates.value.find((t) => t.id === templateId.value)
  template.value = tpl
  // 只认最新一趟：期间/模板切换时，旧响应不许覆盖新数据。
  const res = await run(() => api.render(templateId.value, {
    book_id: store.bookId, period_id: store.periodId
  }))
  // 过期的那一趟：条件早就被改掉了，写进去就是「表里的数对不上顶栏的期间」。
  if (isStale(res)) return
  // 列字母挂在列上，行编辑按钮要用
  res.cols = res.cols.map((c) => ({ ...c, letter: letterOf(c.col_no) }))
  data.value = res
  if (designMode.value && keepDirty) {
    for (const row of res.rows) {
      // 键必须用 cell.col_no（1 基），跟 onCellClick 存进去的一致。
      // 用数组下标 ci（0 基）会永远匹配不上，编辑中的公式就"丢了"
      row.cells.forEach((cell) => {
        const key = `${row.row_no}:${cell.col_no}`
        if (dirty[key] !== undefined) {
          cell.formula = dirty[key]
          cell.value = '（待保存后重算）'
        }
      })
    }
  }
}

function onTemplateChange(id) {
  router.replace(`/reports/${id}`)
  dirtyKeysReset()
  load()
}
const printVisible = ref(false)
const printPath = computed(() => `/books/${store.bookId}/print/report.html`)
/**
 * report.html 的 template_id 是**报表**模板（哪张表、哪些行），不是版式；
 * 用户选的打印版式由 PrintDialog 自己按 print_template_id 送过去 ——
 * 这里两个都塞的话，后端只会看到一个。
 */
const printParams = computed(() => ({
  template_id: templateId.value, period_id: store.periodId,
}))

function previewHtml() {
  printVisible.value = true
}
function exportXlsx() {
  downloadFile(`/books/${store.bookId}/print/report.xlsx`,
    { template_id: templateId.value, period_id: store.periodId }, '报表.xlsx')
}

onMounted(async () => {
  // keepId：按 URL 直接打开一张已被隐藏的内置报表时，下拉里仍要留着它，
  // 否则 load() 按 id 查模板会查不到，页面标题和「是否内置」全成空。
  allTemplates.value = visibleTemplates(await api.templates(store.bookId), templateId.value)
  if (store.bookId) {
    // **年度必带**：科目表每个年度各有一份（后端迁移 0032）
    subjectOptions.value = await subjectApi.subjectFlat(
      store.bookId, { include_parents: true, year: store.currentYear }).catch(() => [])
  }
  if (!templateId.value && allTemplates.value.length) templateId.value = allTemplates.value[0].id
  load()
})
watch(() => store.bookId, async (bookId) => {
  if (bookId) {
    subjectOptions.value = await subjectApi.subjectFlat(
      bookId, { include_parents: true, year: store.currentYear }).catch(() => [])
  }
  load()
})
watch(() => store.periodId, () => load())
</script>
<style scoped>
.report-page { --report-ink: #182230; --report-muted: #728094; --report-line: #dce3ec; --report-blue: #2563eb; }
.report-toolbar { gap: 10px; align-items: center; padding: 10px 14px; border: 1px solid #dfe5ee; box-shadow: 0 2px 10px rgba(27, 44, 72, .04); }
.report-toolbar-copy { display: flex; align-items: baseline; gap: 10px; min-width: 190px; color: var(--report-ink); }
.report-toolbar-actions { display: flex; align-items: center; justify-content: flex-end; gap: 8px; margin-left: auto; flex-wrap: wrap; }
.toolbar-kicker { color: var(--report-blue); font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
.toolbar-context { color: var(--report-muted); font-size: 12px; }
.template-select { width: 160px; }
.toolbar-button { white-space: nowrap; }
.toolbar-button .el-icon { margin-right: 5px; }
.save-button { min-width: 82px; }
.design-strip { display: flex; align-items: center; gap: 10px; margin: 0 0 12px; padding: 10px 14px; color: #244168; background: #eef5ff; border: 1px solid #d7e6ff; border-radius: 6px; }
.design-strip-mark { display: flex; align-items: center; justify-content: center; width: 28px; height: 28px; color: #fff; background: var(--report-blue); border-radius: 6px; }
.design-strip-copy { display: grid; gap: 2px; }
.design-strip-copy strong { font-size: 13px; }
.design-strip-copy span { font-size: 12px; color: #607594; }
.design-strip .el-tag { margin-left: auto; }

.report-card { padding: 18px; border-color: #dfe5ee; box-shadow: 0 6px 20px rgba(27, 44, 72, .04); }
.report-head { text-align: center; margin: 0 0 18px; }
.report-heading-eyebrow { color: var(--report-blue); font-size: 11px; font-weight: 700; letter-spacing: .14em; }
.report-title { margin: 4px 0 5px; color: var(--report-ink); font-size: 20px; letter-spacing: 5px; }
.report-sub { font-size: 12px; color: var(--report-muted); }
.report-notices { display: grid; gap: 8px; margin: 0 0 12px; }
.notice-item { margin: 0 !important; }

.report-workspace { display: grid; grid-template-columns: minmax(0, 1fr); gap: 14px; align-items: start; }
.report-workspace.has-inspector { grid-template-columns: minmax(0, 1fr) 330px; }
.report-canvas { min-width: 0; border: 1px solid #e2e8f0; border-radius: 7px; overflow: hidden; background: #fff; }
.canvas-toolbar { display: flex; align-items: center; gap: 8px; min-height: 38px; padding: 0 12px; border-bottom: 1px solid #e8edf3; background: #fbfcfe; }
.canvas-title { display: inline-flex; align-items: center; gap: 6px; color: var(--report-ink); font-weight: 650; font-size: 12px; }
.canvas-title .el-icon { color: var(--report-blue); }
.canvas-hint { color: var(--report-muted); font-size: 11px; }
.canvas-hint::before { content: '·'; margin-right: 7px; color: #b4bdc9; }
/* 缩放控件靠右，和「打印模板」舞台上的那组是同一套图标与手感 */
.zoom-tools { display: flex; align-items: center; gap: 8px; margin-left: auto; }
.zoom-tools :deep(.el-button-group .el-button) { padding: 6px 9px; }
.zoom-label { min-width: 62px; font-variant-numeric: tabular-nums; }

/* 唯一的滚动容器：整张表按自然尺寸排版，外层 canvas-box 按缩放后的尺寸占位，
   内层 canvas-zoom 负责 transform。脚本里的注释讲了为什么必须分这两层。 */
.table-scroll { overflow: auto; }
.canvas-box { position: relative; overflow: hidden; min-height: 120px; }
.canvas-zoom { position: absolute; top: 0; left: 0; transform-origin: top left; }

.report-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.report-table th, .report-table td { border: 1px solid var(--report-line); padding: 7px 8px; }
.report-table th { background: #f7f9fc; color: #4d5a6d; font-weight: 650; text-align: center; }
.report-table thead th { height: 46px; }
.report-table td.c-num { text-align: right; font-variant-numeric: tabular-nums; }
.report-table td.c-label { text-align: left; white-space: nowrap; }
/* 行次列：法定编号，居中弱化，别跟项目名抢眼 */
.report-table td.c-line { text-align: center; color: var(--cwq-muted); font-variant-numeric: tabular-nums; }
/* 右栏项目名单独加粗（tr.bold 只由行级 row.bold 决定，管不到右栏） */
.report-table td.c-label.b { font-weight: 700; }
.report-table tr.bold td { font-weight: 700; }
/* 段标题 / 合计 / 平衡校验都挂在**单元格**上，不是整行一个类：
   资产负债表同一行两侧性质不同（第 16 行左边「流动资产合计」要压灰，
   右边「递延收益」是普通项目），挂到 tr 上会把右半一起染色。
   与打印渲染器（printing/template.py 的 td.section / td.total）同一口径。 */
.report-table td.is-shade.c-label { background: #eef1f5; color: #354154; }
/* 平衡校验行：不是法定行次，但它不为 0 就说明账不平，淡淡提一下。
   它的项目名是一整句诊断（「资产总计 − 负债和所有者权益总计，应为 0」），
   不许折行的话会把整张表撑宽。 */
.report-table td.is-check.c-label, .report-table td.is-check.c-num { background: #f1faf5; }
.report-table td.is-check.c-label { white-space: normal; }
.report-table td.editable { cursor: cell; }
.report-table td.editable:hover { background: #eff6ff; outline: 1px dashed var(--report-blue); outline-offset: -1px; }
.report-table td.selected { background: #e7f0ff !important; outline: 2px solid var(--report-blue); outline-offset: -2px; }
.report-table td.error { background: #fef2f2; color: var(--cwq-danger); }
.report-table td.dirty { background: #fff8e6; }
.report-table tr.is-hidden td { opacity: .45; }

/* 地址角标：设计模式下每个格子都标 B9 这样的坐标，
   用户写公式时需要看见自己在改哪一格 */
.addr {
  font-size: 10px; color: var(--cwq-muted); font-weight: 400;
  font-variant-numeric: tabular-nums;
}
td.c-num .addr { float: right; margin-left: 6px; }
th.c-head .addr { margin-top: 2px; }

/* 列头的增删按钮走常规流：绝对定位会被 label 输入框压住，点不到也看不见 */
.report-table th.c-head { vertical-align: top; }
.head-edit { display: flex; justify-content: flex-end; gap: 2px; margin-bottom: 2px; }
.col-label { width: 100%; }
.col-meta { margin-top: 4px; }
.col-meta :deep(.el-select) { width: 100%; }

/* 口径角标：小到不抢眼，但一眼能看见「这一列的数跟总账不是同一口径」。
   打印/导出走的是后端那份模板，这里是屏幕上的说明，所以标 no-print。 */
.caliber {
  display: inline-block;
  margin-top: 3px;
  padding: 0 4px;
  border: 1px solid #e6a23c;
  border-radius: 3px;
  background: #fdf6ec;
  color: #b8823b;
  font-size: 10px;
  line-height: 15px;
  font-weight: 400;
  white-space: nowrap;
  vertical-align: middle;
}
.notice-sub { margin-top: 4px; font-size: 12px; }
.notice-sub .money { font-variant-numeric: tabular-nums; }

th.c-gutter, td.c-gutter { background: #fbfbfc; width: 1%; white-space: nowrap; }
.gutter { display: flex; align-items: center; gap: 0; }
.gutter .addr { min-width: 18px; }
.row-label { width: 100%; }

.formula-inspector { position: sticky; top: 12px; min-width: 0; max-height: calc(100vh - 150px); overflow: auto; border: 1px solid #dfe5ee; border-radius: 7px; background: #fff; box-shadow: 0 5px 18px rgba(27, 44, 72, .07); }
.inspector-content { padding: 16px; }
.inspector-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; padding-bottom: 14px; border-bottom: 1px solid #edf0f4; }
.inspector-kicker { display: block; margin-bottom: 5px; color: var(--report-blue); font-size: 11px; font-weight: 700; letter-spacing: .08em; }
.inspector-head h3 { display: flex; align-items: center; gap: 7px; margin: 0; color: var(--report-ink); font-size: 15px; }
.inspector-head p { margin: 6px 0 0; color: var(--report-muted); font-size: 11px; }
.cell-badge { display: inline-flex; align-items: center; height: 22px; padding: 0 6px; color: var(--report-blue); background: #edf4ff; border-radius: 4px; font-family: ui-monospace, monospace; font-size: 11px; }
.builder-modes { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; padding: 12px 0; border-bottom: 1px solid #edf0f4; }
.builder-mode { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; min-height: 56px; padding: 6px 4px; color: #657286; background: #f8fafc; border: 1px solid #e5eaf1; border-radius: 5px; cursor: pointer; font: inherit; font-size: 11px; transition: border-color .15s ease, background-color .15s ease, color .15s ease; }
.builder-mode .el-icon { font-size: 16px; }
.builder-mode:hover { color: var(--report-blue); border-color: #b9d0fb; background: #f3f7ff; }
.builder-mode.active { color: var(--report-blue); border-color: #8eb3f4; background: #edf4ff; font-weight: 650; }
.builder-body { padding-top: 14px; }
.builder-form { display: grid; gap: 7px; }
.builder-intro { margin-bottom: 4px; color: #465469; font-size: 12px; font-weight: 600; }
.builder-label { margin-top: 4px; color: #7c8797; font-size: 11px; }
.builder-input { width: 100%; }
.math-row { display: grid; grid-template-columns: minmax(0, 1fr) 44px minmax(0, 1fr); gap: 5px; }
.operator-select :deep(.el-input__wrapper) { padding: 0 7px; }
.condition-row { display: flex; align-items: center; gap: 6px; }
.condition-op { width: 112px; flex: 0 0 112px; }
.condition-label { width: 52px; color: #7c8797; font-size: 11px; flex: 0 0 52px; }
.formula-preview { display: grid; gap: 6px; margin-top: 16px; padding: 10px; border: 1px solid #dce7f8; border-radius: 5px; background: #f7faff; }
.preview-label { color: var(--report-blue); font-size: 11px; font-weight: 700; }
.preview-copy { color: #59677b; font-size: 11px; }
.formula-preview code { display: block; overflow-wrap: anywhere; color: #244168; font-size: 11px; line-height: 1.5; }
.inspector-actions { display: flex; align-items: center; gap: 6px; margin-top: 16px; padding-top: 12px; border-top: 1px solid #edf0f4; }
/* 全局的 .spacer 只认 .toolbar / .topbar，这里得自己撑开，否则三个按钮会挤在左边 */
.inspector-actions .spacer { flex: 1; }
.inspector-toggle { flex: none; color: var(--report-muted); }
.inspector-toggle .el-icon { margin-right: 4px; }
.inspector-empty { display: grid; justify-items: center; gap: 8px; padding: 64px 24px; color: var(--report-muted); text-align: center; }
.inspector-empty strong { color: #465469; font-size: 13px; }
.inspector-empty span { font-size: 11px; }
.empty-icon { display: flex; align-items: center; justify-content: center; width: 42px; height: 42px; color: var(--report-blue); background: #edf4ff; border-radius: 50%; font-size: 20px; }

.edit-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }
.badge {
  font-family: ui-monospace, monospace; font-weight: 700; background: var(--cwq-primary);
  color: #fff; padding: 1px 8px; border-radius: 4px;
}
.fn-list { margin-top: 14px; max-height: 240px; overflow: auto; font-size: 12px; }
.fn-list.compact { max-height: 210px; margin-top: 10px; padding-top: 10px; border-top: 1px solid #edf0f4; }
.fn-title { font-weight: 600; margin-bottom: 4px; color: var(--cwq-muted); }
.fn { display: flex; gap: 10px; padding: 3px 0; align-items: baseline; }
.fn code { background: #f3f4f6; padding: 1px 5px; border-radius: 3px; white-space: nowrap; }
.fn span { color: var(--cwq-muted); }
.fn-list.compact .fn { display: grid; grid-template-columns: 1fr; gap: 4px; align-items: start; padding: 7px 0; border-bottom: 1px solid #f0f2f5; line-height: 1.5; }
.fn-list.compact .fn:last-child { border-bottom: none; }
.fn-list.compact .fn code { min-width: 0; white-space: normal; overflow-wrap: anywhere; line-height: 1.45; }
.fn-list.compact .fn span { min-width: 0; overflow-wrap: anywhere; }
code.ref { display: inline-block; margin: 2px 4px; background: #f3f4f6; padding: 1px 5px; border-radius: 3px; }
.mb { margin-bottom: 12px; }

@media (max-width: 1180px) {
  .report-toolbar-copy { min-width: 170px; }
  .report-toolbar { flex-wrap: wrap; }
  .report-toolbar-actions { margin-left: auto; }
  .report-workspace.has-inspector { grid-template-columns: minmax(0, 1fr) 300px; }
}
@media (max-width: 900px) {
  .report-workspace.has-inspector { grid-template-columns: 1fr; }
  .formula-inspector { position: static; max-height: none; }
  .report-toolbar-copy { width: 100%; margin-right: 0; }
  .template-select { flex: 1; min-width: 140px; }
  .canvas-toolbar { flex-wrap: wrap; padding: 6px 12px; }
  .zoom-tools { margin-left: auto; }
}

/* 缩放只服务于屏幕。真出纸走的是后端那份打印模板（点「预览打印」），
   但万一路人直接对这个页面按 Ctrl+P，也不能把当前档位的缩放带进纸里 ——
   缩到 60% 的一整版资产负债表，打出来是看不清的。 */
@media print {
  .canvas-zoom { transform: none !important; width: auto !important; }
  .canvas-box { width: auto !important; height: auto !important; min-height: 0; overflow: visible; }
  .table-scroll { overflow: visible !important; }
}
</style>
