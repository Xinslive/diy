<template>
  <!-- voucher-page：这一页占满 .content，凭证卡片把剩余高度交给独立分录滚动区。
       高度链是 .content（定高）→ .voucher-page → .card → .entry-wrap → .entry-scroll；
       flex 链上的容器都要 min-height:0，否则分录区会被内容撑开，失去内部滚动。 -->
  <div class="page voucher-page">
    <div class="toolbar no-print">
      <!-- 文案写「凭证列表」而不是「返回」（2026-10-28 用户报）：
           这一格不是「回到上一页」，是「回到凭证列表」——
           顶上那一排是前一张/后一张，页脚是别的账表，说「返回」只说明往回走，
           往回走到哪儿得靠人猜。写清楚去哪儿，点的人才知道点完会看到什么。 -->
      <el-button @click="backToList">凭证列表</el-button>
      <el-divider direction="vertical" />
      <span class="muted">凭证号</span>
      <!-- 只读：号由系统按期间自动生成，新增 / 插入 / 修改三处都不许手工改
           （用户 2026-10-19 定的）。这里保留一个可点的框而不是纯文字，
           是为了跟右边日期框对齐；readonly 而非 disabled，禁用态会连背景一起
           变灰，读起来像"这张不能看"，实际上号是照常显示的。
           插入/增加模式下预填的就是后端算好的那个号（见 insert-preview），
           保存时服务端重新算一次，页面上的号只是给人看的。
           宽度按**内容**给，不再留一大截空白：号是「凭证字 + 4 位数字」
           （记0015），14px Arial 下大约 45px 宽，加上输入框左右 11px 内边距
           和 1px 边框共 24px，80px 装得下还剩 11px 余量；再宽就只是空框了。 -->
      <el-input v-model="form.voucher_no" readonly style="width: 80px" />
      <span class="muted">日期</span>
      <!-- 日期同理收窄，但要给左边的日历图标留 22px：「2026-12-25」十个字符
           约 72px，图标 22px + 内边距边框 24px，128px 刚好装满不挤。
           clearable 关掉：日期框里那个「×」是清空按钮，日期是这张凭证的
           一部分，不是筛选项 —— 一个手滑的 × 把日期抹掉，比日期框窄一点
           严重得多，而且页面上没有任何入口能把它找回来。 -->
      <el-date-picker v-model="form.voucher_date" type="date" value-format="YYYY-MM-DD"
                      :clearable="false" style="width: 128px" />
      <span class="spacer" />
      <!-- 前一张/后一张翻的是**凭证号顺序**（跟列表「凭证号顺序」那一档同一套）：
           同一张期间内，号小的在前、号大的在后 —— 「前一张」就是凭证号小一号的那张。
           **翻的范围就是列表给用户看的那一批**，跟列表页那条规则同源：
           从列表带条件进来的，就在**筛选结果集**里翻（条件原样带着，可以跨月跨年）；
           没带条件的，就在**这张凭证所属的那个月**里翻 —— 到月底停住，
           月内最后一张的「后一张」变灰，不跳到下个月第一张。
           （2026-10-25 用户打回：「某月最后一张凭证点击下一张，居然能跳后一个月
           第一张凭证。这不对」——列表停在 12 月，翻却能翻到 1 月，两套口径。）
           插入/增加直接开一张新的。原来那个「增加分录」按钮去掉了 ——
           分录靠回车往下加，不用专门去找按钮。
           原来这里还挂着一个气泡，写着「全部凭证里的第 1634 / 2147 张」
           （2026-10-24 用户打回）：那句话解释的是按钮**本来就在干的事**，
           而按钮本身能不能点（:disabled）已经把「有没有前一张/后一张」说完了，
           多这一句只是噪音 —— 跟「凭证页不摆说明条」那条规矩是同一件事。
           所以这里只留按钮，不留气泡。 -->
      <el-button-group>
        <el-button :disabled="!prevId" @click="goNeighbour('prev')">前一张</el-button>
        <el-button :disabled="!nextId" @click="goNeighbour('next')">后一张</el-button>
      </el-button-group>
      <!-- 这四颗动作按钮（插入 / 增加 / 拆分 / 打印）挨得太开：
           Element Plus 给「相邻的按钮」加了 12px 左边距
           （.el-button + .el-button），工具条自己又有 8px 的缝，
           两颗之间就成了 20px —— 一排四颗占掉四百多像素，看着是散着的。
           收进一个自己的小分组：左边距去掉，只留 4px 的缝。 -->
      <div class="tb-acts">
        <el-button :disabled="!canAdd" @click="insertVoucher">插入凭证</el-button>
        <el-button :disabled="!canAdd" @click="addVoucher">增加凭证</el-button>
        <!-- 拆分凭证：把这一张按分录边界拆成两张。**写**动作，所以跟插入/增加
             排在一组 —— 「打印预览」是读，不受「这张不能改」的三条限制影响，
             排在它前面会让人以为拆也是只读的事。 -->
        <el-tooltip :content="splitHint" placement="bottom">
          <el-button :disabled="!canSplit" @click="openSplit">拆分凭证</el-button>
        </el-tooltip>
        <!-- 打印是**读**，不是写：所以它不受「这张不能改」的三条限制
             （已记账 / 所在期间已结账 / 只读账号）影响，照样能点。
             后端那条接口（GET /print/vouchers.html）本来就没有写门禁，只挂
             get_my_book —— 把一张已记账的凭证打出来，是归档前最常做的事，
             把它拦掉拦掉的是最该让打出来的那一类。
             保留的唯一条件是有这张凭证可打（v-if="voucherId"）。 -->
        <el-button v-if="voucherId" @click="printVoucher">打印预览</el-button>
      </div>
    </div>

    <div class="card">
      <div class="voucher-head">
        <div><span class="muted">会计期间</span> <b>{{ voucherPeriod?.code || store.currentPeriod?.code }}</b></div>
        <!-- 标签上写着「已记账」，前面再顶一个「状态」纯属重复；去掉之后这一行
             少一块零碎的东西，视线不用在两个字上绊一下 -->
        <div>
          <el-tag v-if="form.status === 'POSTED'" type="success" size="small">已记账</el-tag>
          <el-tag v-else type="warning" size="small">未记账</el-tag>
        </div>
        <!-- 说的是「这一张所在的期间」，不是顶栏那个月：两张凭证可以摊在两个期间里，
             而能不能改由它们各自那期说了算。含糊成「当前期间」会让人以为说的是顶栏。 -->
        <div v-if="locked" class="tag-bad">该凭证所在期间已结账，不可修改</div>
        <div v-else-if="posted" class="tag-bad">
          已记账凭证已锁定，不能修改。请到「期末结账」对本期间执行「反结账」后再改。
        </div>
        <!-- 只读账套也得说一声：不然一屏全灰着，人只能猜是不是页面坏了 -->
        <div v-else-if="!store.canWrite" class="tag-bad">这个账套是只读的，不能修改凭证</div>
      </div>

      <!-- 左边空出的 48px 是给每一行那对 ⊕ / ＋ 的：它们挂在各自那一行的左边，
           哪一行变绿才从那儿冒出来，平时不跟数字抢地方。
           这条留白带在**滚动区里面**（.entry-scroll 的 padding）——
           放在外面的话，横向溢出时挂在行边的按钮会被滚动容器裁掉。 -->
      <div class="entry-wrap">
        <!-- 表头固定在滚动区上方；分录行单独滚，合计固定在滚动区下方。
             三段共用 entry-wrap 上的 --vg-cols，表头、分录、合计的竖线保持对齐。 -->
        <div class="vg-row vg-head">
            <div class="vg-cell th th-idx">序号</div>
            <div class="vg-cell th th-sum">摘要</div>
            <div class="vg-cell th th-subj">会计科目</div>
            <div class="vg-cell th th-amt d">借方金额</div>
            <div class="vg-cell th th-amt c">贷方金额</div>
            <div class="vg-cell th th-units d"><AmountUnits /></div>
            <div class="vg-cell th th-units c"><AmountUnits /></div>
          </div>

        <!-- 这里只有分录行会滚；左右 48px / 52px 留白属于滚动区本身，
             行外的 ⊕ / ＋ / ⊗ 跟着分录一起移动，不会被裁掉。 -->
        <div class="entry-scroll" ref="entryScroll">
          <div class="vgrid">
            <!-- 一行一个盒子：鼠标扫到哪一行，哪一行整行变绿，
                 ⊕ / ＋ 只在这一行左边冒出来 -->
            <div v-for="(row, i) in form.entries" :key="i" class="vg-row"
               :class="{ 'is-on': isOn(row) }"
               :ref="(el) => setRowRef(el, i)"
               @mouseenter="hoverRow = row" @mouseleave="hoverRow = null">
            <div class="vg-act">
              <el-button class="g-ins" circle size="small" title="在本格子上面插一行"
                         :disabled="readonly" @click.stop="insertRowAbove">
                <el-icon><Top /></el-icon>
              </el-button>
              <el-button class="g-add" circle size="small" title="在本格子下面插一行"
                         :disabled="readonly" @click.stop="insertRowBelow">
                <el-icon><Bottom /></el-icon>
              </el-button>
            </div>
            <div class="vg-cell td td-idx">{{ i + 1 }}</div>
            <!-- 锁的是「行」不是「格」：一行之内先填科目还是先填金额都行，
                 只有前面还有没写完的分录时，这几格才一起不开张。
                 禁用的 input 收不到点击（浏览器压根不给它派发），
                 所以还得把指针穿透掉，让点击落到格子上，提示才出得来 -->
            <div class="vg-cell td td-sum" :class="{ 'is-blocked': blocked(i) }"
                 @click="warnBlocked(i)">
              <!-- 光标进这一格就整格选中：摘要多半是同一句话抄好几遍，
                   要改就整句重打，不必先把光标挪到末尾一个一个退格。
                   但只管刚进来的那一下 —— 再点一下、再按一下方向键，
                   光标就该回到人放的位置，好改其中几个字。
                   用 textarea 而不是 input：摘要写长了单行框会把它截在边上，
                   看不见自己到底写了什么，得专门点进去拖光标才看得到 ——
                   写满一行自动折成两行（最多两行，再多就在框里滚）。 -->
              <textarea v-model="row.summary" class="cell-input" rows="1"
                        :disabled="readonly || blocked(i)"
                        :ref="(el) => setSumRef(el, i)"
                        placeholder="摘要" @focus="focusSumText(i, $event)"
                        @click="selectText($event)" @input="autosizeSum($event.target)"
                        @keydown="onSumKeydown(i, $event)"
                        @keydown.enter.prevent="moveNext(i, 'sum')" />
            </div>
            <div class="vg-cell td td-subj"
                 :class="{ 'is-blocked': blocked(i), 'is-bad': badSubject(row) }"
                 @click="warnBlocked(i)">
              <SubjectPicker
                v-model="row.subject_id"
                :ref="(el) => setSubjectRef(el, i)"
                :options="subjects"
                :disabled="readonly || blocked(i)"
                :has-bottom-hint="!!balanceText(i, row)"
                :allow-inactive="false"
                @focusin="activeRow = row"
                @change="onSubject(row)"
                @add="(kw) => openSubjectCreate(i, kw)"
                @editing="(v) => onMarking(row, v)"
                @enter="moveNext(i, 'subj')"
                @picked="moveNext(i, 'subj')"
                @left="moveSide(i, 'subj', -1)" @right="moveSide(i, 'subj', 1)"
              />
              <!-- 辅助记账：这个科目**到这一行为止**还剩多少（借正贷负）。压在格子左下角，
                   不占行高 —— 分录格是「从上往下写」的一张纸，正中得留给科目名。
                   数字是**滚动**的：同一张凭证里同一科目出现三次借 100，
                   三行分别显示 100 / 200 / 300（不是都显示 300）。
                   只读时不显示：余额是给「正在录」的人看的，核对账另有科目余额表。 -->
              <span v-if="balanceText(i, row)" class="subj-balance">{{ balanceText(i, row) }}</span>
            </div>
            <div class="vg-cell td td-amt d" :class="{ 'is-blocked': blocked(i) }"
                 @click="warnBlocked(i)">
              <AmountGrid
                :model-value="debitOf(row)"
                :ref="(el) => setAmtRef(el, i, 'd')"
                :readonly="readonly || blocked(i)"
                @update:model-value="(v) => setDebit(row, v)"
                @focus="activeRow = row"
                @enter="moveNext(i, 'd')"
                @up="moveRow(i, -1, 'd')" @down="moveRow(i, 1, 'd')"
                @left="moveSide(i, 'd', -1)" @right="moveSide(i, 'd', 1)"
                @balance="autoBalance(i, 'd')"
                @commit="commitAmount(row)"
              />
            </div>
            <div class="vg-cell td td-amt c" :class="{ 'is-blocked': blocked(i) }"
                 @click="warnBlocked(i)">
              <AmountGrid
                :model-value="creditOf(row)"
                :ref="(el) => setAmtRef(el, i, 'c')"
                :readonly="readonly || blocked(i)"
                @update:model-value="(v) => setCredit(row, v)"
                @focus="activeRow = row"
                @enter="moveNext(i, 'c')"
                @up="moveRow(i, -1, 'c')" @down="moveRow(i, 1, 'c')"
                @left="moveSide(i, 'c', -1)" @right="moveSide(i, 'c', 1)"
                @balance="autoBalance(i, 'c')"
                @commit="commitAmount(row)"
              />
            </div>
            <!-- 删除 ⊗ 不占格子的位置：它挂在**自己这一行**的右边外面，
                 跟左边的 ⊕ / ＋ 一个待遇 —— 只有变绿的那一行才看得见它。
                 底下那几行「等着填」的空位上没有 ⊗：那是留给人写字的地方，
                 删了它立刻又被补回来，看着像没删。 -->
            <div class="vg-del">
              <el-icon v-if="readonly || canDeleteRowAt(i)" class="row-del" :class="{ 'is-off': readonly }"
                       :title="delTitle" @click.stop="removeRow(i)"><CircleCloseFilled /></el-icon>
            </div>
          </div>
          </div>
        </div>

          <!-- 合计固定在滚动区下方，录到第几十行都看得见 -->
          <div class="vg-row vg-total">
            <!-- 合计格只写「合计」两个字，靠右贴住借方金额列。中文大写搬到打印模板去了
                 （打印的「合计大写」由后端出），纸上那张凭证才留得住这张老规矩 -->
            <div class="vg-cell td td-sumcell">合计</div>
            <!-- 合计是 0 就不往格子里塞「0」：空格子比一个孤零零的 0 干净 -->
            <div class="vg-cell td td-total d"><AmountCells :value="total.debit" /></div>
            <div class="vg-cell td td-total c"><AmountCells :value="total.credit" /></div>
          </div>
      </div>

      <div class="vfoot">
        <span class="foot-right">
          <span v-if="saving" class="muted">正在自动保存…</span>
          <span v-else-if="savedAt" class="saved-tag">已自动保存 {{ savedAt }}</span>
          <span v-if="nonLeafRows.length" class="tag-bad">
            第 {{ form.entries.indexOf(nonLeafRows[0]) + 1 }} 行不是末级科目
          </span>
          <span v-if="blank" class="muted">还没录金额</span>
          <!-- 有金额没科目的行不会被提交（见 payableEntries）。
               这一档必须排在「借贷平衡」之前：它提交时不存在，
               拿「借贷平衡 ✓」给它看就是骗人（审计 §3.23）。 -->
          <span v-else-if="rowsMissingSubject.length" class="tag-bad">
            第 {{ form.entries.indexOf(rowsMissingSubject[0]) + 1 }} 行还差会计科目
          </span>
          <span v-else-if="balanced" class="tag-ok">借贷平衡 ✓</span>
          <span v-else class="tag-bad">{{ diffTag }}</span>
        </span>
      </div>

      <SubjectFormDialog v-model="subjectCreateOpen" :tree="subjectTree" :preset="subjectPreset"
                        :year="subjectYear" @saved="onSubjectCreated" />
    </div>

    <PrintDialog
      v-model="printVisible"
      :book-id="store.bookId"
      :path="printPath"
      scope="VOUCHER"
      :params="printParams"
      title="打印记账凭证"
    />

    <!-- 拆分凭证：选一个分界处，把这张按前后两半分成两张。
         拆完这一页要重载 —— 原凭证的行数少了（号不动），新凭证在后面。 -->
    <VoucherSplitDialog
      v-model="splitVisible"
      :book-id="store.bookId"
      :voucher-id="voucherId"
      :entries="splitRows"
      @confirm="onSplit"
    />
  </div>
</template>

<script setup>
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as api from '@/api/voucher'
import * as subjectApi from '@/api/subject'
import * as ledgerApi from '@/api/ledger'
import { useSessionStore } from '@/stores/session'
import { useTabsStore } from '@/stores/tabs'
import { moneyText } from '@/utils/format'
import { centsToText, moneyCents } from '@/utils/amountGrid'
import { displayBalanceCents } from '@/utils/balanceSign'
import { findFirstProblem, lastFilledRowAt, rowCanBeDeleted } from '@/utils/voucherRows'
import { setPendingFlush, takePendingFlush } from '@/utils/voucherFlush'
import { todayLocal } from '@/utils/datetime'
import AmountGrid from '@/components/AmountGrid.vue'
import SubjectPicker from '@/components/SubjectPicker.vue'
import AmountCells from '@/components/AmountCells.vue'
import AmountUnits from '@/components/AmountUnits.vue'

// **这两个弹窗是按需加载的**（2026-10-24 性能审计改）。
//
// 原来它们是静态 import，于是**每次进凭证页都要把它们拽下来**：
//   * SubjectFormDialog —— 40.8 KB raw / 14.2 KB gzip，里面带着
//     el-tree + el-tree-select + el-text；
//   * PrintDialog —— 9.5 KB raw / 4.1 KB gzip。
// 而**绝大多数人根本不会点「新增科目」**，更不会一进页面就打印 ——
// 这 18 KB gzip 是白付的固定开销。
//
// 改成 defineAsyncComponent 之后，它们各自分成独立 chunk，只有真的
// 打开弹窗时才会去取。代价是首次打开有一个网络往返（几十 KB，本地
// 开发是瞬时的，生产上也在几十毫秒量级），换来的是首屏少 18 KB gzip。
//
// 用 defineAsyncComponent 而不是在模板里写动态 import()：前者保留了在
// 模板里直接写 ``<SubjectFormDialog v-model=... />`` 的用法。
const SubjectFormDialog = defineAsyncComponent(() => import('@/components/SubjectFormDialog.vue'))
const PrintDialog = defineAsyncComponent(() => import('@/components/PrintDialog.vue'))
import VoucherSplitDialog from '@/components/VoucherSplitDialog.vue'
import { rafThrottle } from '@/utils/frame'
import { Bottom, CircleCloseFilled, Top } from '@element-plus/icons-vue'

const store = useSessionStore()
const tabs = useTabsStore()
const route = useRoute()
const router = useRouter()

// 这张表单属于**哪个账套**。切账套时 store.bookId 先变成新的，
// 此刻任何拿 store.bookId 发的请求，都是把上一个账套的凭证写进新账套 ——
// 这个 ref 记下表单自己的归属，save() 认它，不认 store 上那个正在变的值。
const formBookId = ref(0)

// 这张凭证的 id：0 = 还没落库的空凭证。
// 原来是 computed(route.params.id)，自动保存新建之后 URL 还没来得及换的那一瞬间
// 会以为它是张新凭证 —— 再存一次就多一张重复的
const voucherId = ref(0)
const saving = ref(false)
const subjects = ref([])
const subjectTree = ref([])
// 「插入凭证」/「增加凭证」这一次的目标：参照是谁、这张拿哪个号、谁要顺位。
// position 区分前后（before = 向前占参照的号，after = 向后拿参照号 +1）。
// 只在 voucher-new 且带 ?after= 时有值；存下去就清掉（那时候它已经是张普通凭证了）。
const insertCtx = ref(null)
// 前一张 / 后一张：带着列表筛选条件时翻结果集（可跨月跨年），
// 没有条件时翻**这张凭证所属的那个月**（到月底停住）—— 范围由后端定，
// 前端只管把 prev_id / next_id 拿回来显示成能不能点。
const prevId = ref(null)
const nextId = ref(null)
// 当前正在录入的那一行，整行淡绿底：连续录入时眼睛得有个落点
const activeRow = ref(null)
// 鼠标正压着的那一行也算 —— 手扫到哪一行，那一行就变绿
const hoverRow = ref(null)
/** 变绿的那一行。⊕ / ＋ / ⊗ 都只跟着它出现，所以按钮不会指错行。 */
const greenRow = computed(() => hoverRow.value ?? activeRow.value)
const isOn = (row) => row === greenRow.value

/**
 * 这一行能不能填：前面还有没有没写完的分录。
 *
 * 锁的是**行**不是**格**。一行之内先填科目还是先填金额都认 —— 上一条没写完就锁住
 * 这一行的所有格子，包括摘要，那是「不许跳过中间去写第 3 行」的意思，不是
 * 「这一行里科目和金额谁先谁后」。
 * 返回挡着的那一条（用来把话说清楚：第几行还差什么），没有就返回 null。
 */
function blocker(i) {
  const at = firstProblemAt.value
  return at < i ? form.entries[at] : null
}
/**
 * 第一条「没写完」的分录在第几行（没有就等于行数）—— 判定在 utils/voucherRows.js。
 * `blocked(i)` 挂在每一行上问，靠的是这一个下标，不再每行从头扫一遍
 * （以前每行扫到 i，一屏 154 行就是 1.2 万次判断，套上科目表里的线性查找
 * 就是 O(行数² × 科目数)：那张凭证打开一次 41s，主线程整个占住）。 */
const firstProblemAt = computed(() => findFirstProblem(form.entries, rowProblem))
const blocked = (i) => firstProblemAt.value < i
function warnBlocked(i) {
  const e = blocked(i) ? blocker(i) : null
  if (!e || readonly.value) return
  const at = form.entries.indexOf(e) + 1
  ElMessage.warning(rowProblemText(e, at))
}

/**
 * 这一行还差什么：'subj'（没科目）/ 'nonleaf'（挂到了上级科目）/ 'amt'（没金额）/ null。
 * 非末级科目跟「没写完」是一类东西：它是一条**记错地方**的分录，
 * 放着不管既让后面几笔串不起来，存下去也是错的，一样得当场拦住。
 */
function rowProblem(row) {
  if (!row) return null
  if (!row.subject_id) return 'subj'
  if (badSubject(row)) return 'nonleaf'
  if (!num(row.amount)) return 'amt'
  return null
}
function rowProblemText(row, at = form.entries.indexOf(row) + 1) {
  const p = rowProblem(row)
  if (p === 'subj') return `第 ${at} 行还差会计科目，先把它写完再往下写`
  if (p === 'amt') return `第 ${at} 行还差金额，先把它写完再往下写`
  if (p === 'nonleaf') {
    const sub = subjectOf(row.subject_id)
    return `第 ${at} 行挂的是「${sub.code} ${sub.name}」，不是末级科目，请改到末级科目`
  }
  return ''
}
/** 页脚那一行提示用的：所有挂在非末级科目上的分录（不管是谁挂上去的） */
const nonLeafRows = computed(() => form.entries.filter((e) => badSubject(e) && num(e.amount)))
/**
 * **这一页新挂上去**的非末级科目分录。只有这些才当场拦下来。
 *
 *  原来拿 `is_leaf` 当硬闸，一视同仁地拦住所有非末级分录。而 `is_leaf` 是
 *  服务端按「有没有下级」算出来的**派生值**（ARCHITECTURE §9.6），前端手里
 *  那份科目树只是快照 —— 于是一张老凭证（当年挂 1002，后来 1002 底下加了子科目）
 *  连**只改一下附单据数**都存不下去：页脚红着「第 1 行不是末级科目」，
 *  而人根本没有碰那一条分录。
 *
 *  分界在这里：**账上原来就有的**（`form.legacySubjects` 是后端返回的这张凭证
 *  已经用掉的科目 id）不归前端管 —— 它是服务端的事，服务端自己会拒，
 *  而且它拒的话写得更准（点名是哪几个科目）。**这一页刚挂上去的**才是当场
 *  的笔误，拦下来当场改比等一趟 400 回来强。
 */
const newNonLeafRows = computed(() =>
  nonLeafRows.value.filter((e) => !form.legacySubjects.includes(e.subject_id)))

// 这张凭证自己的会计期间。顶栏那个期间是「现在正在记哪个月」，
// 打开一张旧凭证时两者根本不是一回事，页眉写错期间很容易误导人。
const voucherPeriod = ref(null)

const form = reactive({
  voucher_no: '', voucher_date: '', status: 'UNPOSTED',
  attachments: 0, remark: '', entries: [],
  // 这张凭证**从服务端带回来**时用过的科目 id：分录是新挂的还是账上原有的，
  // 就靠它分家（见 newNonLeafRows）。它不进 entrySig —— 改它不算「这张凭证变了」。
  legacySubjects: []
})

/** 顶栏那个期间结没结账 —— 只管「能不能**开新**凭证」。
 *  新凭证落在哪一期由顶栏说了算，所以这道闸跟着顶栏走。 */
const periodLocked = computed(() => !!store.currentPeriod?.locked)
/**
 * 这张凭证能不能改：**按它自己的期间判，不是顶栏那个**。
 *
 *  原来直接读 `store.currentPeriod?.locked`：顶栏停在未结账的 12 月，
 *  翻到一张 11 月（已结账）的凭证，表单整张可编辑 —— 改一格就触发自动保存的 PUT，
 *  后端那道 `fn_voucher_guard` 照单全收地拒掉，人每改一次弹一次错，
 * 而界面上从头到尾没有一个字说这张不能改。
 *
 *  取的是**它自己的**那个期间（`voucherPeriodId` 直接来自接口，不从
 * `store.periods` 反查 —— 那个数组是异步灌的，页面可能比它先到）。
 * 新凭证还没落库，期间按保存时顶栏那一期走，这时退回顶栏。
 * 认不出（自己的期间还没拿到）也退回顶栏，跟原来的行为一致 ——
 * 宁可少锁一次（后端还会拒），也不能把能改的一张锁死在这儿。
 */
const locked = computed(() => {
  if (!voucherId.value) return periodLocked.value
  const own = store.periods.find((p) => p.id === voucherPeriodId.value) || voucherPeriod.value
  return !!(own ? own.locked : store.currentPeriod?.locked)
})
const posted = computed(() => form.status === 'POSTED')
/**
 * 只读：**没这个账套的写权限**，或这张凭证所在的期间已结账，或它本身已记账。
 *
 *  原来只认后两条，漏了 `canWrite`：VIEWER 的操作列虽然在列表里藏了，
 *  但双击任意一行就能进这一页，整张表可填 —— 改一格触发一次自动保存、
 *  换回一个 403。页面上没有任何一处提示这里不该写。
 *  `canWrite` 用的是 store 那份白名单（认不出的角色一律当不能写），
 *  与后端的 ROLES 是同一份名单，不在前端另立一套标准。
 */
const readonly = computed(() => locked.value || posted.value || !store.canWrite)
// 只读账套不行，**顶栏那个期间**已结账也不行（新凭证落在顶栏那一期）
const canAdd = computed(() => store.canWrite && !periodLocked.value)

/** 科目按 id 建的索引：`subjectOf` 是**每一行都要问好几次**的（标红、余额、
 *  页脚的提示各一次），拿 find 在两千条里线性翻，一屏几百行就是几百万次
 *  响应式代理读取 —— 这条跟上面的 firstProblemAt 是同一个病的两处。
 *  索引只在科目表换掉时重算一次。 */
const subjectById = computed(() => new Map(subjects.value.map((s) => [s.id, s])))
const subjectOf = (id) => subjectById.value.get(id) || null
/** 这一行挂的不是末级科目：科目格标红、保存拦下、回车不许往下走 */
const badSubject = (row) => {
  const sub = subjectOf(row?.subject_id)
  return !!sub && sub.is_leaf === false
}

/** 在凭证里当场加一个科目：加完自动选中，接着就能填金额 */
const subjectCreateOpen = ref(false)
const subjectCreateRow = ref(0)
/** 人刚在格子里敲的那串字（填了查不到的编码再点添加）。带进弹窗去自动铺，
 *  不用人把同一串号在两个地方各敲一遍。 */
const subjectPreset = ref('')
function openSubjectCreate(i, kw) {
  // 科目格本身在只读时是关着的，这里再挡一道：新增科目是个**写**动作，
  // VIEWER 走到这一步只会被服务端 403 拒一次，白等一趟。
  if (readonly.value) return
  subjectCreateRow.value = i
  subjectPreset.value = String(kw || '').trim()
  subjectCreateOpen.value = true
}
async function onSubjectCreated(s) {
  await loadSubjects(subjectYear.value)
  form.entries[subjectCreateRow.value].subject_id = s.id
  activeRow.value = form.entries[subjectCreateRow.value]
}

/**
 * 这一页用的是**哪一年的科目表**。
 *
 * 科目表每个会计年度各有一份（后端迁移 0032），而一张凭证的分录只能挂在
 * **它自己那一期所属年度**的科目上（后端 `accounting/vouchers._validate_entries`
 * 按 `Subject.year == period.year` 校验）。所以这一页要看的不是顶栏那一年，
 * 而是**这张凭证自己那一年**：拿顶栏那年的科目表去认往年凭证，往年的
 * subject_id 在那张表里根本不存在，格子里挂的科目就全是空的
 * （2026-10-22 用户报：搜关键词搜到以前年度的凭证，点开看不到科目）。
 *
 * 新凭证还没落库，期间按保存时顶栏那一期走 —— 这时用顶栏那一年。
 */
const subjectYear = ref(0)
/**
 * 科目表请求的代次。
 *
 * 科目表现在**跟着凭证走**了（不同年度是两张不同的表），于是「翻凭证翻得快」
 * 时两趟取表请求会交错：先发的那一趟后到，会把**上一张**那张表换回来 ——
 * 屏幕上这张凭证配着上一张的科目表，科目格又是空的（跟没修一样）。
 *
 * 与 `load()` 的代次守卫同一个道理：响应回来时先认一认自己是不是最新那趟，
 * 不是就丢弃，一个状态都不碰。
 */
let subjectSeq = 0

async function loadSubjects(year) {
  // **年度必带**：科目表每个年度各有一份（后端迁移 0032）——
  // 2026 与 2027 各有自己的科目表，凭证面板要显示**这张凭证所属的那一年**那张。
  const y = Number(year) || subjectYear.value || store.currentYear
  if (!y) return
  const seq = ++subjectSeq
  // include_inactive=true：老凭证上挂的那个科目可能已经停用，
  // 不返回的话格子里就空了（后端 list_subjects 的说明也是这么写的）。
  const tree = await subjectApi.subjectTree(
    store.bookId, { year: y, include_inactive: true })
  // 过期的那一趟：不碰任何状态（见上面 subjectSeq 的说明）
  if (seq !== subjectSeq) return
  subjectYear.value = y
  subjectTree.value = tree
  // 摊平成一条条，但**父子关系要留着**：凭证面板靠 parent_id 把科目摆成一棵树，
  // 一级一级点开，最后只让末级能选。少了 parent_id 就只剩一张平表，
  // 「为什么 1002 不能选」这个问题在界面上就再也说不清了
  const flat = []
  const walk = (nodes, parent = null) => nodes.forEach((n) => {
    flat.push({
      id: n.id, code: n.code, name: n.name, category: n.category,
      // direction 是科目表里那一列「余额方向」，科目格余额的正负就是按它定的
      direction: n.direction,
      level: n.level, parent_id: parent, is_leaf: n.is_leaf,
      // 停用科目照样**显示**（老凭证指着它），但不能再被新选中 ——
      // 服务端建凭证时会拒（`Subject.active` 为假），让人在界面上就选不到。
      active: n.active !== false
    })
    if (n.children?.length) walk(n.children, n.id)
  })
  walk(tree, null)
  subjects.value = flat
}

// ---- 科目余额：每行科目格左下角那个数字 ----
// 辅助记账时看着余额填金额，用不着跳去科目余额表。
const balances = ref({})
/** 这张凭证自己的期间 id。直接取接口给的 period_id，不去 store.periods 里反查。 */
const voucherPeriodId = ref(0)

/**
 * 算余额取哪一期。
 *
 * 老凭证用它自己那张的：翻一张 2026-12 的老凭证，顶栏可能停在 2027-01，
 * 页眉写的是凭证自己的期间，余额却按顶栏那期算，两处对不上。
 * 新凭证以顶栏为准（保存时也是按顶栏那期落的）。
 *
 * 新凭证这一支尤其不能靠 store.periods 反查：那个数组是异步灌的，
 * 页面完全可能比它先加载完 —— 那一刻 currentPeriod 还是空的，余额就一直拉不出来。
 * 底下那个 watch 接的就是这一下：periodId 从 0 变成真值时再拉一次。
 */
const balancePeriodId = computed(() => voucherPeriodId.value || store.currentPeriod?.id || 0)

let balanceSeq = 0
/**
 * 拉「这张凭证**之前**」的科目余额 —— **时点余额**。
 *
 * 后端按这张在期间里的位置算基数：一张 100、一张 300、一张 500，
 * 月中那张拿到的是 100（它前面那张的数），不是 900（月底的账）。
 * 位置不明时（一张普通的新凭证，落在本期末尾）后端给的是整期期末 ——
 * 那正好是它前面的账。
 *
 * 这张自己的分录由前端按行补回去（见 balanceText），后端给的基数里没有它 ——
 * 它 0.7 秒后就自动保存，落了库还留在账上，前后端各补一次就翻倍了。
 *
 * 好处顺带白拿：基数只在这张/这期换的时候拉一次，录入过程中一个数都不发，
 * 格子里那个数是本机算出来的，没有「等一下才动」。
 *
 * 代次守卫跟 load() 一个道理：翻凭证翻得快时先发的那一趟可能后到，
 * 晚到的那一趟才配写进 balances.value。
 */
async function loadBalances() {
  const seq = ++balanceSeq
  const periodId = balancePeriodId.value
  if (!store.bookId || !periodId) { balances.value = {}; return }
  // 一张**还没落库**、插在某张中间的新凭证：它的位置由参照凭证定
  // （插在后面往后挪一号，插在前面占参照的号）——
  // 跟保存时用的是同一对参数，见 payload.insert_after / insert_position。
  const ins = (!voucherId.value && insertCtx.value) ? insertCtx.value : null
  try {
    const d = await ledgerApi.voucherBalances(store.bookId, {
      period_id: periodId,
      voucher_id: voucherId.value || undefined,
      insert_after: ins?.anchor_id || undefined,
      insert_position: ins?.position || undefined
    })
    if (seq !== balanceSeq) return
    balances.value = d.balances || {}
  } catch {
    if (seq !== balanceSeq) return
    balances.value = {}   // 拉不到就不显示，绝不拿旧数字挡住这一格
  }
}

// 期间或凭证换了就重拉。voucherId 也在盯着：前一张/后一张翻的是不同凭证，
// 各自要算的是**自己那个位置**的账 —— 同一期里翻 balancePeriodId 不会变，
// 光盯它就翻不动了。
// insertCtx 也在盯着：一张插在中间的新凭证，参照凭证换了位置就跟着变
// （它前面是谁变了，基数就跟着变）。
watch([balancePeriodId, voucherId, insertCtx], loadBalances, { immediate: true })

// ---- 科目余额段开始（tests/voucher_running_balance.test.mjs 从这里头抠实现跑）----
/**
 * 每行金额的「已提交值」：敲字的过程中余额按它算，光标离开这一格才换成新值。
 *
 * 存的是这一行**定下来的样子**（方向 + 金额），不是光秃秃的金额 ——
 * 方向也是值的一部分：借 100 与贷 100 对余额的影响正相反。
 *
 * 用**分录对象**当键而不是行号：插入、删除、翻凭证都在动行号，
 * 按行号记的值会跟着行搬家，余额就收错了行（与 markingRows 同一个道理）。
 *
 * 为什么要这一层：那个余额是**照着填**的数 —— 敲一个 1 余额就跳一下，
 * 人还没填完就被半截数牵着走（用户 2026-10-25：「我希望的是，我输入完 100，
 * 然后光标离开这个金额格，才更新余额，也就是余额一步到位，从 300 到 400」）。
 */
const committedAmounts = ref(new Map())

/** 这一行已提交的「样子」。没提交过 = 这一行还没填（金额 0）。 */
function committedOf(row) {
  return committedAmounts.value.get(row) || { direction: row?.direction || 'DEBIT', amount: '' }
}

/** 把这一行当前的样子**定下来** —— 光标离开金额格时调（见 AmountGrid 的 commit 事件）。 */
function commitAmount(row) {
  if (!row) return
  committedAmounts.value.set(row, { direction: row.direction, amount: row.amount ?? '' })
}

/**
 * 把一批分录整批定下来（刚打开一张凭证时用）。
 *
 * 存好的凭证不是「正在敲」的状态 —— 每一行都是定下来的，
 * 所以余额一打开就得是对的，而不是等人去碰某一格。
 */
function seedCommitted(entries) {
  committedAmounts.value.clear()
  for (const e of entries) {
    committedAmounts.value.set(e, { direction: e.direction, amount: e.amount ?? '' })
  }
}

/**
 * 本张凭证里，每个科目**累计到该行为止**的净变动（借正贷负）。
 *
 * 辅助记账要看的是「这一行记进去之后还剩多少」，所以每一行显示的是**滚动**的数：
 * 同一张凭证里同一个科目出现三次借 100，三行就分别显示 100 / 200 / 300
 * （用户 2026-10-25：「每一笔分录之间时序的要能影响…的计算」）。
 * 以前这里是「整张合计」，三行都显示 300 —— 那等于把整张当成一笔，
 * 而录到第三行的人需要知道的是「走到第三行还剩多少」。
 *
 * 金额取的是**已提交值**（committedAmounts），不是正在敲的那串 ——
 * 敲字的过程中余额不动，离开这一格才动（见 commitAmount）。
 *
 * 算一次全表，不在模板里逐行遍历 —— 每次敲键重算一遍 O(行数) 就够，
 * 摊到每行各算一次是 O(行数²)。
 *
 * **单位是整数分（BigInt）**：这一段以前是 `Number(e.amount)` 浮点累加，
 * 与同一页的 centsOf / voucherTotals / autoBalanceNeed 全都走的整数分口径
 * 打架 —— 18 位金额会在这一处差出 0.03 元。金额只在这里定点化一次，
 * 之后全是整数加减。
 */
const runningDelta = computed(() => {
  const acc = new Map()      // subject_id → 累计到当前行为止的净变动
  const out = []             // 下标 → 那一行显示的累计（与 form.entries 同序）
  for (const e of form.entries) {
    const sid = e.subject_id
    if (!sid) { out.push(0n); continue }
    const c = committedOf(e)
    const cents = centsOf(c.amount)        // 整数分（BigInt），与合计那一段同一份定点化
    if (cents) {
      acc.set(sid, (acc.get(sid) || 0n) + (c.direction === 'DEBIT' ? cents : -cents))
    }
    out.push(acc.get(sid) || 0n)
  }
  return out
})

/**
 * 哪几行正在「标记科目」。
 *
 *  用**分录对象**当键而不是行号：插入、删除、翻凭证都在动行号，
 *  按行号记的标记会跟着行搬家，余额就收错了行。翻凭证时整套换掉，这里也整套清掉。
 */
const markingRows = ref(new Set())
function onMarking(row, on) {
  const next = new Set(markingRows.value)
  if (on) next.add(row); else next.delete(row)
  markingRows.value = next
}

/**
 * 这一行显示的余额文本：纯数字，不带「借/贷」也不带「余额」两个字。
 *
 * 显示的是**时点余额**：这张凭证记进去、到这一行为止，账上还剩多少。
 *
 * 正负按科目表那一列「余额方向」定，不是按实际落在借方还是贷方：
 * 借属性借正贷负、贷属性借负贷正（见 utils/balanceSign.js）。
 * 0 显示成空 —— 一个空科目没有余额可言，写个「0.00」只会在每张凭证上多几行噪音。
 * 千分位由 moneyText 统一带，跟页脚合计、科目余额表是同一套写法。
 */
const balanceText = (i, row) => {
  if (readonly.value) return ''          // 只读凭证是给人核对和打印的，不是给人录入的
  // 人正在这一格里敲科目：角上那个数是上一个科目的余额，
  // 摆在还没对上号的字旁边，人会照着它填金额。先收起来，选定了再给
  if (markingRows.value.has(row)) return ''
  const sid = row?.subject_id
  if (!sid) return ''
  // balances 是「这张之前账上已有的」，runningDelta 是「本张累计到本行为止的」，
  // 两个加起来才是「这一行记进去之后的余额」。加了本张的分录，
  // 一个空科目也会被自己这笔钱点亮 —— 600101 原本余额是 0，
  // 借方填了 3000 万就该显示出来，而不是空着。
  //
  // **全程整数分**：
  //   · 账上余额（后端给的是 number/float）先定点成整数分 —— 只在这里转换一次，
  //     转换之后就不再有浮点；
  //   · runningDelta 已经是整数分（见上面那一段），两段相加还是整数分；
  //   · 翻正负在整数分上做（displayBalanceCents），显示走 moneyText（整数分 → 文本）。
  // 以前这里是 `num(账上余额) + ownDelta` —— 两边都是浮点，
  // 18 位金额会差出 0.03 元，而这一格正是辅助记账时人照着填数的那个数。
  //
  // 借贷先按引擎口径（借正贷负）加总，**加完再按科目余额方向翻正负**：
  // 贷属性的科目（负债/权益/收入）借方有余额时显示负数，一眼看出录反了方向。
  // 翻只在这一处发生，别在 runningDelta 里提前翻 —— 那样基数和增量两套符号会打架。
  const sub = subjectOf(sid)
  const base = centsOf(balances.value[sid])          // 账上余额 → 整数分
  const delta = runningDelta.value[i] ?? 0n           // 本张累计到本行为止（整数分）
  const shown = displayBalanceCents(sub?.direction, base + delta)
  if (!shown) return ''
  return moneyText(shown)
}
// ---- 科目余额段结束 ----

// ---- 借方/贷方是两个独立格子，数据层还是一个 direction + amount ----
// 往借方填数就是借方，往贷方填数就是贷方；清空则只清自己那一侧。
const debitOf = (row) => (row.direction === 'DEBIT' ? row.amount : '')
const creditOf = (row) => (row.direction === 'CREDIT' ? row.amount : '')
function setDebit(row, v) {
  if (v === '' || v === null || v === undefined) {
    if (row.direction === 'DEBIT') row.amount = ''
    return
  }
  row.direction = 'DEBIT'
  row.amount = v
}
function setCredit(row, v) {
  if (v === '' || v === null || v === undefined) {
    if (row.direction === 'CREDIT') row.amount = ''
    return
  }
  row.direction = 'CREDIT'
  row.amount = v
}
/* 合计以前走 money2（浮点 toFixed(2)），现在走整数分直接落成字符串（见下面的 total）：
 * 浮点在 14 位以上就会把金额改掉，而合计与分录必须是**同一个数**。 */
// ---- 金额：一律整数分（判平、差额、补差三处共用这一份量化结果）----
//
// 这一段（从本标记到「金额段结束」）是**自足**的：取值、折分、合计、判平、
// 差额、补差全在这里，公共依赖一个都不外借。
// tests/voucher_balance.test.mjs 就是从这里头把实现抠出来跑的 —— 所以这一段里
// 只能用 `computed` / `form` / `moneyText` / `moneyCents` / `centsToText`，
// 引用本段之外的任何东西都会让抠出来的实现断掉。

/** 金额取数。只敲了一个负号的时候 ``Number('-')`` 是 NaN，
 *  NaN 一进合计，整张凭证的差额就变成 NaN，连「不平」都说不出来了。 */
const num = (v) => {
  if (v === '' || v === null || v === undefined) return 0
  const n = Number(v)
  return Number.isFinite(n) ? n : 0
}
//
//  原来是 JS number 累加 + 严格相等判平，而 `100.10 + 200.20 = 300.29999999999995`
//  不等于 `300.3` —— 于是「借 100.10 + 借 200.20 / 贷 300.30」这种完全合法的凭证
//  被判成不平，两道闸门（手动保存、自动保存）都只有 `balanced` 一个口子，
//  于是它**永远存不进去**。枚举下来两借一贷约 22%、三借一贷约 27% 中招。
//  更糟的是页脚那句 `fmt(Math.abs(借 − 贷))` 把残差抹成「贷方大 0.00」，
//  人对着一个 0.00 差额根本看不出自己错在哪。
//
//  现在按**分**这个最小单位比：金额只在这里定点化一次，之后全是整数加减。
//  后端是 Decimal 逐笔累加、一分不差，前端也一分不差，两边才是一套口径 ——
//  不再是「前端拿容差凑合、后端用精确值」这种永远对不齐的双口径。

/**
 * 一个金额折成**整数分**。全程字符串定点，不经过浮点累加。
 *
 *  · `''` / `'-'` / 认不出来的字符 → 0：只敲了一个负号时 `Number('-')` 是 NaN，
 *    NaN 一进合计，整张凭证的差额就废了，连「不平」都说不出来。
 *  · 小数只取到分：`sanitizeAmount` 敲进来时就截到两位，金额列也是两位小数，
 *    再往后的位次从来不会到这里 —— 静默截断比悄悄四舍五入诚实。
 *  · 负号认半角 `-` 和落格用的真正减号 `−`（U+2212）：两种都得是红字，
 *    把其中一个当成正数，冲销就成了再记一遍，方向刚好反。
 */
function centsOf(v) {
  // 定点折成分，**BigInt**（与期初页同一份 moneyCents）。
  //
  // 以前这里是 Number：整数分越过 2^53 之后 `Number.isSafeInteger` 判负，
  // 于是**这笔钱被当成 0** —— 存库那一步 `centsText(centsOf(e.amount))`
  // 就把一个 14 位以上的金额写成了 "0.00"（金额格以前把输入截到 9 位，
  // 所以够不着；金额格不再截断之后够得着了，这条路必须一起修）。
  return moneyCents(v)
}

/** 整数分落成字符串（不带千分位）。补差写回格子、发给后端都走它 ——
 *  所以页面上看见的数、判平用的数、后端收到的数，是同一个数。 */
function centsText(c) {
  return centsToText(c)
}

/**
 * 凭证两栏的合计：**按列求和，负数照减**，在整数分上做。
 *
 *      借方合计 = Σ 借方分录的金额（红字照减）
 *      贷方合计 = Σ 贷方分录的金额（红字照减）
 *
 *  也就是「把这一列显示的数字加起来」—— 合计行摆的数跟上面几行摆的数是同一套。
 *  （用户 2026-10-24：一张借 −333 / 贷 2225 的凭证，合计该是「借 −333 / 贷 2225」，
 *  而不是把 −333 搬到对面去摆成「借 0 / 贷 2558」。）
 *
 *  以前这里用的是「红字算到对面去」（负数搬到对面，两栏只摆正数）。那条口径
 *  在**判平**上没错（`dr_side - cr_side` 与 `dr_signed - cr_signed` 恒等），
 *  但摆出来的数跟账上的数不是一回事：
 *      · 数据库的借贷平衡触发器 —— 按列求和；
 *      · 账套引擎（总账 / 明细账的发生额）—— 按列求和；
 *      · 凭证导入的合计 —— 按列求和；
 *      · 后端 ``_validate_entries`` 的判平 —— 按列求和。
 *  只有「摆给人看的合计」是另一套，于是纸上那一行跟账上对不上：
 *  借方合计摆 0，账上借方发生额却是 −333。合计只有一个口径，就是账上的那个。
 *
 *  **判平、差额、补差、合计，全都用这一份**（带符号，负数照减）：
 *  借方合计 = 贷方合计 ⟺ 带符号借方 = 带符号贷方（与数据库触发器同解）。
 */
function voucherTotals(entries) {
  let debit = 0n, credit = 0n
  for (const e of entries || []) {
    const c = centsOf(e.amount)
    if (e.direction === 'DEBIT') debit += c
    else credit += c
  }
  return { debit, credit }
}

/** 「=」补差：在整数分上算「这一格填多少，整张才平」。 */
function autoBalanceNeed({ opposite, same, mine, mineSide, side }) {
  return opposite - same + (mineSide === side ? mine : -mine)
}

/**
 * **能落库的分录** —— 显示口径与提交口径共用同一份。
 *
 * 判据：**有科目**（subject_id）且**金额不为 0**。
 *
 * 这一份是「页面上算出来的」与「发出去给后端的」之间**唯一**的桥：
 * 合计、判平、差额、合计行、自动保存闸门、以及真正提交的那份 entries，
 * 全部取自这里。
 *
 * 以前显示口径遍历**全部**行、提交口径过滤「有科目且金额非零」的行 ——
 * 两个集合不同，于是出现过一个很难解释的现象（审计 §3.23 实测）：
 * 第 2 行只填金额、没选科目时，页脚显示「借贷平衡 ✓」、合计行显示
 * 「借 100.00 / 贷 100.00」，自动保存也放行；可提交时那一行被**静默丢掉**，
 * 后端收到的是「借 100 / 贷 0」，当场回一句「借贷不平衡」。
 * 用户看到的是「界面说平、后端说不平」，而那 100 元在屏幕上存在、在库里不存在。
 *
 * 现在两边取同一份集合：**页面上算平了，提交出去就一定是平的**。
 *
 * 「金额不为 0」用 `centsOf` 判（整数分），**不用 `num`**：
 * `num` 只认半角负号，落格用的真正减号 U+2212 在它那里是 NaN→0，
 * 而 `centsOf` 认得（红字凭证靠它）。用 `num` 判的话，一张 U+2212 的
 * 红字分录会被当成「没填金额」而**整个不见**。
 * 关键是：`centsText(centsOf(...))` 正是**发出去的那个金额** ——
 * 判据和载荷用同一个算法，才谈得上「页面上算平了，提交出去就是平的」。
 */
const payableEntries = computed(() =>
  form.entries.filter((e) => e.subject_id && centsOf(e.amount) !== 0n))

/**
 * **有金额、却没选科目**的行 —— 它们**不会被提交**（见上面那份集合）。
 *
 * 这一类行以前是**静默丢弃**的：页面上明明有一行数字，提交时它没了，
 * 而后端只说「借贷不平衡」，一句都没提「你有一行没选科目」。
 * 现在它有自己的名字：页脚明说第几行还差科目，保存时也明说。
 */
const rowsMissingSubject = computed(() =>
  form.entries.filter((e) => !e.subject_id && centsOf(e.amount) !== 0n))

/** 判平、差额、合计三处共用的那一份量化结果（单位：分）。 */
const totals = computed(() => voucherTotals(payableEntries.value))
/**
 * 显示用的合计：格子里落的那两行。**单位是分**（与 totals 同源，不再自己除一次 100）——
 * 除以 100 走浮点的话，18 位整数会在这一步掉精度。
 *
 * 一栏摆不摆数，看的是**这一栏有没有录过数**，不是「这一栏合计是不是 0」：
 * 同侧相抵的凭证（借 1000 / 借 −1000）借方那一栏合计是 0，可它明明录了数 ——
 * 按合计判断会把「借 0.00」藏掉，合计那一行看着像没录过
 * （2026-10-12 那条「正负相抵按平处理」的要求，说的就是这种凭证）。
 * 一栏从头到尾没数（没填金额、或者压根没有这一栏的分录）→ 留空：
 * 空格子比一个孤零零的 0 干净。
 *
 * 「录过数」也按**能落库的那一份**算：只有金额没科目的行不进合计 ——
 * 它提交时会被丢掉，摆在合计里就是一个提交后不存在的数（审计 §3.23）。
 */
function columnTotal(direction) {
  const sum = direction === 'DEBIT' ? totals.value.debit : totals.value.credit
  const hasNumber = payableEntries.value.some((e) => e.direction === direction && centsOf(e.amount) !== 0n)
  return hasNumber ? centsText(sum) : ''
}
const total = computed(() => ({
  debit: columnTotal('DEBIT'),
  credit: columnTotal('CREDIT')
}))
/** 差额（分）。借方大就是正的 —— 方向和大小在同一个数上，不用分两处判断。 */
const diffCents = computed(() => totals.value.debit - totals.value.credit)
/**
 * 平不平：**借方合计 = 贷方合计，就是平的**，在整数分上完全相等。
 *
 * 这里没有容差，也不该有：差一分就是不平（后端 Decimal 也是这么判的），
 * 而 `5e-15` 那种浮点残差在 `centsOf` 那一步就已经不存在了 ——
 * 加容差只会把「真差一分钱」也一起放过。
 *
 * 两种看着「不像平」的形状都是平的，别去替人把它们判成不平：
 *  红字凭证（借 −300 / 贷 −300）冲的是老账，照样该存；
 *  正负相抵（借 111 / 借 −111）记的是一次冲销 —— 两栏各自抵成 0，一样大，
 *  就该按平处理（2026-10-12 用户明确要求的）。
 *
 * 判平就是「按列求和的两栏相等」—— 与数据库触发器、后端 `_validate_entries`
 * 同一个口径（以前是「红字算到对面」那套展示口径，两者同解，但纸上摆的数不一样）。
 *
 *  一条都还没录是另一回事：那走 `blank`，页脚说「还没录金额」，不是「平」。
 */
const balanced = computed(() => diffCents.value === 0n)
/**
 * 差额要说清是**哪一边大**。
 *
 *  只写「差额 90.00」，90 这个数看着像凭空冒出来的；其实两栏摆在那儿，
 * 一眼比得出大小。不说方向，人还得自己回头数一遍是借多还是贷多，
 * 才知道该往哪一格补 —— 而这恰恰是页脚这句话唯一该干的事。
 *
 *  金额从整数分直接落成字符串：不平的时候至少差一分，
 * 所以这里**不可能再出现「贷方大 0.00」**那种看不懂的话。
 *
 * **页脚是文本，合计格是金额格 —— 两者写法不一样是刻意的，别去「统一」。**
 *   · 合计格（AmountCells）是**逐位落格**的：亿…元九格 + 角 + 分，
 *     每一位单独占一格，为的是跟打印纸上的金额格对齐（打印出来是同一张纸）；
 *     所以它不带千分位 —— 格子是定宽的，塞不下千分位逗号。
 *   · 页脚这一句是**一句人话**，带千分位（"借方大 3,003,000.00"），
 *     它跟科目余额表、期初页的合计栏用的是同一份 moneyText。
 * 强行把两边改成一样，只会让其中一个失去它存在的理由。
 */
const diffTag = computed(() => `${diffCents.value > 0n ? '借方大' : '贷方大'} ${
  moneyText(diffCents.value < 0n ? -diffCents.value : diffCents.value)}`)

// 一分钱都还没录的时候别急着报红字，那是噪音不是提示
//
// 「没录」看的是**有没有录过数**，不是「两栏合计是不是 0」：
// 同侧相抵的凭证（借 111 / 借 −111）两栏合计都是 0，可它明明录了数 ——
// 按合计判断会把它说成「还没录金额」，人一看就知道程序没看懂。
// （2026-10-12 那条「正负相抵按平处理」的要求，说的就是这种凭证要按平处理。）
const blank = computed(() => !form.entries.some((e) => centsOf(e.amount) !== 0n))
// ---- 金额段结束（tests/voucher_balance.test.mjs 从这里头抠实现跑）----

// 「这张凭证现在长什么样」：自动保存靠它判断有没有改动。没动过就不重存。
//
// **凭证号不进这个签名**：它不是人改的（页面上只读，用户 2026-10-19 定的），
// 而是服务端发的 —— 改了日期跨到另一个期间，服务端会**按目标期间重新发一个号**
// （该期现有最大号 + 1）。号一变签名就变，等于凭空多一趟自动保存；
// 更要命的是：号是**跟着服务端**走的，签名里带着它就会把「服务端改的号」
// 误判成「人改了这张凭证」。
const entrySig = computed(() => JSON.stringify([
  form.voucher_date, form.attachments, form.remark,
  form.entries.map((e) => [e.subject_id, e.direction, e.amount, e.summary])
]))
const lastSaved = ref('')      // 上一次落库的签名
const savedAt = ref('')        // 上一次落库的时间，页脚上给个准信
/** 存失败的那一版：同一版不再自动重试。
 *
 *  不记的话，一次失败（老凭证挂着后来被加了子科目的分录、服务端 400）
 *  会变成「每敲一下键弹一次同样的错」—— 报错反而把页面变成噪声，
 *  人连自己在改什么都看不清了。 */
const failedSig = ref('')

/**
 * 新建凭证的**幂等键**：这一张凭证从开始填到落库，一直是同一个键。
 *
 * 为什么需要它（审计 §3.28）：POST 发出 → 服务端**已入库** → 响应在网络上
 * 丢了（30 秒超时）→ 用户以为没存上，再敲一个字 → 自动保存又 POST 一次 →
 * 库里两张内容几乎相同的凭证。
 *
 * 键必须**跟着这一张凭证**，不能跟着「这一版内容」：内容一变签名就变，
 * 而要防的恰恰是「内容变了一点之后重发」。所以它在**第一次创建**时生成，
 * 直到这张落库为止都不换；换一张凭证（新开、翻页、换账套）才换新的。
 *
 * 服务端按这把键去重：同一把键只创建一张凭证，重复的请求落到**同一张**上，
 * 不会在库里多出一张几乎一样的。
 */
const createKey = ref('')

/**
 * 这张凭证的**乐观锁令牌**（服务端给的 ``updated_at`` ISO 文本）。
 *
 * 它是「我读到的是哪一版」的凭据：保存时原样送回去，服务端拿库里此刻的
 * 那一版比一下 —— 不一致就是「你改的是别人已经改过的那一版」。
 *
 * **没有它的时候，正确性靠服务端的行锁兜着**（那是主体）；
 * 它是**第二道**：把人能看懂的那种冲突（「已被他人改动，请刷新」）
 * 提前说清楚，而不是让后保存的人默默覆盖前一个人的改动。
 *
 * 每次读/写回来都换成服务端给的新值 —— 令牌是一次性的，
 * 用旧的下一次就会被自己判成「版本过期」。
 */
const voucherVersion = ref('')

/**
 * **版本冲突**：服务端回过 409（这一版的令牌作废了）。
 *
 * 掐掉它是为了不让自动保存一路重试一路弹红字 —— 人每敲一下就重试一次，
 * 结果一条也没存上，屏幕还被红字盖满。出路是刷新页面。
 */
const versionConflict = ref(false)

/** 生成一把幂等键。优先用 crypto.randomUUID，拿不到就退到时间戳+随机数。 */
function newIdempotencyKey() {
  const rand = globalThis.crypto?.randomUUID
    ? globalThis.crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`
  return `vch-${rand}`
}

/** 这一页现在有没有「能落库的分录」：至少一条有科目、金额不为 0。
 *
 *  自动保存、离开前 flush、关页签拦问，问的都是同一句话，
 *  判据也就只该有这一处。早先这三处各写一遍，其中 watch 那处漏了「有分录吗」，
 *  只看 ``balanced`` —— 而空表时借贷都是 0、``diffCents === 0``，
 *  ``balanced`` 就是 ``true``，于是新凭证页填好号码日期、停手 0.7 秒就
 *  弹一句「请至少填写一条分录」。什么都没填的人被判成保存失败，
 *  而且 ``failedSig`` 记不上它，每敲一下摘要就再弹一次。
 */
const hasCompleteEntry = computed(() => payableEntries.value.length > 0)

function subjectText(id) {
  const s = subjectById.value.get(id)
  return s ? `${s.code} ${s.name}` : ''
}
function onSubject(row) {
  if (!row.summary) {
    const n = form.entries.filter((e) => e.summary).length
    if (n === 0) {
      const first = form.entries.find((e) => e.subject_id)
      if (first) first.summary = ''
    }
  }
}
// 每行的盒子也要拿到实例：回车往下写时要按行收回锚点高度，
// 得能问出「这一行的盒子现在在滚动区里的哪个位置」。
const rowEls = new Map()
/**
 * 键是**行下标**，不是分录对象 —— 与 ``sumRefs`` / ``amtRefs`` 一致。
 *
 * 以前键是分录对象，注释说「值会跟着行搬家」。搬是搬了，可搬得**不是时候**：
 * ``v-for`` 用的是下标 key（``:key="i"``），所以删掉中间一行时 Vue 是
 * **按位置复用 DOM** 的 —— 旧尾节点被卸载时，它那个函数式 ref 会以 ``null``
 * 再调一次，而那个闭包捕获的是**上移后落到该下标的那一行**。
 * 于是「刚写进去的条目」被这条陈旧的 null 调用又删掉了：
 * 那一行从此在 ``rowEls`` 里没有条目，``keepActiveRowAnchored`` 取不到元素
 * 直接 return —— **滚动锚定从此失效**（录到第 20 行时输入位置一路掉到屏幕底下），
 * 同时被删行的条目永久残留在 Map 里（审计 §3.31）。
 *
 * 按下标键就没这个问题：那条陈旧的 null 调用删的是**已经不存在的那个下标**
 * （尾部下标），而新下标上的条目是刚写进去的，不会被误删。
 */
function setRowRef(el, idx) {
  if (el) rowEls.set(idx, el)
  else rowEls.delete(idx)
}

// 每行的格子都要能拿到实例：回车要挨着往下走，得能直接落笔到某一行某一格
const subjectRefs = new Map()
const sumRefs = new Map()
const amtRefs = new Map()
function setSubjectRef(el, idx) {
  if (el) subjectRefs.set(idx, el)
  else subjectRefs.delete(idx)
}
function setSumRef(el, idx) {
  if (el) { sumRefs.set(idx, el); nextTick(() => autosizeSum(el)) }
  else sumRefs.delete(idx)
}

/**
 * 摘要格按内容长高：写满一行折成两行，最多两行，再多就在框里滚。
 *
 *  高度得自己算，因为格子（td-sum）是定高的 68px，框也铺满整格：
 * 框若一直是单行高，长摘要就贴着上边沿被截掉，看不见写了什么。
 * 先把 height 撤成 auto 量出内容真正要多少，再取 min(内容, 两行) ——
 * 少了 min，超长摘要会把框撑到五行、盖住下面那一行；少了 auto，量到的永远是当前高度。
 */
function autosizeSum(el, measured) {
  if (!el) return
  sumArmed = false        // 已经动手改了：整格选中到此为止，别等下一帧又划掉刚打的字
  const lh = parseFloat(getComputedStyle(el).lineHeight) || 21
  const max = lh * 2
  let need = measured
  const prev = el.style.height
  if (need === undefined) {
    el.style.height = 'auto'
    need = el.scrollHeight
  }
  // **量到 0 不是「内容高度是 0」，是「这一格此刻没有布局盒」**
  // （整棵子树 display:none 那一类，scrollHeight 量出来就是 0）。
  // 把 0 写进 height，这一格就永远 0 高 —— 摘要看不见了
  // （用户 2026-10-28 报「摘要空的」「会计科目不显示」，同一个原因）。
  // 这时候把高度放回原样：内容多高就显示多高，等页面有盒子了再重量。
  if (!need) {
    el.style.height = prev
    return
  }
  el.style.height = Math.min(need, max) + 'px'
  el.style.overflowY = need > max ? 'auto' : 'hidden'
}
/** 摘要一改、列宽一变（窗口缩放），折行位置就变了，高度得跟着重量一遍 */
watch(() => form.entries.map((e) => e.summary).join('\u0001'), () => {
  nextTick(() => sumRefs.forEach((el) => autosizeSum(el)))
})
function setAmtRef(el, idx, side) {
  const k = `${idx}:${side}`
  if (el) amtRefs.set(k, el)
  else amtRefs.delete(k)
}

/**
 * 回车往下写时，当前行停在滚动区的哪个高度。
 *
 * 原来根本没有这一层：moveNext() 只把光标交给下一行，页面就那么一直往下顶，
 * 录到第七行空分录行已经掉到屏幕底下，手得伸到页面最下方去写。
 *
 * 停多高？用户 2026-10-19 定的：**第 4 行下方一行**（即第 5 行所在的位置）。
 * 为什么不是正中间 —— 上面留着的三行是刚写完的，下面留着的空行才是接着要写的，
 * 这跟人低头看纸的视线一致；停在正中反而是把写好的和没写的挤到两边。
 *
 * 这个数字不写死成 px：滚动区从第一条分录开始，锚点就是 ANCHOR_ROW × 行高，
 * 行高改了它自己跟着变；表头和合计已经移到滚动区外，不参与这个偏移量。
 */
const ANCHOR_ROW = 4

/** 分录滚动区。表头与合计在它外面固定，只有这里的分录行会滚。 */
const entryScroll = ref(null)

/**
 * 把当前录入行收回锚点高度。
 *
 * 滚的是**视口**，不是数据行 —— form.entries 的顺序一个字节都不动，
 * 序号还是 1..N 递增，已录的那些行照旧留在上面给人回头核对。
 * 所以每按一次回车，视图往上走一行、当前行回到同一个屏幕高度；
 * 录到第 20 行时，录入的位置跟录到第 5 行时一模一样。
 *
 * 瞬时定位，不用 smooth：录入是连续快速的键盘动作，平滑动画会在一次次
 * 回车里排队，越按越滞后于手，反倒跟不上人。
 */
/** 表头与分录区共用的实际内容宽度（滚动条出现时要扣掉它）。 */
function syncGridWidth() {
  const box = entryScroll.value
  const wrap = box?.parentElement
  if (!box || !wrap) return
  const w = box.clientWidth
  // **页面此刻没有布局盒**（整棵子树 display:none 那一类）：量到的是 0，
  // 不是真实宽度。把 0 写下去，表头与合计行就塌成 0 宽 ——
  // 列宽退回各列的最小值，竖线跟分录行再也对不齐（2026-10-28 用户报「表格错位」）。
  // 这时候宁可先不改：CSS 里那个 calc(100% - 100px) 是能用的近似值，
  // 等页面有盒子了，下面的 ResizeObserver 会再量一次准的。
  if (!w) return
  wrap.style.setProperty('--vg-grid-width', `${Math.max(0, w - 100)}px`)
}

function keepActiveRowAnchored() {
  const box = entryScroll.value
  const row = activeRow.value
  if (!box || !row) return
  // 行盒子按**下标**存（见 setRowRef），所以这里先换成下标再取。
  const idx = form.entries.indexOf(row)
  if (idx < 0) return
  const el = rowEls.get(idx)
  if (!el) return
  syncGridWidth()
  // 现在滚动区从第一条分录开始，当前行在内容里的位置不再包含表头高度。
  const rowTop = el.getBoundingClientRect().top - box.getBoundingClientRect().top + box.scrollTop
  const anchorTop = ANCHOR_ROW * rowHeight()
  const want = rowTop - anchorTop
  const max = Math.max(0, box.scrollHeight - box.clientHeight)
  const to = Math.min(Math.max(0, want), max)
  if (Math.abs(box.scrollTop - to) < 1) return   // 已经在锚点上，别做无谓的重排
  box.scrollTop = to
}
/** 行高：量一行分录，量不到用 CSS 里那份 68。 */
let cachedRowH = 0
function rowHeight() {
  if (cachedRowH) return cachedRowH
  const el = entryScroll.value?.querySelector('.vg-row:not(.vg-head):not(.vg-total)')
  cachedRowH = el?.offsetHeight || 68
  return cachedRowH
}

/** 光标落到第 i 行的摘要格：整格选中，接着写就是接着改，不用来回挪光标 */
function focusSum(i) {
  nextTick(() => {
    const el = sumRefs.get(i)
    el?.focus()
    el?.select()
  })
}

/**
 * 整格选中只在**刚进这一格**的那一下有效，跟科目格一个规矩。
 *
 *  摘要多半是同一句话抄好几遍，改的时候常常是整句重打 —— 这时候全选省事。
 *  但偶尔只想改中间几个字（日期、金额、一个词），全选又按不掉、也点不掉的话，
 *  这一格就只能整段覆盖着改。过了进格的那一下，人再点、再按方向键，
 *  都是他自己要挪光标，别再抢。
 */
let sumArmed = false

/** 整格选中（连同「下一帧再补一次」那一下） */
function selectAllSum(el) {
  if (!el?.select) return
  el.select()
  sumArmed = true
  // Chrome 把「光标落到点上的那个位置」排在 click 处理器**之后**才做，
  // 刚选好的整格会被抹成一个光标位。下一帧再选一次，抹掉的正是它。
  requestAnimationFrame(() => {
    if (sumArmed && document.activeElement === el && el.selectionStart === el.selectionEnd) el.select()
    sumArmed = false
  })
}

/** 点哪个格就选中哪个格的字：改内容整句重打，省得先按住退格键一路退到头 */
function selectText(e) {
  const el = e?.target
  if (!el?.select || !sumArmed) return       // 已经是自己点进来的：光标落在点上
  selectAllSum(el)
}

/** 光标进摘要格：把这一句让出来，接着打就是重打 */
function focusSumText(i, e) {
  activeRow.value = form.entries[i]
  selectAllSum(e?.target)
}

/**
 * 金额格里按 ↑ / ↓：落到**相邻行的同一侧**。
 *
 *  上下键在单行输入框里本来什么也干不了（框里没有第二行），
 *  而连续录凭证时人正是一路顺着金额列往下敲 —— 让上下键带着手走到
 *  上一行/下一行的借方（或贷方）格，接着就是选中 + 重打，跟鼠标点过去一样。
 * 到头了就停住：凭空加一行不算「上下移动」。
 */
function moveRow(i, delta, side) {
  const at = i + delta
  if (at < 0 || at >= form.entries.length) return
  if (blocked(at)) return warnBlocked(at)
  const row = form.entries[at]
  activeRow.value = row
  nextTick(() => amtRefs.get(`${at}:${side}`)?.focus?.())
}

/**
 * 金额格的光标走到头，方向键往旁边那一格走 —— 跟 ↑↓ 走相邻行是同一个手势。
 *
 *  横向的口子跟纵向不一样：借方的左边是会计科目，右边是贷方；
 *  贷方的左边是借方，右边已经是表格外头了 —— 到头就停住，
 *  凭空把光标甩到别处去只会让人丢了自己正在记的那一行。
 *
 *  落过去一律走 focus()：键盘进来的光标照旧落到元/角之间那条红线上，
 *  跟 Tab、跟上下键进来是同一个约定，鼠标点过去的那一套不掺和进来。
 */
/**
 * 摘要格的方向键：走到头就跟金额格、科目格一样**换格子**。
 *
 *  整行是「摘要 → 会计科目 → 借方 → 贷方」，手一路往右敲下来，
 *  到摘要末尾再按一下 → 却只能把光标按在原地 —— 明明右边就摆着下一格。
 *  到头才换、不到头照旧在格内挪光标：有选区时也先收选区，跟别处一个规矩。
 *
 *  ← 到头没地方去：左边那一列是序号，不可编辑 —— 停住，别把人甩到别处去。
 */
function onSumKeydown(i, e) {
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
  const el = e.target
  const s = el.selectionStart ?? 0
  if ((el.selectionEnd ?? s) !== s) return        // 有选区：先照默认的收掉
  const edge = e.key === 'ArrowRight' ? el.value.length : 0
  if (s !== edge) return                          // 还在格内：交给默认行为
  if (e.key === 'ArrowLeft') return               // 左边是序号，没有可编辑的格子
  e.preventDefault()
  moveSide(i, 'sum', 1)
}

function moveSide(i, side, dir) {
  if (blocked(i)) return warnBlocked(i)
  activeRow.value = form.entries[i]
  nextTick(() => {
    // 左边：摘要的左边是序号（不可编辑），科目的左边是摘要，借方的左边是科目，
    // 贷方的左边是借方。右边：科目的右边是借方，借方的右边是贷方，
    // 贷方的右边已经是表格外头了 —— 到头就停住。
    if (dir < 0) {
      if (side === 'subj') return sumRefs.get(i)?.focus?.()
      if (side === 'd') return subjectRefs.get(i)?.focusInput?.()
      if (side === 'c') return amtRefs.get(`${i}:d`)?.focus?.()
      return
    }
    if (side === 'sum') return subjectRefs.get(i)?.focusInput?.()
    if (side === 'subj') return amtRefs.get(`${i}:d`)?.focus?.()
    if (side === 'd') return amtRefs.get(`${i}:c`)?.focus?.()
  })
}

/**
 * 在金额格里敲「=」：这一格直接补成**让整张凭证平掉**的那个数。
 *
 * 一行只有一个数：往贷方格写数，这一行就从借方变成贷方，原来那笔借方金额
 * **一起没了**（格子只有一个方向）。所以补多少不能拿「当前贷方合计 + 全部借方」
 * 来算 —— 那样会把这一行自己那笔也算两遍。
 *
 *   写进去的方向跟这一行现在一样 → 新的合计 = 原来的合计 − 这一行 + 新数
 *   方向会翻（借方有数的行往贷方写） → 借方少掉这一行，贷方多出这个数
 *
 * 借 100 + 借 200 + 借 200，在第三行贷方敲「=」补 300：这一行从「借 200」
 * 变成「贷 300」，合计正好是借 300 / 贷 300。补 500 的话这一行会被顶成
 * 贷 500，账上就凭空少了那 200 —— 是这张凭证最常见的一处笔误。
 *
 * 已经平了就不用补，直接说一声；补出来是负数就红字（冲销）。
 */
function autoBalance(i, side) {
  const row = form.entries[i]
  if (!row) return
  // opposite = 写进去这一格的**对面**合计；same = 写进去这一格的**本侧**合计
  // 全是整数分：补完这一格之后判平用的那份量化结果也必然是平的，
  // 不会再出现「已按差额补上 300.30」却依然存不了的情况。
  //
  // 走的就是合计那份（按列求和、负数照减）—— 与判平、与数据库触发器同一个口径。
  const opposite = side === 'd' ? totals.value.credit : totals.value.debit
  const same = side === 'd' ? totals.value.debit : totals.value.credit
  const mine = centsOf(row.amount)
  // 这一行现在的方向。空格也记着方向（默认借方），但金额是 0，翻不翻都一样
  const mineSide = row.direction === 'CREDIT' ? 'c' : 'd'
  const need = autoBalanceNeed({ opposite, same, mine, mineSide, side })

  // 补出来是 0：这一格该空着。空着本来就没错，说一声已经平了；
  // 不空着（前面有一笔同方向的数）就得清掉 —— 留着它反而不平。
  if (need === 0n) {
    if (!mine) return ElMessage.info('这张凭证已经平了，不用补')
    if (side === 'd') setDebit(row, '')
    else setCredit(row, '')
    commitAmount(row)     // 这一格被清空了，也是定下来的数
    return ElMessage.info('这一格得空着，填着反而不平')
  }
  // 已经是这个数：什么都不用做，别再报一遍「已按差额补上 300」
  if (mineSide === side && mine === need) return

  // 负数原样写进去（红字冲销），别在这里被当成垃圾字符剔掉
  const text = centsText(need)
  if (side === 'd') setDebit(row, text)
  else setCredit(row, text)
  // 「=」补出来的是一个**定数**，不是正在敲的半截数 —— 当场定下来，
  // 余额一步到位，不用等光标离开这一格。
  commitAmount(row)
  activeRow.value = row
  // 金额从整数分直接落成文本（moneyText）：中间不经过浮点，
  // 18 位金额不会被 Number 改小（以前这里是 fmt(text) —— 文本 → 浮点 → 文本）。
  ElMessage.success(need < 0n
    ? `已按差额补上 ${moneyText(need)}（红字冲销）`
    : `已按差额补上 ${moneyText(need)}`)
}

/**
 * 回车往哪儿走：一行里「摘要 → 会计科目 → 借方 → 贷方」从左到右，
 * 一行走完落到**下一行的摘要**，接着才是再下一行 —— 像一张纸从上往下写，
 * 而不是第一行写完直接蹦到最后一行去。
 * 这一行没写完（差科目或差金额）就不往下走：中间跳过一条不写完整的分录去写下一行，
 * 存出来是一张谁也看不懂的纸。走到最后一行最后一个格子，才谈得上「加一行」。
 */
function moveNext(i, from) {
  const row = form.entries[i]
  if (!row) return
  activeRow.value = row
  // 落笔都要等这一轮渲染过去：刚选中的科目，那一行的金额格这会儿还挂着 disabled，
  // 这时候 focus() 打在一个禁用的输入框上是打不进去的
  const jump = (fn) => nextTick(fn)
  if (from === 'sum') return jump(() => subjectRefs.get(i)?.focusInput?.())
  if (from === 'subj') return jump(() => amtRefs.get(`${i}:d`)?.focus?.())
  if (from === 'd') return jump(() => amtRefs.get(`${i}:c`)?.focus?.())

  // 这一行到头了。先看它成没成条：没成条就停在原地把话说明白
  const hint = incompleteHint(row)
  if (hint) return ElMessage.warning(hint)
  // 已经是最下面一行 → 试着加一行
  if (i + 1 >= form.entries.length) return addRow(row)

  const nextRow = form.entries[i + 1]
  // 往下一行接着写的多半还是同一件事（凭证还没平），摘要跟着上一行走，
  // 省得每加一行分录都要把同一句话再敲一遍。已经写过的摘要不动。
  if (row.summary && !nextRow.summary && !balanced.value) nextRow.summary = row.summary
  activeRow.value = nextRow
  focusSum(i + 1)
  // 手往下写了一格，就把这一行收回锚点高度：录到第七行、第几行都一样，
  // 输入的位置不会一路往屏幕底下掉。等这一轮渲染过去再量，行的盒子才在。
  nextTick(keepActiveRowAnchored)
}

/**
 * 一条分录最基本的两个元素：会计科目 + 金额。少一个就不算分录，
 * 也就没资格接着往下写 —— 半条分录一路传下去，最后存出来的是一张谁也看不懂的纸。
 * 返回该说哪句提示；成了返回 null。
 */
function incompleteHint(row) {
  if (!row) return null
  if (!row.subject_id) return '先给这一行选一个会计科目'
  if (badSubject(row)) return rowProblemText(row)
  if (!num(row.amount)) return '先给这一行填上金额'
  return null
}

/**
 * 在绿着的这一行上面 / 下面插一行。
 *
 * 两个按钮一个在上一行、一个在下一行，方向就是按钮所在的位置，不该再靠
 * 「这个 ⊕ 是往下插的」去记 —— 一个记不住的图标比没有图标更糟。
 *
 * 新行沿用这一行的方向和摘要：拆行本来就是把「同一句话有好几笔」摊成几行，
 * 方向得跟着走（连续三行借方），摘要也跟着走（同一句话抄三遍不如抄一次）。
 */
function insertRowAt(offset) {
  const at = Math.max(0, form.entries.indexOf(greenRow.value)) + offset
  const cur = form.entries[at - offset]
  const hint = incompleteHint(cur)
  if (hint) return ElMessage.warning(hint)
  form.entries.splice(at, 0, {
    subject_id: null,
    direction: cur ? cur.direction : 'DEBIT',
    amount: '',
    summary: cur ? cur.summary : ''
  })
  activeRow.value = form.entries[at]
  focusSum(at)
}
const insertRowAbove = () => insertRowAt(0)
const insertRowBelow = () => insertRowAt(1)

/**
 * 最后一行**有内容**的行号（一张都没填就是 -1）。
 *
 *  底下那几行「等着填」的空位不算内容 —— 它们是留给人写字的地方。
 *  算成一条线而不是每一行现算：一屏 154 行时，逐行现算就是 154² 次判断
 *  （这一页刚因为同类问题卡死过一次，见 utils/voucherRows.js）。
 */
const lastFilledAt = computed(() => lastFilledRowAt(form.entries))

/**
 * 规则在 utils/voucherRows.js 里（rowCanBeDeleted）：底下那几行等着填的空位
 *  之外，哪一行都删得掉 —— 四行是**行数的下限**，不是**分录的护栏**。
 */
function canDeleteRowAt(i) {
  return rowCanBeDeleted(form.entries, i, lastFilledAt.value)
}

/** 鼠标压在 ⊗ 上要说的话：只读就说清楚只读，别让人以为按钮坏了 */
const delTitle = computed(() => (readonly.value ? '只读凭证不能改' : '删除这一行'))

/**
 * 删掉这一行。
 *
 *  **后面的行自动往上排** —— 这是用户 2026-10-24 明确要的：删掉前四行里的
 *  某一行时，后面的行顶上来的位置，别在那儿留一个空行。
 *
 *  删完底下补回空位（还是四行）：那几行是留给人接着写字的地方，
 *  删一行就少一行地方，录下一条时又得临时找地方。
 */
function removeRow(i) {
  if (readonly.value || !canDeleteRowAt(i)) return
  const rows = form.entries
  const gone = rows[i]
  rows.splice(i, 1)              // 后面的行自动往上排
  ensureRows(4)
  // 绿行得跟着往上挪：被删的那一行已经不在表里了，而鼠标还压在这个位置上。
  // 绿行得落在**顶上来的那一行**上 —— 不然 ⊕ / ＋ / ⊗ 一起消失，
  // 人还以为按钮坏了。
  const next = rows[i] ?? rows[rows.length - 1] ?? null
  if (activeRow.value === gone) activeRow.value = next
  if (hoverRow.value === gone) hoverRow.value = next
  // 正在「标记科目」的那一行被删了，它的标记也得收掉（键是分录对象，不是行号）
  if (markingRows.value.has(gone)) {
    const rest = new Set(markingRows.value)
    rest.delete(gone)
    markingRows.value = rest
  }
  // 这一行「已提交的数」也一起收掉（键也是分录对象）——
  // 留着的话，Map 里就攒着一份没人再引用的旧值。
  committedAmounts.value.delete(gone)
}

/** 至少留 4 行空着：多数凭证两三条就录完了，但行得摆够，
 *  录第三条第四条时眼睛不用临时去找地方。 */
function ensureRows(n) {
  // 补出来的空行是「等着填」的，不跟着上一行抄摘要 —— 抄了反而像是这张凭证真有四条
  while (form.entries.length < n) {
    form.entries.push({ subject_id: null, direction: 'DEBIT', amount: '', summary: '' })
  }
}

/** 在最后加一行。落笔在**新那行的摘要**上，不去自动弹科目面板 ——
 *  回车是一路往下写的，弹面板会把光标从摘要上抢走。 */
function addRow(row) {
  // 没指定行就是从「＋」来的：看的是最后一行
  const hint = incompleteHint(row ?? form.entries[form.entries.length - 1])
  if (hint) return ElMessage.warning(hint)
  // 自动补一行方向相反的分录，符合连续录入的习惯
  const last = form.entries[form.entries.length - 1]
  form.entries.push({
    subject_id: null,
    direction: last ? (last.direction === 'DEBIT' ? 'CREDIT' : 'DEBIT') : 'DEBIT',
    amount: '',
    summary: last ? last.summary : ''
  })
  activeRow.value = form.entries[form.entries.length - 1]
  focusSum(form.entries.length - 1)
  // 新长出来的那条空分录行同样收回锚点高度（见 keepActiveRowAnchored）：
  // 这正是用户 2026-10-19 提的那一下 —— 第七行再回车，新行别掉到屏幕底下。
  nextTick(keepActiveRowAnchored)
}

/**
 * 从列表带过来的筛选条件（kw / min / max）。
 * 凭证页自己不知道账是怎么筛的，全靠列表页把条件写在 URL 上带过来 ——
 * 进来的路上带着，返回的时候原样捎回去，前一张后一张时也一路带着。
 */
const listFilter = computed(() => {
  const q = route.query || {}
  const out = {}
  if (q.kw) out.keyword = String(q.kw)
  if (q.min !== undefined && q.min !== '') out.min_amount = q.min
  if (q.max !== undefined && q.max !== '') out.max_amount = q.max
  return out
})
/** 有没有跟着列表来。空对象 = 不知道从哪张列表进来的，按同期间翻。 */
const hasListFilter = computed(() => Object.keys(listFilter.value).length > 0)

/** 返回：回**列表页**，并把筛选条件原样带回去。
 *  早先用的是 `$router.back()` —— 那是从哪儿来回哪儿去：从「新增凭证」跳进来
 *  就回新增凭证，从打印预览跳进来就回打印预览，压根不保证回到列表；
 *  就算碰巧回到了列表，条件也已经丢了，用户得重新筛一遍。 */
function backToList() {
  router.push({ name: 'vouchers', query: route.query || {} })
}

/** 前一张 / 后一张：按**凭证号顺序**翻（期间内按号、跨期间按月份）。
 *  范围由后端定：带筛选条件时在筛选结果集里翻（可跨月跨年），
 *  没带条件时在这张所属的那个月里翻（到月底停住）。
 *  条件一路带着，跳到头/尾时按钮自己禁用。 */
function goNeighbour(which) {
  const id = which === 'prev' ? prevId.value : nextId.value
  if (!id) return
  router.push({ name: 'voucher-edit', params: { id }, query: route.query || {} })
}

/** 插入凭证：**向前**插入 —— 接着当前这张开一张新的，占当前这张的号，
 *  当前及以后的号整体顺延。日期沿用当前凭证的日期。
 *
 *  `after` 是参照凭证的 id —— 新页拿它去后端问「这张占哪个号、谁要顺位」，
 *  顺延跟建凭证在同一个事务里做。所以这里只跳页、什么也不改：
 *  一个格子都没填就退出，后头那叠号原封不动；空凭证占号、把别人的号顶走才是真事故。 */
function insertVoucher() {
  if (!voucherId.value) return
  router.push({
    name: 'voucher-new',
    query: { date: form.voucher_date, after: voucherId.value, position: 'before' }
  })
}

/** 问后端要插入位的预填值。拿不到就退回普通新开一张 —— 别把整页卡住。 */
async function loadInsertPreview(afterId, position = 'before') {
  // **只读账号不调它**（2026-10-28）：与 ``loadNextDefaults`` 同理 ——
  // 「插入凭证」是写动作，它的预填值（占哪个号、谁要顺位）只读账号用不上。
  if (!store.canWrite) return null
  try {
    return await api.insertPreview(store.bookId, afterId, position)
  } catch {
    return null
  }
}

/** 增加凭证：**向后**插入（用户 2026-10-19 定的口径）——
 *  在当前这张**后面**接着记：新凭证拿当前号 +1，比它大的号往后顺延。
 *  日期沿用当前凭证的日期（「接着上一张往下记」）。
 *
 *  原来这里只是 `router.push({name:'voucher-new'})`，不带任何参照：落到的
 *  是期间末尾的下一个号，在哪张凭证上点都一样 —— 与「向后插入」是两件事。
 *  而且没有参照凭证就没有号可算，`/vouchers/next` 拿到的还是「期间最大号 +1」。
 */
function addVoucher() {
  if (!voucherId.value) return
  router.push({
    name: 'voucher-new',
    query: { date: form.voucher_date, after: voucherId.value, position: 'after' }
  })
}

// --------------------------------------------------------------------- 拆分凭证

const splitVisible = ref(false)

/** 这一页真正填了内容的分录 —— 拆分看的是这些，不是那几行空白的占位行。
 *
 *  与「能落库的分录」是**同一份**：有科目且金额非零。
 *  两处各写一遍过滤条件，早晚会长歪 —— 现在共用一份。
 */
const filledRows = computed(() => payableEntries.value)

/**
 * 能不能拆：必须是**已落库**的凭证、这张还能改、且至少两条分录。
 *
 * 已记账的不能拆（数字已经进账簿），已结账期间的不能拆（那段账簿定稿了）——
 * 这两条与「改」「删」是同一道闸，措辞也一致。
 */
const canSplit = computed(() =>
  !!voucherId.value && !readonly.value && filledRows.value.length >= 2)

/** 按钮灰着的时候，把为什么灰说出来 —— 灰按钮自己不会解释自己。 */
const splitHint = computed(() => {
  if (!voucherId.value) return '新凭证还没有落库，保存后才能拆'
  if (posted.value) return '已记账凭证不能拆分，请到「期末结账」反结账后再拆'
  if (locked.value) return '该凭证所在期间已结账，不能拆分'
  if (!store.canWrite) return '这个账套是只读的，不能修改凭证'
  if (filledRows.value.length < 2) return '至少要有两条分录才能拆'
  return '在分录边界处把这一张拆成两张'
})

/**
 * 弹窗里那两张小表要显示的分录。
 *
 * **行号按这里的顺序重排**（1..n），与库里那张凭证的 line_no 一致 ——
 * 因为拆之前会先把没存上的改动冲掉（``flushIfDirty``），
 * 存完之后两边的行号就是同一条数。
 */
const splitRows = computed(() => filledRows.value.map((e, i) => {
  const sub = subjectOf(e.subject_id)
  return {
    line_no: i + 1,
    summary: e.summary || '',
    subject_text: sub ? `${sub.code} ${sub.name}` : '',
    direction: e.direction,
    amount: centsText(centsOf(e.amount))
  }
}))

/**
 * 打开拆分弹窗。
 *
 * **先把没存上的改动冲掉再开**：这一页是自动保存的，而拆的是**库里**那张凭证 ——
 * 屏幕上的改动还没落库就拆，拆走的是旧内容，人却以为是刚改的那版。
 */
async function openSplit() {
  if (!canSplit.value) return
  if (!(await flushIfDirty())) {
    ElMessage.warning('还有改动没存上，先存好再拆')
    return
  }
  splitVisible.value = true
}

/**
 * 拆完了：原凭证的行数少了（号不动）→ **必须重载**，否则页面上还摆着旧的。
 *
 * 凭证 id 没变（还是这一张），所以地址栏不用动。
 */
/** 拆分在途：这一趟还没回来之前，不再发第二趟（审计 §3.29）。 */
let splitting = false

async function onSplit({ at_line, done }) {
  // 用 store.bookId（与弹窗的预览、后面的重载同一个账套）：
  // 拆的是当前账套里的这张凭证 —— 账套不对时后端会 404，不会写到别家账套去。
  const bookId = store.bookId
  if (splitting) { done?.(); return }
  splitting = true
  try {
    const r = await api.splitVoucher(bookId, voucherId.value, { at_line })
    splitVisible.value = false
    await load()
    await loadNeighbour()
    ElMessage.success(
      `已拆成两张：${r.original.voucher_no}（保留前 ${at_line} 行）`
      + ` 和 ${r.created.voucher_no}（带走后 ${r.created.entries.length} 行）`)
  } catch (e) {
    // 错误文案已经由 http 拦截器弹过了，这里不再重复报一遍
  } finally {
    splitting = false
    // 成功时弹窗关掉了；失败时要让人能再试一次 —— 按钮不能一直灰着。
    done?.()
  }
}

// 打印不直接出纸：先弹窗选模板和纸型、看预览，确认了才打。
const printVisible = ref(false)
const printPath = computed(() => `/books/${store.bookId}/print/vouchers.html`)

/** 打印这一张凭证。
 *
 *  模板里这个「打印预览」按钮挂的是 `printVoucher`，而这个函数曾经**根本不存在** ——
 *  按钮点下去只会得到一句 `printVoucher is not a function`。列表页那边叫
 *  `printVouchers`（复数，走勾选），这里是单张，所以另起一个名字而不是共用。
 *
 *  期间取**这张凭证自己的** `voucherPeriodId`，不是顶栏那个 —— 顶栏停在别的月时
 *  打开一张旧凭证，打印出来的东西必须仍然是这张凭证所属期间的（P1-16 同一条理由）。
 *
 *  这个回落口径只写在 printParams 一处：守卫和弹窗读的是同一个值，
 *  分成两处各算一次，日后改了一处就会悄悄打错月。
 */
const printParams = computed(() => ({
  period_id: voucherPeriodId.value || store.periodId,
  voucher_ids: String(voucherId.value),
}))

function printVoucher() {
  if (!voucherId.value) return
  if (!store.bookId || !printParams.value.period_id) {
    ElMessage.warning('账套或期间还没就绪，打印需要稍等一下')
    return
  }
  printVisible.value = true
}

/** 拉上一张/下一张的导航信息。
 *
 *  seq 传进来是为了丢弃过期响应：load() 里那次必须传，而保存成功后的那次
 *  （保存不走 load()，没有并发）可以省略 —— 所以只在**显式给了**的情况下
 *  才判代次，免得把保存路径也卡住。
 */
async function loadNeighbour(seq) {
  if (seq !== undefined && stale(seq)) return
  if (!voucherId.value) {
    prevId.value = null
    nextId.value = null
    return
  }
  const myId = voucherId.value
  let r = null
  try {
    r = await api.neighbour(store.bookId, myId, listFilter.value)
  } catch (e) {
    // **失败不许抛出去**（2026-10-28 修只读账号的假错误时改的）：
    //
    // 原来这里没有 try/catch，那个 403 一路抛到 ``load()`` 外面 ——
    // 而 ``load()`` 的最后一句正是 ``await loadNeighbour(seq)``，
    // 于是 **onMounted 里那句 ``await nextTick(() => syncGridWidth())``
    // 跟着不执行了**：凭证页的表格列宽没对齐。
    //
    // 界面上还弹一条「当前账套为只读权限」—— 可那张凭证明明好好地
    // 显示着，用户只会被这句无关的话唬住。
    //
    // 拿不到前后张只是「翻不动」，不是「这张凭证打不开」。
    const status = e?.response?.status
    if (status !== 403 && status !== 404) {
      // **真错照样要说**（500、网络断了）：别把真问题一起吞掉。
      ElMessage.error(e?.response?.data?.detail || '加载前后张失败')
    }
    // 兜底成「两头都没有」：按钮于是灰着 —— 这正是「翻不动」该有的样子。
    r = { prev_id: null, next_id: null }
  }
  if ((seq !== undefined && stale(seq)) || voucherId.value !== myId) return
  prevId.value = r.prev_id
  nextId.value = r.next_id
  // 响应里还有 index/total/scope（这套顺序里的第几张、总共几张、翻的是哪个范围），
  // 前端不再用：那句话在页面上被用户打回（2026-10-24），后端这份数据
  // 留着不碍事（接口契约与性能用例都指着它），只是不再往页面上摆。
  // （scope 现在有三种取值：period=这张所属的月、filtered=筛选结果集、
  //   book 是旧值，已随 2026-10-25 那次范围收敛一并作废。）
}

/** load() 的代次标记。
 *
 *  切换凭证只改 route.params.id，组件不重新挂载，load() 会被反复触发；
 *  而 load() 内部要先 await getVoucher() 再 await loadSubjects()，两次调用
 *  天然会交错。于是「先发的请求后到」时，那份**旧**响应会
 *  Object.assign(form, ...) 覆盖掉新凭证的表单 —— 页面上显示的是 A 凭证的
 *  编号配着 B 凭证的分录，而且没有任何报错。
 *
 *  每次 load() 领一个自增代次；任何 await 之后先确认代次仍是自己的，
 *  不是就直接返回，不碰任何状态。没有 AbortController 是因为这里有 4 个
 *  分散的 await 点，逐个判代次比在每处都塞 signal 更容易看漏 ——
 *  看漏一处就是一个静默覆盖。
 */
let loadSeq = 0
/** 响应回来后确认「我还是最新那一次」，不是就说明该丢弃了。 */
function stale(seq) { return seq !== loadSeq }

/**
 * 把整张表单抹回「什么都没有」的样子。换账套时用。
 *
 * 原来换账套只是让 load() 重跑一遍，而 load() 开头无条件
 * `voucherId.value = Number(route.params.id || 0)` —— 地址栏里的 id 是上一个
 * 账套的凭证号，在新账套里要么不存在、要么是**另一张**凭证。表单、分录、
 * 「已自动保存 09:49」这些上一张的痕迹全留在屏幕上，而 `voucherId` 已经
 * 指着外账套的号了：接下来随便改一格，就是拿新账套的地址去更新旧账套的凭证。
 *
 * `loadSeq` 也得一起抬一代：还在路上的旧响应回来时必须自己认得出过期，
 * 否则抹干净的表单又被它填回上一张的样子。
 */
/**
 * 切账套时，地址栏里那个 id 是**上一个账套**的凭证号。
 *
 * 凭证 id 是**账套内自增**的，两个账套撞号是常事 —— 于是切过去有两种结果：
 *   · 新账套**恰好有**这个 id → 页面**静默打开另一本账的另一张凭证**，
 *     屏幕上那张跟人刚才看的那张长得完全不一样，却没有任何提示；
 *   · 新账套没有这个 id → 报错跳列表（那半处理得体面）。
 *
 * 这里记下「上一个账套的哪张凭证」，切过去之后**明示一次** ——
 * 只加提示，**不改加载行为**（还是打开本账套的同号凭证，不是回到上一本账）。
 */
const crossBook = ref(null)

/** 账套的名字（提示里要指名道姓，光说「另一个账套」等于没说）。 */
function bookName(bookId) {
  const b = store.books.find((x) => x.id === bookId)
  return b?.company_name || b?.name || `账套 ${bookId}`
}

function resetForm() {
  ++loadSeq
  // 换一张凭证 = 换一把幂等键（这一把键只对应「这一张新建的凭证」）。
  createKey.value = ''
  // 换一张凭证 = 换一枚乐观锁令牌（上一张的令牌对这一张没有意义）。
  voucherVersion.value = ''
  // 冲突标志也是「上一张的事」—— 换一张就该重新开始。
  versionConflict.value = false
  // 「上一张是哪个账套的哪张」**不再在这里记** —— 见 load() 里的说明：
  // 切账套时这个组件会被整个重挂掉，组件内记的东西活不到那一步。
  // 跨过重挂的记号在 store 里（stores/session.js 的 prevBookId）。
  voucherId.value = 0
  voucherPeriod.value = null
  voucherPeriodId.value = 0
  insertCtx.value = null
  savedAt.value = ''
  failedSig.value = ''
  prevId.value = null
  nextId.value = null
  subjectRefs.clear()
  rowEls.clear()
  sumRefs.clear()
  amtRefs.clear()
  markingRows.value = new Set()
  committedAmounts.value.clear()     // 分录对象整套换掉了，已提交的数也一起换
  activeRow.value = null
  hoverRow.value = null
  Object.assign(form, {
    voucher_no: '', voucher_date: '', status: 'UNPOSTED',
    attachments: 0, remark: '', entries: [], legacySubjects: []
  })
  ensureRows(4)
  // 抹干净之后不算「有没存上的改动」：这一页本来就不该替上一个账套存任何东西
  lastSaved.value = entrySig.value
}

// ---- 科目表年度：这一页用哪一年的科目表 ----
/**
 * 这张凭证属于哪一年 —— 决定这一页用**哪张科目表**。
 *
 * 服务端直接把年度随凭证给出来（凭证详情里的 `year`，取自它自己那一期）。
 * 万一没有（旧接口、异常返回），退回本地按 period_id 反查，最后才落到顶栏那一年。
 */
function yearOfVoucher(v) {
  if (v?.year) return Number(v.year)
  const p = store.periods.find((x) => x.id === v?.period_id)
  return p?.year || store.currentYear
}
// ---- 科目表年度段结束 ----

async function load() {
  const seq = ++loadSeq
  // **路由里的 id**：打开一张存好的凭证、翻上一张/下一张时都有。
  const routeId = Number(route.params.id || 0)
  // **「这一张已经建出来了」这个事实**（审计 N5）。
  //
  // 新建凭证落库后，地址栏换成了 /vouchers/{id}，但 **router 内部还停在
  // /vouchers/new**（走 router 会把组件整个重挂，正在填的表单、光标、绿行
  // 全没了 —— 见 save() 里那段说明）。于是：
  //   · 重挂（切期间、切账套、点刷新）之后，route.params.id 仍是 undefined
  //   · → myId = 0 → 这一张被当成**全新的空凭证**
  //   · → 下一趟自动保存**又 POST 一张**，库里多出一张几乎一样的。
  //
  // 那个事实记在 store 里（跨得过重挂），这里把它捡回来：
  // **只认当前账套的那一张** —— 换账套后不能拿旧账套的 id 去新账套里查。
  const created = store.createdVoucher
  const mine = created && created.bookId === store.bookId ? created : null
  const myId = routeId || (mine ? mine.id : 0)
  // **幂等键也要跟着活下来**：响应在路上丢了的那一趟（服务端其实已经入库，
  // 只是回执没到），重挂后靠这把键去重，服务端把请求收敛到同一张上
  // （审计 §3.28）。没有它，重挂后就是一把新键 → 服务端当真新建一张。
  if (!routeId && mine && mine.createKey && !createKey.value) {
    createKey.value = mine.createKey
  }
  // **切账套撞号**：上一本账是从 store 里取的（见 stores/session.js 的 prevBookId）。
  //
  // 以前这件事记在组件内（resetForm 里），但**永远不会执行**：切账套时
  // viewKey 变了 → router-view 先把旧组件卸载掉 → 组件内 watch 的 effect
  // 随 scope 一起停用 → 回调根本不跑。于是「已切换到账套…」那句提示
  // 永远发不出来，用户看着本账套的 #12，以为还是刚才那本账的那张
  // （审计 §3.33）。
  //
  // 现在跨过重挂的记号放在 store 里，凭证页**重新挂载后**才来取，
  // 所以提示这一次真的发得出来。
  const prevBook = store.takePrevBookId()
  if (prevBook && prevBook !== store.bookId && myId) {
    crossBook.value = { bookId: prevBook, voucherId: myId }
  }
  // 先认下路由里的 id：换凭证的那一瞬间，工具栏不该还挂着上一张的打印预览
  voucherId.value = myId
  savedAt.value = ''
  insertCtx.value = null
  subjectRefs.clear()
  rowEls.clear()          // 分录对象整套换掉了，上一张的行盒子一并扔掉
  // 标记状态是拿**分录对象**当键的，而分录对象刚被换了一整套 ——
  // 不清的话，上一张还在标记的行会留在集合里，白收着这张的余额
  markingRows.value = new Set()
  // 「已提交的数」也是拿分录对象当键的，同理要清（见 commitAmount 的说明）
  committedAmounts.value.clear()
  if (myId) {
    let v = null
    try {
      v = await api.getVoucher(store.bookId, myId)
    } catch (e) {
      if (stale(seq)) return
      // 这个 id 在**当前账套**里读不出来。多半是切了账套而地址栏还挂着
      // 上一张的号（两个账套的凭证 id 是各自自增的，撞上号也不稀奇）。
      // 停在一张空表上、还绑着那个 id，只会更让人以为凭证没了 —— 回列表去。
      const cross = crossBook.value
      if (cross) {
        // 切账套撞号：说清「你原来看的是哪本账的哪张」（见 crossBook 的说明）
        crossBook.value = null
        ElMessage.error(
          `已切换到账套「${bookName(store.bookId)}」：原凭证（账套「${bookName(cross.bookId)}」`
          + `#${cross.voucherId}）在此账套里不存在`)
      } else {
        ElMessage.error('这张凭证在当前账套里不存在')
      }
      router.push({ name: 'vouchers' })
      return
    }
    if (stale(seq)) return
    // 双重确认：代次之外再核对 id，防的是「代次对上了但期间切换了」
    if (Number(route.params.id || 0) !== myId) return
    // 切账套撞号：这张是**本账套**的同号凭证，不是刚才那本账的那张 —— 明示一次。
    // 只提示，不改加载行为（下面照旧打开本账套的这张）。
    if (crossBook.value) {
      const cross = crossBook.value
      crossBook.value = null
      ElMessage.warning(
        `已切换到账套「${bookName(store.bookId)}」：原凭证（账套「${bookName(cross.bookId)}」`
        + `#${cross.voucherId}）不在此账套，已打开本账套的 #${myId}`)
    }
    voucherPeriod.value = store.periods.find((p) => p.id === v.period_id) || null
    voucherPeriodId.value = v.period_id   // store.periods 还没灌完时也能算余额
    // **这枚乐观锁令牌**是从这一份读到的 —— 保存时原样送回去。
    voucherVersion.value = v.version || ''
    // **科目表按这张凭证自己那一年取**（不是顶栏那一年）——
    // 往年凭证的分录挂在往年的科目上，用错年度的科目表，格子里全是空的。
    await loadSubjects(yearOfVoucher(v))
    if (stale(seq)) return
    Object.assign(form, {
      voucher_no: v.voucher_no, voucher_date: v.voucher_date, status: v.status,
      attachments: v.attachments, remark: v.remark,
      entries: v.entries.map((e) => ({
        subject_id: e.subject_id, direction: e.direction, amount: e.amount, summary: e.summary
      })),
      // 这张凭证在**账上**用掉的科目：这些分录不是这一页新挂的，
      // 非末级那道闸就不该拦它们（见 newNonLeafRows）
      legacySubjects: v.entries.map((e) => e.subject_id)
    })
    ensureRows(4)
    // 打开一张**存好的**凭证：每一行都是**定下来的**，不是正在敲的半截数 ——
    // 余额一开始就得是对的（见 commitAmount）。
    seedCommitted(form.entries)
    lastSaved.value = entrySig.value      // 刚打开的这张就是「已保存」的样子，别一进来就重存一遍
    await loadNeighbour(seq)
  } else {
    form.legacySubjects = []              // 新凭证：所有分录都是这一页新挂的
    // 新开一张：凭证号和日期都先替人填好。
    // 号码问后端要（`/vouchers/next`），因为它跟落库时用的是同一个发号函数 ——
    // 自己照着列表猜一个「最大号 +1」会跟真正发出来的号对不上，白填一次。
    // 日期取这个期间**最后一张凭证**的日期：接着上一张往下记才对。
    // 之前填的是「今天，28 号封顶」，月底录 12 月时日期会一天天往回退，看着像倒退。
    // 「插入凭证」带 ?date= 的以它为准 —— 插在谁后面就沿用谁的日期。
    // 「插入凭证」走的是另一个预填源：插在谁后面，这张就拿谁的号，
    // 后面的号整体顺延。顺延真的发生是在保存那一刻（同一个事务里），
    // 这里只是先把号码和「谁要顺位」摆在页面上，让人看清再动手。
    const after = Number(route.query.after || 0)
    if (after) {
      const ip = await loadInsertPreview(after, route.query.position === 'after' ? 'after' : 'before')
      if (stale(seq)) return
      insertCtx.value = ip
      if (ip) {
        form.voucher_no = ip.voucher_no || ''
        form.voucher_date = ip.voucher_date || route.query.date || defaultDate()
      } else {
        const n = await loadNextDefaults()
        if (stale(seq)) return
        form.voucher_no = n.voucher_no || ''
        form.voucher_date = route.query.date || n.voucher_date || defaultDate()
      }
    } else {
      const n = await loadNextDefaults()
      if (stale(seq)) return
      form.voucher_no = n.voucher_no || ''
      form.voucher_date = route.query.date || n.voucher_date || defaultDate()
    }
    // **科目表按这张新凭证要落的那一年取**：
    //   · 「插入凭证/增加凭证」是插在**参照凭证那一期**里的（后端要求同一期间），
    //     参照在往年、顶栏停在当前年时，用顶栏那年的科目表就是挂错表；
    //   · 普通的「新增凭证」落在顶栏那一期，用顶栏那一年。
    await loadSubjects(yearOfVoucher(insertCtx.value || { period_id: store.periodId }))
    if (stale(seq)) return
    voucherPeriod.value = null   // 新凭证还没保存，期间以保存时按日期落库为准
    voucherPeriodId.value = 0    // 还没落库：期间跟顶栏（保存时也是按顶栏那期落的）
    ensureRows(4)
    prevId.value = null
    nextId.value = null
  }
}

/** 问后端要「新开一张」的预填值。拿不到就退回空号 + 本地日期，别把整页卡住。 */
async function loadNextDefaults() {
  // **只读账号不调它**（2026-10-28）：这是「新建凭证」那道**写动作**的预填值，
  // 而只读账号不能新建凭证 —— 那个号与日期对它没有用，后端按设计也拒了它。
  // 不调，就不发这趟请求，也就不会有那个 403。
  if (!store.canWrite) return {}
  try {
    return await api.nextVoucher(store.bookId, store.periodId)
  } catch {
    return {}
  }
}

function defaultDate() {
  const p = store.currentPeriod
  // **本地日期，不是 UTC。** 原来这里写的是 `toISOString().slice(0, 10)` ——
  // 那是 UTC 日期：北京时间 00:00–08:00 之间会落到**前一天**，而下面
  // 走 `getDate()` 的那条路是本地日期。两条路口径不一致，同一张单据
  // 「有没有当前期间」不同就会落在不同日子上。函数上面的注释写的就是
  // 「本地日期」，代码没兑现它。
  if (!p) return todayLocal()
  const day = new Date().getDate()
  const d = String(Math.min(day, 28)).padStart(2, '0')
  return `${p.year}-${String(p.month).padStart(2, '0')}-${d}`
}

/**
 * 落库。
 *
 * 这里原来挂着个 `quiet` 形参，`quiet === false` 那条路是「存好了 + 弹成功 + 跳回列表」。
 * **它已经删掉了**，因为走不到：
 *  这一页**没有保存按钮**（工具栏上只有返回 / 前一张 / 后一张 / 插入 / 增加 / 打印，
 *  见模板顶部），自动保存是唯一的落库路径；而两个调用方 `autoSave()` 与
 *  `flushIfDirty()` 都传 `quiet: true`。形参没有默认调用方（`save()` 不经 defineExpose
 *  暴露出去，模板里也没有保存控件），于是 `!quiet` 恒假 —— 留着一个永远不进的
 *  分支，比没有更糟：它让人以为「手动保存」这条老路还在。
 *
 * 删掉之后 `quiet` 就没有存在意义了 —— 它原本只控制「成功那句提示 + 跳列表」，
 * 失败的几句提示（只读 / 空表 / 不平）它同样一句都挡不住。所以形参一并去掉，
 * 调用方写成 `save()`。**失败一定要说**：自动保存失败而页面上一声不吭，人是会信的。
 *
 * 返回有没有存成。离开页面前那趟 flush 靠它决定「放不放人走」——
 * 吞掉失败就又变成静默丢数据。
 */
async function save() {
  if (readonly.value) {
    ElMessage.warning(posted.value ? '已记账凭证不能修改，请到「期末结账」反结账后再改'
      : !store.canWrite ? '这个账套是只读的，不能修改凭证'
        : locked.value ? '该凭证所在期间已结账，不能修改'
          : '当前期间已结账，不能修改凭证')
    return false
  }
  // **版本冲突之后不再自动保存。**
  //
  // 服务端回 409 = 「你改的是别人已经改过的那一版」，这一版的令牌作废了。
  // 而自动保存是**跟着人敲字走的**：不掐掉的话，人每敲一下就重试一次、
  // 每次都 409、每次都弹一条一样的红字 —— 吵得没法干活，而结果一条也没存上。
  //
  // 正确的出路是**刷新页面看当前内容**（页面上的这些改动本来也存不进去）。
  // 这里只报一次，后面安静。
  if (versionConflict.value) return false
  // **提交的那一份，就是页面上算平的那一份**（见 payableEntries）。
  // 以前这里是另一份过滤条件，于是出现过「界面说平、后端说不平」：
  // 页脚显示「借贷平衡 ✓」、合计行显示「借 100.00 / 贷 100.00」，自动保存也放行，
  // 可提交时那一行被静默丢掉，后端只回一句「借贷不平衡」——
  // 而那行数字在屏幕上明明存在（审计 §3.23）。
  const entries = payableEntries.value
    .map((e) => ({
      subject_id: e.subject_id,
      direction: e.direction,
      // 金额按同一套定点化落成字符串：页面上看见的、判平用的、后端收到的
      // 是同一个数，不会出现「看着是 300.30、发出去是 300.29999999999995」
      amount: centsText(centsOf(e.amount)),
      summary: e.summary || ''
    }))
  // **这一版的签名**：既是「发出去的那一版」，也是「**被拦住的那一版**」。
  //
  // 「发出去的那一版」：请求在路上时人可能还在改，回来后要把水位定在发出去的那版，
  // 否则这几秒里敲的东西会被当成「已保存」丢掉。
  //
  // 「被拦住的那一版」：下面那两条「这一版根本不允许落库」的拦截
  // （有金额没科目、非末级科目）—— **它们也必须把签名记进 failedSig**。
  // 不记的话，自动保存的 700ms 防抖每触发一次就再弹一条一模一样的红字：
  // 报错成了背景噪音，人连自己在改什么都看不清了（审计 N4）。
  //
  // 记签名**不丢人**：提示照弹（「保存被拦住」这件事对用户依然可见），
  // 只是**同一版**不再重复弹 —— 人改完那一行，签名一变，下一趟照常自动保存。
  const sentSig = entrySig.value
  // 「有金额、没科目」的行**不能静默丢掉**：它提交时不存在，可屏幕上摆着数字。
  // 这时要说清楚是**哪一行**差什么，而不是让后端拿「借贷不平衡」当谜语。
  if (rowsMissingSubject.value.length) {
    const missing = rowsMissingSubject.value[0]
    const at = form.entries.indexOf(missing) + 1
    // 记下这一版：同一版不再自动重试（否则每敲一下就再弹一遍，见上面的 sentSig）。
    failedSig.value = sentSig
    ElMessage.error(`第 ${at} 行还差会计科目 —— 有金额没科目的行不会被提交，请先把它写完`)
    return false
  }
  if (!entries.length) { ElMessage.warning('请至少填写一条分录'); return false }
  // 非末级科目拦下来一定要出声：闷声不响的话，人只会以为存好了，
  // 而这笔分录其实根本没落库 —— 等发现时已经按它记了好几天的账。
  // 只拦**这一页新挂上去的**那些（见 newNonLeafRows）；账上原有的是服务端的事。
  if (newNonLeafRows.value.length) {
    // 同上：这一版也被记下来，别让防抖每 700ms 弹一遍同一句话（审计 N4）。
    failedSig.value = sentSig
    ElMessage.error(rowProblemText(newNonLeafRows.value[0]))
    return false
  }
  if (!balanced.value) { ElMessage.error('借贷不平衡，无法保存'); return false }

  const payload = {
    voucher_date: form.voucher_date, entries, remark: form.remark,
    attachments: form.attachments,
    // **凭证号不送**（用户 2026-10-19 定的：新增 / 插入 / 修改三处一律只读，
    // 号由服务端发）。服务端那边也不采信客户端送来的号 ——
    // 新增走 next_voucher_no，插入走 insert_slot，修改跨期时按目标期间重发。
    //
    // 以前这里是把 form.voucher_no 一起送过去的：跨期改日期时，服务端重发完号、
    // 页面还没来得及换（或者人连着改了两次日期，两趟保存撞在一起），
    // 送过去的还是**旧号** —— 服务端当场回一句「凭证号不能手工修改（记0009 → 记0012）」，
    // 人完全看不懂是自己改的日期惹的祸。不送就没有这个坑。
    //
    // 只有新凭证 + 还在插入模式里才带 insert_after：更新老凭证时带了会把号再顺位一次
    insert_after: !voucherId.value && insertCtx.value ? insertCtx.value.anchor_id : null,
    // 向前（占参照的号）还是向后（拿参照号 +1）。与 insert_after 同生共死。
    insert_position: !voucherId.value && insertCtx.value
      ? (insertCtx.value.position === 'after' ? 'after' : 'before') : 'before',
    // **乐观锁令牌**（审计 §3.15）：把这张凭证我**读到时**的版本带回去。
    //
    // 服务端拿库里此刻的版本比一下：不一致 → 409「已被他人改动，请刷新」。
    // 那正是「别人改了这张、我这份还是旧的」那个真实场景 ——
    // 以前那种情况是**静默覆盖**：谁后保存谁赢，前一个人的改动人间蒸发，
    // 而账上看着还是平的（这正是 §3.15「数字错了但系统全绿」那一类）。
    //
    // **不带 = 不比**（老客户端、以及刚建出来还没拿到令牌的那一趟），
    // 那条路上的正确性由服务端的行锁兜着。
    version: voucherVersion.value || null
  }
  const wasNew = !voucherId.value
  // 请求打到**这张表单所属的账套**，不是当下的 store.bookId：切账套那一瞬间
  // store.bookId 已经变成新的了，拿它发请求等于把上一个账套的凭证写进新账套。
  const bookId = formBookId.value || store.bookId
  saving.value = true
  const p = (async () => {
    try {
      // 新建才带幂等键（更新本来就有 id，重复提交也是同一条记录）。
      if (wasNew && !createKey.value) createKey.value = newIdempotencyKey()
      // **「这一张正在建」这件事要跨过重挂**（审计 N5）。
      //
      // 为什么要在**发请求之前**记：请求发出去、服务端**已经入库**、响应却在
      // 网络上丢了 —— 这时候这一版既没有 id 也没有「已落库」的凭据，
      // 只剩这把幂等键。记在组件里，重挂即丢；记在 store 里，重挂后还能
      // 拿同一把键去重（服务端按键去重，见 createKey 的说明）。
      //
      // 记号只对「同一段路里的重挂」有用：MainLayout 的路由 watcher 会在
      // 换页面时把它清掉（用户点「新增凭证」重新开一张时不该被带回刚才那张）。
      if (wasNew) {
        store.createdVoucher = { bookId, id: 0, createKey: createKey.value }
      }
      let saved
      if (wasNew) {
        const created = await api.createVoucher(bookId, payload, createKey.value)
        // **回放**：服务端说这把键已经用过了 —— 上一次其实**落库了**，
        // 只是响应在路上丢了（审计 §3.28）。它回给你的是**上一次**那张凭证，
        // 里面存的是**上一次**那一版内容；而用户此刻在屏幕上又敲了几个字。
        //
        // 所以这里补一次 PUT，把当前内容收敛到那张凭证上 ——
        // 不补的话，这几下敲的就白敲了（库里还是旧内容，界面却显示新的）。
        //
        // 补完之后 ``saved`` 取 PUT 的响应：凭证号可能因为改期而重新发过，
        // 页面上那个只读的号得跟着换（见下面那段）。
        saved = created.replayed
          ? await api.updateVoucher(bookId, created.id, payload)
          : created
      } else {
        saved = await api.updateVoucher(bookId, voucherId.value, payload)
      }
      voucherId.value = saved.id
      // 「这一张**已经建出来了**」—— 把 id 补进 store 的记号（审计 N5）：
      // 重挂之后凭证页能认出「这不是一张新凭证，是刚才建的那张」，
      // 下一趟自动保存走的是 PUT（改那张），不会再 POST 一张新的出来。
      if (wasNew) {
        store.createdVoucher = { bookId, id: saved.id, createKey: createKey.value }
      }
      // 换成**服务端刚给的新令牌** —— 令牌是一次性的：
      // 留着旧的，下一趟保存就会把自己判成「版本过期」。
      if (saved.version) voucherVersion.value = saved.version
      // **服务端可能给这张重新发过号**：改了日期跨到另一个期间，凭证号是按期间
      // 唯一的，号段也就跟着换了 —— 服务端按目标期间重新发了一个（该期最大号 + 1）。
      // 页面上那个只读的凭证号必须跟着换，否则人看着「记0009」、库里是「记0012」，
      // 而且下一趟自动保存会把**旧的号**发回去，后端当场回一句
      // 「凭证号不能手工修改」，人完全看不懂是自己改的日期惹的祸。
      if (saved.voucher_no && saved.voucher_no !== form.voucher_no) {
        form.voucher_no = saved.voucher_no
      }
      // 期间也一样：跨期之后页眉那个「会计期间」得跟着换，
      // 辅助记账的余额、打印时取的期间才跟这张是一期的。
      if (saved.period_id && saved.period_id !== voucherPeriodId.value) {
        voucherPeriod.value = store.periods.find((p) => p.id === saved.period_id) || null
        voucherPeriodId.value = saved.period_id
      }
      lastSaved.value = sentSig
      failedSig.value = ''
      savedAt.value = new Date().toTimeString().slice(0, 5)
      formBookId.value = bookId
      if (wasNew) {
        // 地址栏换成 /vouchers/{id}：这时候刷新页面不会变成另一张空凭证。
        // 只动地址栏、**不走 router** —— voucher-new 和 voucher-edit 是两条路由记录，
        // router.replace 会把组件整个重挂一遍：正在填的表单、光标、绿行全没了。
        // replaceState 不触发 hashchange，router 内部状态原样保留。
        history.replaceState(history.state, '', `#/vouchers/${saved.id}`)
        // 标签栏那条记的还是 /vouchers/new：只改地址栏不同步它的话，
        // 关掉标签再点回来会跳到**另一张新的空凭证**，刚录好的那张像丢了。
        // tabs.open 认的是 (name, path) —— 名字还是原来那个，path 换成存下来的地址。
        tabs.open({ name: 'voucher-new', path: `/vouchers/${saved.id}` })
        voucherPeriod.value = store.periods.find((p) => p.id === saved.period_id) || null
        voucherPeriodId.value = saved.period_id
        // 顺位已经落库，这张从「待插入」变成普通凭证：说明条收起来，
        // 再改内容走的是 update，不会把后面那叠号再顶一次
        insertCtx.value = null
        await loadNeighbour()
      }
      // 原来这里有 `if (!quiet) { ElMessage.success('已保存'); router.push('/vouchers') }`，
      // 那是「手动保存」的老路：存好了说一声、把人送回列表。走不到，已经删掉 ——
      // 自动保存存完必须把人**留在这张凭证上**接着录，一存完就踢回列表，
      // 等于逼着人每次都重新找回来。页脚那句「已自动保存 HH:MM」（savedAt）才是准信。
      return true
    } catch (e) {
      // 这一版没存上，记下来：同一个签名不再自动重试（见 failedSig）。
      // 错误本身已经由 http 拦截器弹过了，这里不重复报一遍。
      failedSig.value = sentSig
      // **409 = 版本冲突**：这一版的令牌作废了，后面自动保存会一路 409。
      // 掐掉它，并**多说一句**该做什么（拦截器那句是给「为什么失败」的，
      // 这句是给「接下来怎么办」的）。
      if (e?.response?.status === 409) {
        versionConflict.value = true
        ElMessage.warning('这张凭证已被他人改动，本次修改没有保存。请刷新页面确认当前内容后再改。')
      }
      return false
    } finally {
      saving.value = false
    }
  })()
  savingPromise = p
  const ok = await p
  savingPromise = null
  return ok
}

/**
 * 借贷一平就自动落库。半张凭证加一个刷新 = 白填十分钟，会计一天要录几十张，
 * 没人会记得每张都去按一下保存。改动停手 0.7 秒再存，免得每敲一个数字都打一趟接口。
 */
const AUTO_SAVE_DELAY = 700
let autoSaveTimer = null
/** 正在路上那趟保存。离开页面前要等它回来，不然同一张凭证会被 POST 两遍。 */
let savingPromise = null
async function autoSave() {
  // 上一趟还在路上：改完的东西还没发出去，等它回来再走一趟，别丢
  if (saving.value) {
    autoSaveTimer = setTimeout(autoSave, AUTO_SAVE_DELAY)
    return
  }
  // 这一版刚才存失败了 —— 同一版不再自动重试（failedSig 的本意）。
  //
  // 以前这道判断**只在 watch 里**：已经排好的那趟定时器不看它。
  // 于是「请求发出去了、响应丢了」这一版会被**再发一次**，
  // 在服务端就是第二张凭证（审计 §3.28）。
  if (entrySig.value === failedSig.value) return
  await save()
}
watch([entrySig, balanced], () => {
  clearTimeout(autoSaveTimer)
  if (readonly.value || !balanced.value) return
  // 一条能落库的分录都还没有 → 不是「有改动没存上」，是「还没录完」。
  // 空表时 balanced 本来就是 true（借贷都是 0），光看它会把空表放进来去弹报错。
  if (!hasCompleteEntry.value) return
  if (entrySig.value === lastSaved.value) return    // 跟刚落库的那一版一模一样，不用再存
  if (entrySig.value === failedSig.value) return   // 这一版刚才存失败了，别反复弹同一个错
  autoSaveTimer = setTimeout(autoSave, AUTO_SAVE_DELAY)
})

/**
 * 离开之前把没落库的改动补上。
 *
 * 这一页**没有保存按钮**（工具栏上只有返回/翻页/插入/增加/打印），
 * 自动保存是唯一的落库路径 —— 于是「停手 700ms 之内点返回、关页签、
 * 切账套、切期间」这一串动作，改动就永远丢了，而屏幕上没有任何提示。
 *
 * 只在「确实有能落库的改动」时才发请求：还没录平的不是没存上的改动，
 * 是还没录完 —— 为这个拦住人不让人走是本末倒置。
 *
 * 返回 true = 没有待补的改动（或补上了）；false = 有改动但没存上。
 *
 * **必须 `await`**：save() 是 async，返回的是 Promise。
 * 原来这里写的是 `save() === true` —— 拿一个 Promise 对象跟 `true` 比，
 * **恒为假**：于是「改动已经存上了」也被报成「没存上」，
 * 离开凭证页时弹出那个红框（"这一张还有改动没能存上，离开就会丢掉"），
 * 而保存其实早就成功了；拆分凭证则被**永久误拦**，提示语还是错的。
 * await 之后拿到的是 save() 真正 resolve 出来的那个布尔值。
 */
async function flushIfDirty() {
  if (readonly.value) return true
  if (savingPromise) await savingPromise        // 上一趟还在路上，先等它回来
  if (entrySig.value === lastSaved.value) return true
  if (!balanced.value) return true
  if (!hasCompleteEntry.value) return true
  return await save()
}

/** 真有东西没存上吗？浏览器的关页签/刷新提示问的是这一句。 */
function hasUnsavedChanges() {
  if (readonly.value) return false
  if (entrySig.value === lastSaved.value) return false
  if (!balanced.value) return false
  return hasCompleteEntry.value
}

/** 关页签 / 刷新：拦不住就至少让浏览器自己问一句「确定要走？」 */
function onBeforeUnload(e) {
  if (!hasUnsavedChanges()) return
  e.preventDefault()
  e.returnValue = ''
}

/** 组件被换掉（切账套 / 切期间时 MainLayout 的 viewKey 变化会直接重挂载）：
 *  这一条路没有路由跳转，`onBeforeRouteLeave` 根本不触发，只能在这儿补一趟。
 *  换账套时表单刚被 resetForm 抹平、什么都不脏，这里自然什么都不做。 */
onBeforeUnmount(() => {
  clearTimeout(autoSaveTimer)
  onWindowResize.cancel()          // 还挂着的那一帧别再对着已卸载的 DOM 跑
  gridResizeObserver?.disconnect()  // 别让观察器替已卸载的 DOM 记着尺寸
  gridResizeObserver = null
  window.removeEventListener('resize', onWindowResize)
  window.removeEventListener('beforeunload', onBeforeUnload)
  // **这一趟必须挂出去**，不能就这么丢掉（审计 §3.24）。
  //
  // 切期间时 viewKey 变了 → 旧组件先卸载、新组件马上挂载。
  // 以前这里 fire-and-forget：PUT 在路上，新组件的 GET 已经在服务端排上队了 ——
  // GET 先回，新页面显示的是**改动前**的值；用户在新页上再改一次，
  // 自动保存 PUT 的是**整张表单**，前一版改动被无声覆盖。
  //
  // 现在把 promise 挂到跨组件的盒子里（utils/voucherFlush.js），
  // 新组件 onMounted 先 await 它再 load()。
  if (formBookId.value === store.bookId) setPendingFlush(flushIfDirty())
})

/**
 * 摘要列宽跟着窗口变，折行位置跟着变，双行的框得重新量。
 *
 * resize 一秒能来 60 次，而每量一个框都是「写 height:auto → 读 scrollHeight →
 * 再写 height」，中间要把布局**同步刷一遍**。一张凭证 8~20 个框，一次 resize
 * 就是 8~20 遍强制重排 —— 拖个窗口整页跟着卡，跟这张凭证本身多重没关系。
 * 两处一起治：
 *   1. 按帧合并（rafThrottle）：两次绘制之间缩多少次，量一趟就够；
 *   2. 同一帧里**先给所有框写 auto，再一起读 scrollHeight，最后一起写回** ——
 *      布局只被逼刷一次，而不是每个框刷一遍。
 */
function reAutosizeSums() {
  cachedRowH = 0          // 窗口变了行高未必变，但量过一次就别信旧的，下回按新的算
  syncGridWidth()
  const els = []
  for (const el of sumRefs.values()) if (el) els.push(el)
  if (!els.length) return
  sumArmed = false                       // 已经动手改了：整格选中到此为止
  for (const el of els) el.style.height = 'auto'
  const needs = els.map((el) => el.scrollHeight)
  els.forEach((el, i) => autosizeSum(el, needs[i]))
}
const onWindowResize = rafThrottle(reAutosizeSums)

/**
 * 列宽的「补测」：页面从**没有布局盒**变成有盒子的那一刻，重量一次。
 *
 * 挂载后那趟 syncGridWidth() 只跑一次，而它跑的时候页面可能还量不到宽度
 * （骨架撤下之前，整棵子树没有盒子）。这一趟补测就是等盒子回来再量一次：
 * 分录区的尺寸一变（骨架撤下、窗口缩放、侧栏收起）就重新量。
 *
 * 观察的是 .entry-scroll 自己 —— 它的高度由内容决定，
 * 宽度由外层给；改 --vg-grid-width 不会反过来改它的大小，不会有自激循环。
 */
let gridResizeObserver = null

/**
 * 离开这一页前把改动补上；补不上就问一句，不让人无声无息地丢掉半张凭证。
 *
 * 取消确认框会 reject（「cancel」），不接住就是一个未处理的 rejection ——
 * 控制台一片红，还可能连带吞掉后面真正的报错。
 */
onBeforeRouteLeave(async () => {
  if (await flushIfDirty()) return true
  return ElMessageBox.confirm('这一张还有改动没能存上，离开就会丢掉。仍然离开？',
    '改动未保存', { type: 'warning', confirmButtonText: '仍然离开', cancelButtonText: '留下继续改' })
    .then(() => true)
    .catch(() => false)
})

onMounted(async () => {
  window.addEventListener('resize', onWindowResize)
  window.addEventListener('beforeunload', onBeforeUnload)
  if (typeof ResizeObserver !== 'undefined' && entryScroll.value) {
    gridResizeObserver = new ResizeObserver(() => syncGridWidth())
    gridResizeObserver.observe(entryScroll.value)
  }
  if (!store.periodId) return
  formBookId.value = store.bookId   // 这张表单从这一刻起属于这个账套
  // 切期间/切账套时，旧组件的「离开前那一趟 flush」可能还在路上 ——
  // **先等它回来再取数**，否则新页面拿到的是改动前的值（审计 §3.24）。
  const pending = takePendingFlush()
  if (pending) { try { await pending } catch { /* flush 自己会报 */ } }
  await load()
  await nextTick(() => {
    syncGridWidth()
  })
})
/**
 * 切账套 / 切期间 / 翻凭证都会重载表单。
 *
 *  换**账套**时先整张抹平：地址栏里的 id 是上一个账套的凭证号，留着它
 *  就是拿新账套的地址去动旧账套的数据，而且上一张的表单还摊在屏幕上。
 *  换**期间**或**翻凭证**属于同一个账套，先把这一张没落库的改动补上再重载。
 *
 *  前一张/后一张只是换 route.params，组件不会重新挂载 —— 必须盯着 id。
 */
// 换年度也要重取科目面板：另一个年度是**另一张**科目表
watch(() => [store.bookId, store.periodId, store.currentYear, route.params.id], async () => {
  if (store.bookId !== formBookId.value) {
    resetForm()
    formBookId.value = store.bookId
    await load()
    return
  }
  await flushIfDirty()
  await load()
  await nextTick(() => {
    syncGridWidth()
  })
})
</script>

<style scoped>
.voucher-head { display: flex; gap: 24px; align-items: center; margin-bottom: 12px; font-size: 13px; }


/* 这一页自己占满内容区：工具条、凭证卡片、卡片里的页脚各占自己那份，
   剩下多少全给分录区。外层 .content 因此不用滚 —— 一屏之内只有 .entry-scroll 滚。 */
.voucher-page { height: 100%; display: flex; flex-direction: column; min-height: 0; }
.voucher-page > .card { flex: 0 1 auto; min-height: 0; display: flex; flex-direction: column; }

/* 分录格。左右各空出一条：左边 48px 放每行的 ⊕ / ＋，右边 52px 放每行的 ⊗。
   它们都绝对定位在**自己那一行**的外头，哪一行变绿才冒出来 ——
   所以不用为按钮单开一列，表格右边那条竖线也就没有了。

   这里头有个反直觉的点：两条带子**不能自己** visibility: hidden。
   带子是绝对定位在行外面的（right:100% / left:100%），本身没有背景，看着像空的；
   但它一藏就跟着不接鼠标了 —— 光标停在行左侧那条空带子上时，命中的是
   那一行外面的留白而不是任何一行，hoverRow 置空，绿行就退回「上一次录入的那行」
   纹丝不动。用户看到的就是：鼠标明明压着第二行绿着第一行。

   所以：**带子永远接鼠标，藏的只是里面的按钮**。带子没背景，藏不藏都一个样；
   按钮用 visibility 藏，既不出现在无障碍树里，也收不到点击。

   留白（48 / 52）挂在 .entry-scroll 的 padding 上；带子绝对定位在每行外侧，
   属于滚动内容，随分录行一起移动。 */
.entry-wrap {
  --vg-line: #c0c4cc;
  --vg-active: #f2faf2;
  --vg-total: #fffbf7;
  /* 列宽：序号固定，两列金额各 210px（11 个数位槽），剩下的全给摘要与科目。

     **两列文本列的下限是 0，不是 170 / 230**（2026-10-31 改）。

     为什么：这里的下限**不是**「列窄到多少就不好看了」，而是**硬底线** ——
     五列下限加上四条 1px 的缝，一旦超过容器实得宽度，grid 就把多出来的那截
     **放到格子外面**。而容器宽度会被纵向滚动条吃掉一截：
     macOS 是覆盖式滚动条（不占宽），Windows / Linux 是经典滚动条（占 15~22px，
     还要随显示缩放放大）。于是同一张凭证在 macOS 上好好的，换到 Windows 上：

       列下限合计 876 + 4px 缝 = 880px  >  行宽 894 - 22px 滚动条 = 872px

     多出来的 8px 全跑到**贷方金额格**上 —— 那一格是不透明的（`background: #fff`），
     它正好盖住 `.vgrid` 那条 1px 的右边框线。结果是**贷方金额格右框线整条消失**，
     而表头与合计两段（不在滚动区里，宽度不受滚动条影响）照旧有线 ——
     一张表上，同一条框线时有时无。

     账套磁贴那处踩过同一个坑（`tests/book_grid_columns.test.mjs` 钉着）：
     minmax 的下限必须 ≤ 容器实得宽度，差 2px 就会静默换一种排法。

     改成 0 之后：空间不够时**由文本列让位**（摘要/科目本来就是文字，窄一点
     只是少显示几个字），金额列的 210px 下限**一个字不动** ——
     那 11 个数位槽的宽度是记账格式的一部分，不能被挤。
     算式与守卫见 `tests/voucher_grid_columns.test.mjs`。

     渲染不变：行宽够时（≥ 约 918px，即 894 + 各列下限之外的余量）
     两列文本列拿到的宽度与改之前**逐像素相同** —— 因为今天的分配结果
     本来就是「金额列各 210px 固定，剩下 414px 按 1 : 1.4 分给摘要与科目」。 */
  --vg-cols: 56px minmax(0, 1fr) minmax(0, 1.4fr) minmax(210px, 1fr) minmax(210px, 1fr);
  --vg-row-h: 68px;
  --vg-grid-width: calc(100% - 100px);
  flex: 0 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: #fff;
  border: 1px solid var(--vg-line);
  border-radius: 2px;
}
.vg-act {
  position: absolute;
  right: 100%;
  /* top: -1px：绝对定位是相对 padding box 算的，行顶上那条 1px border 在它上头。
     不探这一像素，光标压在这一行的上边线上，两条带子就都收不到 */
  top: -1px;
  bottom: 0;
  width: 48px;                 /* 正好等于 entry-scroll 的左内边距：整条带子都是这一行的 */
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
}
.vg-act > * { visibility: hidden; }   /* 没变绿的行，这一对按钮不占地方 */
.vg-row.is-on .vg-act > * { visibility: visible; }
.vg-act .el-button + .el-button { margin-left: 0; }
.g-ins { color: #fff; background: #67c23a; border-color: #67c23a; font-weight: 700; }
.g-ins:hover { background: #85ce61; border-color: #85ce61; color: #fff; }
.g-add { color: #fff; background: #409eff; border-color: #409eff; font-weight: 700; }
.g-add:hover { background: #66b1ff; border-color: #66b1ff; color: #fff; }

/* 分录区优先按内容高度排布，最多显示五个完整分录行；更多行只在这里滚动。
   五行视窗使最后一行自然停在锚点处，不需要在表格里塞一大块空白尾垫。 */
.entry-scroll {
  flex: 0 1 auto;
  min-height: 0;
  position: relative;            /* 行的 offsetParent 就在这儿，量位置才准 */
  overflow-y: auto;
  overflow-x: hidden;
  max-height: calc((var(--vg-row-h) + 1px) * 5);
  padding: 0 52px 0 48px;
  background: #fff;
  /* 这里**故意不写** overscroll-behavior。
     写了（contain / none 都算）Chromium 就把这一盒里的 Home / End 弄坏：
     格子里的 textarea 选中一段文字后按 End，选区不收、光标也不动 ——
     「选着按 End 收掉选区」这条路整个断掉（Chromium 的老毛病：
     文本框在带 overscroll-behavior 的滚动盒里，光标位置算不准）。
     默认的 auto 没这个毛病；而这一页外层 .content 本来也不需要滚，
     滚到头接着往外传不传都无所谓 —— 为了一件不要紧的事，
     坏掉一个常用的键，不划算。 */
}

/* 凭证格：表头、分录行、合计三段共用的列宽只写这一份。
   1px 的 gap 当格子线 —— 不用给每个单元格描边，行高也就随 grid-auto-rows 走。
   三段各自是一行 grid，共用 entry-wrap 上的列宽变量。 */
.vgrid {
  min-width: 0;
  width: 100%;
  background: var(--vg-line);
  border-left: 1px solid var(--vg-line);
  border-right: 1px solid var(--vg-line);
  /* 这里原来有 overflow: hidden：会把贴在行左边的 ⊕ / ＋ 一起裁掉 */
}

/* 一行一个盒子。原来是 display: contents —— 行没有盒子，鼠标扫过去整行不会变色，
   也没有地方挂「这一行的动作按钮」。现在行自己摆格子（列宽跟外层同一份），
   行高、竖线跟以前一模一样，多出来的只是行有了自己的命中区域。 */
.vg-row {
  display: grid;
  grid-template-columns: var(--vg-cols);
  grid-auto-rows: var(--vg-row-h);
  gap: 0 1px;                    /* 竖线走列间距；横线走行的 border-top */
  background: var(--vg-line);
  position: relative;
}
/* 行与行之间那 1px 缝原本属于外层 grid —— 光标正好卡在缝上，两行都收不到鼠标，
   绿行就赖在原地不动，看着就是「鼠标压着第二行，第一行绿着」。
   现在把行间距收成 0，横线改由每一行自己的 border-top 画：行与行严丝合缝，
   每一个像素都有主人，谁也不跟谁抢。

   （走过弯路：先试过 ::before 往外探一像素，透明盒子盖在格子上，
   格子里的输入框就再也收不到点击；又试过负边距让两行各探一像素，
   缝是补上了，但缝上那一像素归谁全看谁在 DOM 里靠后，不该有的运气。） */
.vg-row:not(.vg-head) {
  box-sizing: border-box;
  border-top: 1px solid var(--vg-line);
}
/* 表头固定在滚动区上方；列宽扣掉滚动条后仍与分录、合计逐像素对齐。 */
.vg-head {
  flex: 0 0 auto;
  width: var(--vg-grid-width);
  margin-left: 48px;
  grid-template-rows: 48px 40px;
  background: var(--vg-line);
  border: 1px solid var(--vg-line);
  border-bottom: 0;
  border-radius: 2px 2px 0 0;
}
/* 行间距收成 0 了，表头里那两行之间得自己补一条，不然「借方金额」和
   「亿千百十万」会贴在一起 */
.th-amt { border-bottom: 1px solid var(--vg-line); }
.vg-cell { background: #fff; min-width: 0; min-height: 0; display: flex; align-items: center; }

/* 表头：序号/摘要/会计科目竖跨两行，借贷金额上排方向、下排亿…分 */
.th { justify-content: center; font-weight: 600; font-size: 14px; }
.th-idx, .th-sum, .th-subj { grid-row: 1 / 3; }
.th-amt { grid-row: 1; }
.th-units { grid-row: 2; }
.th-idx { grid-column: 1; } .th-sum { grid-column: 2; } .th-subj { grid-column: 3; }
.th-amt.d { grid-column: 4; } .th-amt.c { grid-column: 5; }
.th-units.d { grid-column: 4; } .th-units.c { grid-column: 5; }

/* 分录行 */
.td-idx { grid-column: 1; justify-content: center; color: var(--cwq-muted); }
.td-sum { grid-column: 2; }
.td-subj { grid-column: 3; position: relative; }

/* 科目余额：贴在格子左下角，字号比科目名小一号、灰一档。
   绝对定位而不是并排放 —— 并排会把 SubjectPicker 挤窄（它靠 flex: 1 铺满整格），
   科目名一长就得缩成省略号，那才是真干扰。
   pointer-events: none：它只是给人看的一个数，别挡住这一格的点击（点格子要能选科目）。 */
.subj-balance {
  position: absolute;
  left: 12px;
  bottom: 8px;
  font-size: 12px;
  line-height: 1;
  font-variant-numeric: tabular-nums;
  color: var(--cwq-muted);
  pointer-events: none;
}

/* 前面有没写完的分录时，这几格一起不开张：淡一点 + 指针变禁止，点一下说清差在哪。
   禁用的 input 收不到点击事件（浏览器压根不给它派发），所以还得把指针穿透掉，
   让点击落到格子上，提示才出得来 —— 否则点半天没反应，人只会以为页面卡了 */
.td-amt { display: block; }
.is-blocked { cursor: not-allowed; }
/* 挂到了上级科目：红框 + 红字。这一格不改掉，整张凭证都存不下去 */
.is-bad { box-shadow: inset 0 0 0 2px #f56c6c; }
.is-bad :deep(.sp-input) { color: #d93025; font-weight: 600; }
/* 开不开张只决定「能不能写」，不决定「长什么样」：
   前面还有没写完的分录时，后面几行不开张，可它们跟别的格子长得一模一样。
   褪色看着像「这几格的数据丢了/坏了」，其实只是还没轮到你写 ——
   跟已记账凭证那个道理一样（见 readonly_styling）：锁的是写入路径，不是外观。
   点一下照旧说清差在哪，够用了，不必靠变淡来暗示。 */
.is-blocked .cell-input,
.is-blocked :deep(.sp-input),
.is-blocked :deep(.amt-input) { pointer-events: none; }
.td-amt.d { grid-column: 4; } .td-amt.c { grid-column: 5; }

/* 合计行固定在滚动区下方：录到第几十行，借贷合计一直在眼前。 */
.vg-total {
  flex: 0 0 auto;
  width: var(--vg-grid-width);
  margin-left: 48px;
  border: 1px solid var(--vg-line);
  border-top: 0;
  border-radius: 0 0 2px 2px;
}
.vg-total .td-sumcell {
  grid-column: 1 / 4;
  padding: 0 16px;
  font-weight: 600;
  font-size: 14px;
  justify-content: flex-end;  /* 格子是 flex 容器（见 .vg-cell），摆位置的是它，不是 text-align */
}
.vg-total .td-total { display: block; }
.vg-total .td-total.d { grid-column: 4; } .vg-total .td-total.c { grid-column: 5; }
.vg-total .vg-cell { background: var(--vg-total); }

/* 当前录入行：淡绿底，眼睛跟着手走。鼠标扫过的那一行同样算当前行 */
.vg-row.is-on .vg-cell { background: var(--vg-active); }
/* 删除 ⊗：挂在这一行的右边外面，跟左边那对 ⊕ / ＋ 一个待遇 */
.vg-del {
  position: absolute;
  left: 100%;
  top: -1px;                   /* 同 .vg-act */
  bottom: 0;
  width: 52px;                 /* 同上，正好铺满右侧留白 */
  display: flex;
  align-items: center;
  justify-content: center;
}
.vg-del > * { visibility: hidden; }    /* 同上：带子接鼠标，⊗ 本体才藏 */
.vg-row.is-on .vg-del > * { visibility: visible; }

/* 格里就是一个普通输入框，不带边框，才像一张填在纸上的凭证 */
/* textarea：高度由 autosizeSum 按内容写进 style，这里只给初值（一行）。
   line-height 必须写成固定值 —— autosizeSum 要靠它算「两行有多高」，
   写成 normal 量不出来。resize:none 是不要右下角那个拖拽把手：
   那是给固定尺寸的框准备的，这一格高度由内容说了算。 */
.cell-input {
  width: 100%;
  height: 22px;
  border: 0;
  outline: none;
  background: transparent;
  padding: 0 12px;
  font: inherit;
  font-size: 14px;
  line-height: 22px;
  color: var(--cwq-text);
  resize: none;
  overflow-y: hidden;
  display: block;
}
.cell-input::placeholder { color: #c0c4cc; }

/* 自动保存的报平安：绿，但比「借贷平衡」淡一点 —— 平衡是状态，保存是动作 */
/* 自动保存的「准信」：页脚里最该被一眼扫到的一句，字号抬到 13px 并加粗。
   12px 的小绿字混在页脚一排灰字里，录凭证的人根本不会注意到它，
   那这条准信等于没有 —— 存没存进去、存的哪一刻，全靠它说。 */
.saved-tag { color: #529b2e; font-size: 13px; font-weight: 700; }

.row-del { font-size: 17px; color: #d93025; cursor: pointer; }
.row-del.is-off { color: #dcdfe6; cursor: not-allowed; }

.vfoot {
  flex: 0 0 auto;                /* 页脚不参与压缩：分录再多也得留住借贷差额 */
  display: flex;
  justify-content: flex-end;      /* 页脚只剩状态区（自动保存/借贷平衡），一律靠右 */
  align-items: center;
  gap: 16px;
  margin-top: 14px;
  font-size: 13px;
}
.foot-right { display: inline-flex; align-items: baseline; gap: 22px; }
.foot-right b { font-variant-numeric: tabular-nums; color: var(--cwq-text); }

/* 工具条右边那四颗动作按钮（插入 / 增加 / 拆分 / 打印）。
   Element Plus 给「相邻的按钮」加了 12px 左边距（.el-button + .el-button），
   工具条自己又有 8px 的缝 —— 两颗之间就是 20px，一排四颗占掉四百多像素，
   看上去是散着的（用户 2026-10-23：「这四个按钮间距缩小一点」）。
   收进一个自己的小分组：左边距去掉，只留 4px 的缝。 */
.tb-acts { display: flex; align-items: center; gap: 4px; }
.tb-acts :deep(.el-button + .el-button) { margin-left: 0; }

/* 打印预览与打印都已改走 PrintDialog，页内不再留任何打印用样式：
   预览由后端渲染的那份 HTML 负责，页内再留一套就是两套版式，
   改了一处另一处不动，打出来才发现走错了分支。 */
</style>
