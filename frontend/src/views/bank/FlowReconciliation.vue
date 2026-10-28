<template>
  <div class="page flow-page">
    <div class="flow-head">
      <div>
        <h2>流水对账</h2>
        <p>
          上传网银导出的对账单，把它绑到具体的现金科目（1002 下面的子科目）上；
          流水可以自动生成凭证，发票的结算科目也会自动跟到对应账户。
        </p>
      </div>
      <div class="flow-head-actions">
        <el-button :disabled="!store.canWrite" @click="loadAll">
          <el-icon><Refresh /></el-icon>刷新
        </el-button>
      </div>
    </div>

    <el-tabs v-model="tab" class="flow-tabs" @tab-change="onTabChange">
      <!-- ============================================================ 对账单 -->
      <el-tab-pane label="对账单" name="statements">
        <el-card shadow="never" class="flow-upload-card">
          <template #header>
            <div class="section-head">
              <span>上传对账单</span>
              <span class="muted">
                支持 Excel（xls/xlsx）、网页版表格、CSV 与 PDF；
                格式按**文件内容**识别，不看扩展名
              </span>
            </div>
          </template>
          <el-form :model="uploadForm" label-width="92px" class="flow-upload-form">
            <!-- **gutter 只能是数字。**
                 原来这里写的是 ``:gutter="'0 28'"``（想表达「横向 0、纵向 28」），
                 而 el-row 的 gutter 只认 Number —— 传字符串会算出 NaN，
                 margin 变成 ``NaNpx`` 被浏览器丢掉，于是**两栏贴着、窄屏上也没有
                 28px 的竖间距**，控制台还一直报 prop 类型错。
                 横向不要间距就写 0，纵向的间距由下面的 CSS 给。 -->
            <el-row :gutter="0" class="flow-upload-row">
              <el-col :xs="24" :sm="10">
                <el-form-item label="现金科目">
                  <el-select
                    v-model="uploadForm.account_subject_id"
                    :disabled="!store.canWrite"
                    filterable clearable :persistent="false"
                    placeholder="这份流水记到哪个账户"
                  >
                    <el-option
                      v-for="s in cashSubjects"
                      :key="s.id"
                      :label="subjectLabel(s)"
                      :value="s.id"
                    />
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :xs="24" :sm="14">
                <el-form-item label="对账单">
                  <el-upload
                    :auto-upload="false"
                    :show-file-list="false"
                    accept=".xls,.xlsx,.csv,.html,.htm,.pdf"
                    :disabled="!store.canWrite"
                    :on-change="onFile"
                  >
                    <el-button :disabled="!store.canWrite">
                      <el-icon><Upload /></el-icon>选择文件
                    </el-button>
                  </el-upload>
                </el-form-item>
              </el-col>
            </el-row>
            <el-form-item>
              <el-button
                type="primary"
                :disabled="!store.canWrite || !canUpload"
                :loading="uploading"
                @click="doUpload"
              >
                <el-icon><Check /></el-icon>上传并解析
              </el-button>
              <span v-if="pickedFile" class="muted picked">{{ pickedFile.name }}</span>
            </el-form-item>
          </el-form>
        </el-card>

        <el-card shadow="never">
          <template #header>
            <div class="section-head">
              <span>对账单台账</span>
              <span class="muted">
                「差额」= 对账单期末余额 − 账面余额（含未记账）；
                差额能被未入账流水解释时，下面会写清楚
              </span>
            </div>
          </template>
          <el-table :data="statements" v-loading="loading" empty-text="还没有上传过对账单">
            <el-table-column label="账户" min-width="170">
              <template #default="{ row }">
                <div>{{ row.account_subject_code }} {{ row.account_subject_name }}</div>
                <div class="muted small">{{ row.account_name || row.account_no || '—' }}</div>
              </template>
            </el-table-column>
            <el-table-column label="文件" min-width="180">
              <template #default="{ row }">
                <div>{{ row.filename }}</div>
                <div class="muted small">{{ row.container_label }} · {{ row.row_count }} 行</div>
              </template>
            </el-table-column>
            <el-table-column label="期间" width="150">
              <template #default="{ row }">
                {{ row.period_start || '—' }} ~ {{ row.period_end || '—' }}
              </template>
            </el-table-column>
            <el-table-column label="收支合计" width="180">
              <template #default="{ row }">
                <div>收 {{ money(row.income_total) }}</div>
                <div>付 {{ money(row.expense_total) }}</div>
              </template>
            </el-table-column>
            <el-table-column label="期末余额" width="120">
              <template #default="{ row }">{{ money(row.closing_balance) }}</template>
            </el-table-column>
            <el-table-column label="账面余额" width="120">
              <template #default="{ row }">
                <span v-if="row.reconcile?.available">{{ money(row.reconcile.book_balance) }}</span>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column label="差额" width="150">
              <template #default="{ row }">
                <span v-if="row.reconcile?.available" :class="diffClass(row.reconcile)">
                  {{ money(row.reconcile.difference) }}
                </span>
                <span v-else class="muted small">{{ row.reconcile?.reason || '—' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="150">
              <template #default="{ row }">
                <el-tag :type="parseType(row.parse_status)" size="small">
                  {{ parseText(row.parse_status) }}
                </el-tag>
                <div v-if="row.pending_count" class="muted small">
                  {{ row.pending_count }} 行待确认科目
                </div>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="230" fixed="right">
              <template #default="{ row }">
                <el-button link type="primary" @click="openRows(row)">查看流水</el-button>
                <el-button link type="primary" :disabled="!store.canWrite" @click="doReparse(row)">
                  重新解析
                </el-button>
                <el-button link type="primary" @click="doDownload(row)">下载原件</el-button>
                <el-button link type="danger" :disabled="!store.canWrite" @click="doDelete(row)">
                  删除
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <!-- 解析诊断 -->
        <el-card v-if="current" shadow="never" class="flow-diag-card">
          <template #header>
            <div class="section-head">
              <span>解析诊断 · {{ current.filename }}</span>
              <span class="muted">
                认错了列不要紧：在这里手工指定一次，并「记住这个格式」，
                下次同一家银行的文件自动套用
              </span>
            </div>
          </template>
          <div class="diag-grid">
            <div><span class="muted">容器</span> {{ current.container_label }}</div>
            <div>
              <span class="muted">表头行</span>
              {{ current.diagnosis?.header_row != null ? `第 ${current.diagnosis.header_row + 1} 行` : '—' }}
            </div>
            <div><span class="muted">指纹</span> {{ current.diagnosis?.fingerprint || '—' }}</div>
            <div><span class="muted">解析</span> {{ parseText(current.parse_status) }}</div>
          </div>
          <div v-if="current.parse_message" class="diag-notes">
            <div v-for="(note, i) in noteList" :key="i" class="diag-note">{{ note }}</div>
          </div>
          <div class="diag-mapping">
            <span class="muted">列映射</span>
            <el-tag v-for="(col, field) in current.diagnosis?.column_mapping || {}"
                    :key="field" size="small" class="mapping-tag">
              {{ fieldLabel(field) }} = 第 {{ Number(col) + 1 }} 列
            </el-tag>
          </div>

          <el-collapse v-model="diagPanels" class="diag-collapse">
            <el-collapse-item name="grid" title="网格预览（前 20 行）与手工指定列">
              <el-table :data="gridRows" size="small" border>
                <el-table-column type="index" width="46" />
                <el-table-column
                  v-for="(col, index) in gridColumns"
                  :key="index"
                  :label="`第 ${index + 1} 列`"
                  min-width="120"
                >
                  <template #default="{ row }">
                    <div class="grid-cell">{{ row[index] }}</div>
                    <el-select
                      v-model="manualMapping[index]"
                      size="small"
                      placeholder="（不认）"
                      clearable
                      class="mapping-select"
                    >
                      <el-option
                        v-for="f in FIELD_OPTIONS"
                        :key="f.value"
                        :label="f.label"
                        :value="f.value"
                      />
                    </el-select>
                  </template>
                </el-table-column>
              </el-table>
              <div class="diag-actions">
                <el-button
                  type="primary"
                  :disabled="!store.canWrite || !manualMappingCount"
                  :loading="reparsing"
                  @click="applyManualMapping"
                >
                  按指定重新解析
                </el-button>
                <el-button
                  :disabled="!store.canWrite || !manualMappingCount"
                  @click="rememberFormat"
                >
                  记住这个格式
                </el-button>
                <span class="muted small">
                  手工指定的列会**整体替换**自动映射，不做逐字段合并
                </span>
              </div>
            </el-collapse-item>
            <el-collapse-item name="profiles" title="已记住的格式画像">
              <el-table :data="profiles" size="small" empty-text="还没有记住任何格式">
                <el-table-column label="指纹" prop="fingerprint" width="200" />
                <el-table-column label="列映射" min-width="320">
                  <template #default="{ row }">
                    <el-tag
                      v-for="(col, field) in row.column_mapping"
                      :key="field"
                      size="small"
                      class="mapping-tag"
                    >
                      {{ fieldLabel(field) }} = 第 {{ Number(col) + 1 }} 列
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="命中" width="80" prop="hit_count" />
                <el-table-column label="操作" width="80">
                  <template #default="{ row }">
                    <el-button link type="danger" :disabled="!store.canWrite" @click="doDeleteProfile(row)">
                      删除
                    </el-button>
                  </template>
                </el-table-column>
              </el-table>
            </el-collapse-item>
          </el-collapse>
        </el-card>
      </el-tab-pane>

      <!-- ============================================================ 流水明细 -->
      <el-tab-pane label="流水明细" name="rows">
        <el-card shadow="never">
          <template #header>
            <div class="section-head">
              <span>流水明细</span>
              <span class="muted">
                每行的「对账信息」（方向、对方科目、状态、操作）**默认展开**；
                嫌占地方就用行首箭头收起（不加列，不会出左右滑动条）
              </span>
            </div>
          </template>
          <div class="flow-rows-toolbar">
            <el-select v-model="activeStatementId" placeholder="选择对账单" class="flow-select" @change="loadRows">
              <el-option
                v-for="s in statements"
                :key="s.id"
                :label="`${s.filename}（${s.period_start || '?'} ~ ${s.period_end || '?'}）`"
                :value="s.id"
              />
            </el-select>
            <el-button :disabled="!activeStatementId" :loading="loadingRows" @click="loadRows">
              <el-icon><Refresh /></el-icon>刷新流水
            </el-button>
            <el-button
              type="primary"
              :disabled="!store.canWrite || !canCommit(plan)"
              :loading="committing"
              @click="doCommit"
            >
              生成流水凭证
            </el-button>
            <span v-if="plan" class="muted">
              可生成 {{ plan.candidate_count }} 条
              <template v-if="plan.pending_count">，待确认科目 {{ plan.pending_count }} 条</template>
              <template v-if="plan.unknown_count">，待确认方向 {{ plan.unknown_count }} 条</template>
            </span>
            <!-- **账上已有同一笔、只是记错了账户**：这几行要提醒用户，
                 否则用户根本不知道有笔钱记错了地方。
                 （行默认就是展开的，所以这里只报个数，不再做「点我展开」的动作。） -->
            <el-tag
              v-if="reclassifyCount"
              type="warning"
              effect="plain"
            >
              账上已有对应分录 {{ reclassifyCount }} 行
            </el-tag>
          </div>

          <el-table
            :data="rows"
            v-loading="loadingRows"
            size="small"
            class="flow-rows-table"
            empty-text="先选一份对账单"
            row-key="id"
            :expand-row-keys="expandedRowIds"
            @expand-change="onExpandChange"
          >
            <!-- 「对账信息」：**往下展开**，不是往右加列。
                 早先那版是加 4 个列（方向/对方科目/状态/操作），列一多表格就被
                 撑宽 → 出现**左右滑动条**，而这张表在窄屏上本来就挤 —— 于是
                 用户得一边横拖一边看，改起来很难受。
                 改成展开行后**列数恒定**，表格宽度不变，横向永远不会溢出。

                 **默认展开的是「还有活要干的行」**（方向未定、对方科目待确认、
                 还没入账）；**已入账的、被发票占用的默认收起** —— 那些行的
                 对账信息看一眼就够了，全摊开反而把要处理的那几行淹了。
                 用户手动开过的行留着（用户开过就说明他在看）。

                 **不能用 ``:default-expand-all``**：它是「全展或全不展」，
                 没有「哪些行展」这一说。要按行控制就得用
                 ``row-key`` + ``:expand-row-keys``。

                 **展开箭头一直都在**：曾经前面还挂一个「对账信息」勾选开关，
                 不勾就没有箭头 —— 于是「不看对账信息的人」和「要改对账信息的人」
                 看到的是**两张不同的表**。用户原话：「去掉勾选对账信息的开关」。
                 现在只剩一个动作：点行首箭头，往下展开/收起。 -->
            <el-table-column type="expand" width="42">
              <template #default="{ row }">
                <div class="flow-extra">
                  <div class="flow-extra-item">
                    <span class="flow-extra-label">方向</span>
                    <el-tag
                      v-if="row.direction !== 'UNKNOWN'"
                      size="small"
                      :type="row.direction === 'IN' ? 'success' : 'warning'"
                    >
                      {{ row.direction === 'IN' ? '收入' : '支出' }}
                    </el-tag>
                    <el-select
                      v-else
                      :model-value="row.direction"
                      size="small"
                      placeholder="指定方向"
                      class="flow-direction"
                      :disabled="!store.canWrite"
                      @change="(v) => doSetDirection(row, v)"
                    >
                      <el-option label="收入" value="IN" />
                      <el-option label="支出" value="OUT" />
                    </el-select>
                  </div>
                  <div class="flow-extra-item">
                    <span class="flow-extra-label">对方科目</span>
                    <div v-if="row.plan_subjects?.length" class="flow-plan">
                      <el-tag
                        v-for="item in row.plan_subjects"
                        :key="item.subject_id"
                        size="small"
                        type="info"
                        effect="plain"
                        class="flow-plan-tag"
                      >
                        {{ planSubjectText(item) }}
                      </el-tag>
                    </div>
                    <span v-else class="muted small">待确认</span>
                    <!-- **已入账的对方科目由凭证定死** —— 所以那颗按钮是灰的。
                         这句话不再摆在界面上（2026-10-28 用户要求去掉）：
                         一行行都挂着一句警告，看着像出了事，其实只是
                         「这一行已经制过证了」。 -->
                    <el-button
                      link
                      type="primary"
                      size="small"
                      :disabled="!canEditPlanSubject(row)"
                      @click="openPlan(row)"
                    >
                      {{ row.plan_subjects?.length ? '调整' : '指定' }}
                    </el-button>
                  </div>
                  <div class="flow-extra-item">
                    <span class="flow-extra-label">状态</span>
                    <el-tag size="small" :type="stateType(row.voucher_state)">
                      {{ stateText(row.voucher_state) }}
                    </el-tag>
                    <el-button
                      v-if="row.reclassify_candidates?.length"
                      link
                      type="warning"
                      size="small"
                      @click="openReclassify(row)"
                    >
                      可改科目
                    </el-button>
                  </div>
                  <div class="flow-extra-item">
                    <span class="flow-extra-label">操作</span>
                    <el-button
                      link
                      type="danger"
                      :disabled="!store.canWrite || !canToggleIgnore(row)"
                      @click="doIgnore(row)"
                    >
                      {{ row.status === 'IGNORED' ? '恢复' : '忽略' }}
                    </el-button>
                  </div>
                </div>
              </template>
            </el-table-column>

            <!-- 默认列：只有对账单上解析出来的那些 -->
            <el-table-column label="#" width="46" prop="seq" />
            <el-table-column label="日期" width="96">
              <template #default="{ row }">{{ row.tx_date || '—' }}</template>
            </el-table-column>
            <el-table-column label="摘要" min-width="150" show-overflow-tooltip>
              <template #default="{ row }">{{ row.summary || '—' }}</template>
            </el-table-column>
            <el-table-column label="用途" min-width="120" show-overflow-tooltip>
              <template #default="{ row }">{{ row.purpose || '—' }}</template>
            </el-table-column>
            <el-table-column label="对方户名" min-width="130" show-overflow-tooltip>
              <template #default="{ row }">{{ row.counterparty_name || '—' }}</template>
            </el-table-column>
            <el-table-column label="收入" width="104" align="right">
              <template #default="{ row }">
                <span v-if="row.direction === 'IN'">{{ money(row.amount) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="支出" width="104" align="right">
              <template #default="{ row }">
                <span v-if="row.direction === 'OUT'">{{ money(row.amount) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="余额" width="104" align="right">
              <template #default="{ row }">{{ money(row.balance) }}</template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- ============================================================ 科目规则 -->
      <!-- 名字就「科目规则」：这一页管的是「按关键词定对方科目」，
           前面那个「流水」是废话 —— 页签叫「流水对账」已经交代了上下文。 -->
      <el-tab-pane label="科目规则" name="rules">
        <!-- **账套级摘要模板已下线（2026-10-28）** —— 用户原话：「完全使用
             内置口径就可以了，不要让用户更改」。

             摘要模板是**一个要学语法的东西**（占位符、渲染规则），而它真正
             解决的问题很小 —— 让摘要写成什么样。放在账套级，意味着每个账套
             都要有人先学会这套语法才敢用；大部分账套一句都不需要改。

             **规则级模板留着** —— 那是「这一类流水（运费、工资）要用另一种
             说法」的表达方式，与关键词规则放在一起才对得上语境。 -->
        <el-card shadow="never">
          <template #header>
            <div class="section-head">
              <span>科目规则</span>
              <span class="muted">
                按摘要 / 用途 / 对方户名里的关键词定对方科目；
                优先级大的赢，同级取更长的关键词。
                每条规则可以单独设摘要模板（盖过内置口径）
              </span>
            </div>
          </template>
          <el-form :inline="true" :model="ruleForm" class="flow-rule-form">
            <el-form-item label="匹配列">
              <el-select v-model="ruleForm.match_field" style="width: 120px">
                <el-option label="摘要" value="SUMMARY" />
                <el-option label="用途" value="PURPOSE" />
                <el-option label="对方户名" value="CP_NAME" />
              </el-select>
            </el-form-item>
            <el-form-item label="关键词">
              <el-input v-model="ruleForm.keyword" placeholder="如：运费" style="width: 160px" />
            </el-form-item>
            <el-form-item label="对方科目">
              <el-select v-model="ruleForm.subject_id" filterable clearable :persistent="false" style="width: 220px">
                <el-option v-for="s in subjects" :key="s.id" :label="subjectLabel(s)" :value="s.id" />
              </el-select>
            </el-form-item>
            <el-form-item label="优先级">
              <el-input-number v-model="ruleForm.priority" :step="1" style="width: 110px" />
            </el-form-item>
            <el-form-item label="摘要模板">
              <el-input
                v-model="ruleForm.summary_template"
                placeholder="留空 = 用内置口径"
                style="width: 200px"
              />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :disabled="!store.canWrite" :loading="savingRule" @click="saveRule">
                新增规则
              </el-button>
            </el-form-item>
          </el-form>
          <!-- 新增规则的模板：占位符能点、还有预览（不然用户看不懂） -->
          <div class="flow-template-help">
            <span class="muted small">点一下插入：</span>
            <el-tag
              v-for="field in SUMMARY_TEMPLATE_FIELDS"
              :key="field"
              size="small"
              effect="plain"
              class="flow-placeholder-tag"
              @click="insertPlaceholder(ruleForm.summary_template, field)"
            >
              {{ '{' + field + '}' }}
            </el-tag>
            <span class="muted small">示例：</span>
            <el-tag
              v-for="ex in SUMMARY_TEMPLATE_EXAMPLES"
              :key="ex.label"
              size="small"
              type="info"
              effect="plain"
              class="flow-placeholder-tag"
              @click="ruleForm.summary_template = ex.template"
            >
              {{ ex.label }}
            </el-tag>
          </div>
          <div class="flow-template-preview">
            <span class="muted small">{{ previewLabel }}</span>
            <span class="flow-template-preview-text">{{ rulePreviewText }}</span>
            <div class="flow-template-values muted small">
              那一行的值：{{ previewValuesText }}
            </div>
          </div>

          <el-table :data="rules" empty-text="还没有规则 —— 没有规则的流水会标成「待确认」">
            <el-table-column label="匹配列" width="110">
              <template #default="{ row }">{{ row.match_label }}</template>
            </el-table-column>
            <el-table-column label="关键词" prop="keyword" min-width="140" />
            <el-table-column label="对方科目" min-width="180">
              <template #default="{ row }">{{ row.subject_code }} {{ row.subject_name }}</template>
            </el-table-column>
            <el-table-column label="摘要模板" min-width="220">
              <template #default="{ row }">
                <el-input
                  :model-value="row.summary_template"
                  size="small"
                  placeholder="留空 = 用内置口径"
                  @change="(v) => saveRuleTemplate(row, v)"
                />
                <!-- **已保存的规则不再逐行给预览。**
                     预览是**写模板时**才需要的东西：模板长什么样，得当场看着
                     一行流水才知道。规则存下来之后，用途是在凭证上（那张凭证的
                     摘要是什么样子），一行一格地铺开预览只会把表格撑得又高又乱，
                     真正要盯的几行反而被淹了。
                     所以：**新增规则时预览一下**（上面那块），存完就干净了。 -->
              </template>
            </el-table-column>
            <el-table-column label="优先级" prop="priority" width="80" />
            <el-table-column label="启用" width="80">
              <template #default="{ row }">{{ row.active ? '是' : '否' }}</template>
            </el-table-column>
            <el-table-column label="操作" width="90">
              <template #default="{ row }">
                <el-button link type="danger" :disabled="!store.canWrite" @click="doDeleteRule(row)">
                  删除
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- ========================================================== 存疑凭证 -->
      <!-- 流水对账的**另一半**。

           流水明细页那条路：「对账单上有、账上没有」→ 生成凭证。
           这一页这条路：「账上有、对账单上没有」→ 那是**错账** ——
           钱记在了不该记的地方，或者根本不该记。

           **按对账单分组**：一份对账单 = 一个账户 + 一段期间，
           期间就是判定的那把尺子 —— 离开它就说不清「这一笔凭什么可疑」。
           -->
      <el-tab-pane label="存疑凭证" name="suspicious">
        <el-card shadow="never">
          <template #header>
            <div class="section-head">
              <span>存疑凭证</span>
              <span class="muted">
                账上有、对账单里找不到对应流水的那些凭证 —— 多半是账户或金额记错了
              </span>
            </div>
          </template>

          <div class="flow-suspicious-summary muted small">
            <template v-if="suspiciousTotal">
              共 <b>{{ suspiciousTotal }}</b> 张存疑凭证。
            </template>
            <template v-else>
              没有存疑凭证。
            </template>
            <!-- **邻期的分录不判定，但要说个数** —— 用户看得见「那几个月还有
                 几笔没说法」，而不是以为它们不存在。 -->
            <span v-if="suspiciousNearCount">
              另有 {{ suspiciousNearCount }} 条邻期分录未判定（那几个月还没有对账单）。
            </span>
            <span v-if="suspiciousInternalCount">
              另有 {{ suspiciousInternalCount }} 张内部转账（钱在自家账户之间挪，不算错账）。
            </span>
          </div>

          <el-empty
            v-if="!suspiciousRows.length"
            description="没有存疑凭证 —— 账上每一笔现金分录都能在流水里找到对应的行"
          />
          <el-table
            v-else
            :data="suspiciousRows"
            class="flow-suspicious-table"
            size="small"
          >
            <el-table-column label="对账单" min-width="190">
              <template #default="{ row }">
                <div>{{ row.account_code }} {{ row.account_name }}</div>
                <div class="muted small">{{ row.period_text }}</div>
              </template>
            </el-table-column>
            <el-table-column label="凭证号" width="100">
              <template #default="{ row }">{{ row.voucher_no }}</template>
            </el-table-column>
            <el-table-column label="日期" width="105">
              <template #default="{ row }">{{ row.voucher_date }}</template>
            </el-table-column>
            <el-table-column label="摘要" min-width="150" show-overflow-tooltip>
              <template #default="{ row }">{{ row.summary || '—' }}</template>
            </el-table-column>
            <el-table-column label="金额" width="130" align="right">
              <template #default="{ row }">{{ money(row.amount) }}</template>
            </el-table-column>
            <el-table-column label="为什么可疑" min-width="260">
              <template #default="{ row }">
                <div>{{ row.reason }}</div>
                <!-- **线索**：没有线索的「疑问」是没用的疑问 —— 用户要能
                     立刻看出是金额错了、还是日期错了。 -->
                <div
                  v-for="(clue, ci) in row.clues"
                  :key="ci"
                  class="muted small"
                >
                  流水第 {{ clue.seq }} 行 · {{ clue.tx_date }} · {{ money(clue.amount) }}
                </div>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="150">
              <template #default="{ row }">
                <el-button link type="primary" size="small" @click="openVoucher(row.voucher_id)">
                  去改这张凭证
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <!-- ======================================================== 对方科目 -->
    <el-dialog v-model="planDialog.open" title="对方科目" width="640px">
      <div class="muted small">
        一笔流水可能对应好几个科目（房租 + 押金、货款 + 运费）。
        各个科目的金额加起来要等于流水金额
        <strong>{{ money(planDialog.row?.amount) }}</strong>，否则凭证会不平衡。
        <br />
        <strong>每个科目的凭证摘要各写各的</strong>：框里那句是
        <strong>按规则算出来的</strong>（规则级模板 → 内置口径），
        改了就用你写的；没改就还是它。留空 = 交回规则去算。
      </div>
      <div v-for="(item, index) in planDialog.items" :key="index" class="flow-plan-row">
        <div class="flow-plan-line">
          <el-select
            v-model="item.subject_id"
            filterable clearable :persistent="false"
            placeholder="选科目"
            class="flow-plan-select"
            @change="onPlanSubjectChange(item)"
          >
            <el-option
              v-for="s in subjects"
              :key="s.id"
              :label="subjectLabel(s)"
              :value="s.id"
            />
          </el-select>
          <el-input-number
            v-model="item.amount"
            :min="0"
            :step="100"
            :precision="2"
            :controls="false"
            class="flow-plan-amount"
          />
          <el-button link type="danger" @click="removePlanItem(index)">删</el-button>
        </div>
        <!-- **摘要跟着这个科目走** —— 不是整张凭证一句。
             一笔拆成「业务招待费 + 房屋租赁费」时，这两个科目该写的话
             本来就不一样（「招待客户」「办公室房租」），一句话管不住。 -->
        <div class="flow-plan-summary">
          <span class="flow-extra-label">凭证摘要</span>
          <el-input
            v-model="item.summary"
            placeholder="留空 = 按规则/模板自动生成"
            clearable
            @input="item.summaryTouched = true"
            @clear="item.summaryTouched = true"
          />
          <el-tag
            v-if="item.summary && item.summary !== item.defaultSummary"
            size="small"
            type="warning"
            effect="plain"
          >
            人工
          </el-tag>
        </div>
      </div>
      <el-button link type="primary" @click="addPlanItem">+ 再加一个科目</el-button>
      <div class="flow-plan-total" :class="planTotalOk ? 'is-ok' : 'is-bad'">
        合计 {{ planTotalText }} / 流水金额 {{ money(planDialog.row?.amount) }}
        <span v-if="!planTotalOk"> —— 差 {{ planTotalDiffText }}</span>
      </div>
      <template #footer>
        <el-button @click="planDialog.open = false">取消</el-button>
        <el-button
          type="primary"
          :disabled="!planTotalOk"
          :loading="savingPlan"
          @click="savePlan"
        >
          保存
        </el-button>
      </template>
    </el-dialog>

    <!-- ======================================================== 改科目 -->
    <el-dialog v-model="reclassifyDialog.open" title="账上已有同一笔分录" width="640px">
      <div class="muted small">
        账上已经有一笔记在别的现金科目上的分录，可能就是这一笔钱。
        确认后把它的**现金那一侧**改成这个账户（{{ accountLabel }}），
        对方侧不动 —— 对方侧该记成什么科目，流水这一侧判不出来。
        <br />
        日期差得比较远的（跨月入账是常事）会标出来，**确认是不是同一笔**再改。
      </div>
      <div
        v-for="item in reclassifyDialog.row?.reclassify_candidates || []"
        :key="item.entry_id"
        class="flow-reclassify-item"
      >
        <div class="flow-reclassify-text">
          <div>{{ reclassifyCandidateText(item) }}</div>
          <div class="muted small">
            {{ item.summary || '（无摘要）' }} · 凭证日期 {{ item.voucher_date }}
          </div>
          <div v-if="isCrossPeriod(item)" class="flow-warn small">
            ⚠ {{ reclassifyCrossPeriodText(item) }}
          </div>
          <div v-if="item.block_reason" class="flow-warn small">{{ item.block_reason }}</div>
        </div>
        <el-button
          type="primary"
          size="small"
          :disabled="!item.changeable || !store.canWrite"
          :loading="reclassifying"
          @click="doReclassify(item)"
        >
          改成 {{ accountLabel }}
        </el-button>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Check, Refresh, Upload } from '@element-plus/icons-vue'
import * as api from '@/api/bankstatement'
import * as subjectApi from '@/api/subject'
import { reportNumber } from '@/utils/format'
import {
  FIELD_OPTIONS, canCommit, canEditPlanSubject, canToggleIgnore, defaultExpandedRowIds,
  diffClass,
  fieldLabel,
  isCrossPeriod, planSubjectCents, planSubjectSummary,
  planSubjectText, planTotalDiff, planTotalMatches, reclassifyCandidateText,
  reclassifyCrossPeriodConfirm, reclassifyCrossPeriodText, reclassifyRowCount,
  renderSummary, settlementSourceText, settlementSourceType,
  suspiciousRows as suspiciousRowsOf,
  toColumnMapping, unknownTemplateFields, voucherStateText, voucherStateType,
  summaryTemplateValuesText,
  SUMMARY_TEMPLATE_EXAMPLES, SUMMARY_TEMPLATE_FIELDS,
} from './flow_state'
import { useSessionStore } from '@/stores/session'

const store = useSessionStore()
const router = useRouter()
const tab = ref('statements')

const statements = ref([])
const subjects = ref([])
const rules = ref([])
const profiles = ref([])
const rows = ref([])
const plan = ref(null)
const current = ref(null)
const activeStatementId = ref(null)

/**
 * **存疑凭证**：账上有、对账单里找不到对应流水的那些凭证。
 *
 * **懒加载**：它要扫一遍账上所有现金分录，而用户未必会去看它
 * （大多数时候根本没有存疑凭证）—— 每次进来都查一遍是白查。
 */
const suspicious = ref(null)

const loading = ref(false)
const loadingRows = ref(false)
const uploading = ref(false)
const reparsing = ref(false)
const committing = ref(false)
const savingRule = ref(false)
const savingPlan = ref(false)
const reclassifying = ref(false)
const pickedFile = ref(null)
const diagPanels = ref([])
const manualMapping = reactive({})

/**
 * 「对账信息」栏：**往下展开**（不是往右加列）。
 *
 * 方向、对方科目、状态这三样是**对账时每行都要看**的东西 —— 让用户一格格点
 * 行首箭头才看得见，等于把主要工作藏起来了。所以默认摊开；嫌占地方就用
 * 行首箭头收起。表格本身仍然只显示对账单上解析出来的内容（日期、摘要、
 * 用途、对方户名、收入、支出、余额）作为行首那一条。
 *
 * **为什么不加列**：加了列表格就被撑宽，窄屏上会冒出**左右滑动条** ——
 * 一边横拖一边看，账对起来很难受。改成展开行后列数恒定，横向不会溢出。
 *
 * **没有开关**：展开箭头一直都在（曾经过来是「勾上『对账信息』才出现箭头」，
 * 那样「不看对账信息的人」和「要改对账信息的人」看到的是两张不同的表）。
 * 现在只剩一个动作：点行首箭头，往下展开/收起。
 *
 * **默认摊开的是「还有活要干的行」**（见
 * :func:`flow_state.defaultExpandedRowIds`）：已入账的、被发票占用的
 * 默认收起 —— 那些行的对账信息看一眼就够，全摊开反而把要处理的那几行淹了。
 */

/**
 * **用户手动开过 / 关过的行**（行 id → true / false）。
 *
 * **为什么单独记一份**：``:expand-row-keys`` 一变，el-table 会把展开状态
 * **整个换成**那批 key（``setExpandRowKeys`` 是赋值，不是合并）。而数据每
 * 换一次（刷新流水、忽略一行、生成凭证）那个数组都会重算 —— 不记这一份，
 * 用户刚点开的行就被弹回去了。
 *
 * 记的是**用户的意愿**，不是「当前状态」：开了记 true，关了记 false。
 * 没记过的行按默认来（还有活 → 摊开；已办完 → 收起）。
 */
const rowExpandOverrides = ref(new Map())

/**
 * 当前要展开的行 id —— 绑到 el-table 的 ``:expand-row-keys``。
 *
 * **判据与用户意愿都算进去**：默认摊开的是「还有活要干的行」，用户手动
 * 开过/关过的以用户为准。
 */
const expandedRowIds = computed(() => {
  const base = new Set(defaultExpandedRowIds(rows.value))
  const overrides = rowExpandOverrides.value
  const out = []
  for (const row of rows.value) {
    const override = overrides.get(row.id)
    const expanded = override !== undefined ? override : base.has(row.id)
    if (expanded) out.push(row.id)
  }
  return out
})

/**
 * 用户点了行首箭头。
 *
 * **只记用户的动作，不改展开数组本身** —— 展开数组一变，el-table 就把
 * 状态整个换掉，那正是我们要避开的事。
 *
 * @param {object} row 被点的那一行
 * @param {Array} expandedRows 现在展开着的行
 */
function onExpandChange(row, expandedRows) {
  if (!row || row.id === undefined || row.id === null) return
  const isOpen = (expandedRows || []).some((item) => item && item.id === row.id)
  const next = new Map(rowExpandOverrides.value)
  next.set(row.id, isOpen)
  rowExpandOverrides.value = next
}

/** 对方科目编辑弹窗。 */
const planDialog = reactive({
  open: false,
  row: null,
  /**
   * 每一个对方科目一行：`subject_id` + `amount` + **它自己的摘要**。
   *
   * 摘要挂在**每一项**上，不是整个弹窗一个 —— 凭证上每条分录的摘要本来
   * 就是各自独立的字段（见 ``flow_state.planSubjectSummary``）。
   */
  items: [],
})
/** 改科目弹窗。 */
const reclassifyDialog = reactive({ open: false, row: null })

const uploadForm = reactive({ account_subject_id: null })
const ruleForm = reactive({
  match_field: 'SUMMARY', keyword: '', subject_id: null, priority: 0, active: true,
  summary_template: '',
})

/**
 * 把占位符插到模板里（接到末尾）。
 *
 * **为什么做成能点的**：占位符是中文的（「{对方户名}」），手打既慢又容易打错，
 * 而「看不懂」正是这个功能最大的问题 —— 能点一下就有的东西，不该让人背。
 *
 * **只有规则级模板了**（2026-10-28 账套级模板下线）—— 所以不必再问
 * 「插到哪一份」。
 */
function insertPlaceholder(current, field) {
  ruleForm.summary_template = `${current || ''}{${field}}`
}

/**
 * 预览拿哪一行流水渲染：**当前这份对账单的第一行**。
 *
 * 预览的作用是让人**看懂模板怎么拼**，不是预告「这条流水最后会生成什么摘要」
 * —— 那个得等真的制了凭证才说得准。
 */
const previewRow = computed(() => rows.value[0] || null)

/** 预览那句说明：**要说清楚拿哪一行渲染的**，否则用户会以为那就是结果。 */
const previewLabel = computed(() => {
  const row = previewRow.value
  if (!row) return '先选一份对账单，下面的预览才有内容'
  return `预览（用第 ${row.seq} 行流水演示模板效果）：`
})

/** 新增规则里那个模板的预览。 */
const rulePreviewText = computed(
  () => renderSummary(ruleForm.summary_template, previewRow.value))

/**
 * 预览那一行的**各个字段的值**。
 *
 * 只给渲染结果是不够的：模板渲染为空时后端会退回内置口径（用途优先、摘要
 * 兜底），预览显示的也就是内置口径那个结果 —— 只看结果，用户会把「摘要」
 * 误当成「用途」。把字段值摆出来，一眼就看得明白。
 */
const previewValuesText = computed(
  () => (previewRow.value ? summaryTemplateValuesText(previewRow.value) : ''))

const subjectLabel = (s) => `${s.code} ${s.name}`

/** 现金类科目：1001 库存现金、1002 银行存款、1012 其他货币资金。 */
const cashSubjects = computed(() => subjects.value.filter(
  (s) => ['1001', '1002', '1012'].some((p) => String(s.code || '').startsWith(p)),
))

const canUpload = computed(() => !!pickedFile.value && !!uploadForm.account_subject_id)

/** 有多少行「账上可能已有同一笔、只是记错了账户」。 */
const reclassifyCount = computed(() => reclassifyRowCount(rows.value))

/** 当前这份对账单。 */
const activeStatement = computed(
  () => statements.value.find((s) => s.id === activeStatementId.value) || null)

/** 改科目的目标账户（这份对账单绑定的那个）。 */
const accountLabel = computed(() => {
  const s = activeStatement.value
  if (!s) return '本账户'
  return [s.account_subject_code, s.account_subject_name].filter(Boolean).join(' ')
})

/** 对方科目的合计与流水金额是否一致。 */
const planTotalOk = computed(() => {
  const row = planDialog.row
  if (!row) return false
  if (!planDialog.items.length) return false
  if (planDialog.items.some((item) => !item.subject_id)) return false
  return planTotalMatches(planDialog.items, row.amount)
})

const planTotalText = computed(
  () => money((planSubjectCents(planDialog.items) / 100).toFixed(2)))

const planTotalDiffText = computed(() => {
  const diff = Math.abs(planTotalDiff(planDialog.items, planDialog.row?.amount))
  return money((diff / 100).toFixed(2))
})

/**
 * 金额显示：**走 ``utils/format`` 的 ``reportNumber``**，
 * 不在这里另写一份格式化。
 *
 * ``reportNumber`` 处理的是后端给来的十进制字符串（它给的就是两位小数的
 * 字符串），全程按字符串处理，一个浮点都不经过 —— 18 位整数在双精度里
 * 已经不是原来那个数了。这里若自己用 ``toLocaleString`` 走一遍 Number，
 * 同一个金额在流水页与凭证页就会显示成两个数。
 */
const money = (v) => {
  if (v === '' || v === null || v === undefined) return '—'
  const text = reportNumber(v)
  return text === '' ? '—' : text
}

const parseText = (v) => ({ OK: '解析正常', PARTIAL: '解析有疑点', FAILED: '解析失败' }[v] || v || '—')
const parseType = (v) => ({ OK: 'success', PARTIAL: 'warning', FAILED: 'danger' }[v] || 'info')

const stateText = voucherStateText
const stateType = voucherStateType

const noteList = computed(() => {
  const notes = current.value?.diagnosis?.notes || []
  const msg = current.value?.parse_message
  if (msg && !notes.includes(msg)) return [msg, ...notes]
  return notes
})

const gridRows = computed(() => current.value?.diagnosis?.grid_preview || [])
const gridColumns = computed(() => {
  const rows = gridRows.value
  const width = rows.reduce((max, r) => Math.max(max, r.length), 0)
  return Array.from({ length: width }, (_, i) => i)
})

const manualMappingCount = computed(() => Object.keys(manualMapping).filter(
  (k) => manualMapping[k],
).length)

async function loadAll() {
  loading.value = true
  try {
    const [list, flat, ruleList, profileList] = await Promise.all([
      api.listStatements(store.bookId),
      subjectApi.subjectFlat(store.bookId, { year: store.currentYear }),
      api.listRules(store.bookId),
      api.listProfiles(store.bookId),
    ])
    statements.value = list || []
    subjects.value = flat || []
    rules.value = ruleList || []
    profiles.value = profileList || []
    if (current.value) {
      const again = statements.value.find((s) => s.id === current.value.id)
      if (again) current.value = again
    }
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '加载失败')
  } finally {
    loading.value = false
  }
}

/**
 * 存疑凭证清单（**摊平成一张表**）。
 *
 * 后端按**对账单**分组返回（一份对账单 = 一个账户 + 一段期间，
 * 期间就是判定的那把尺子）；界面上把它们摊平成一张表、每行带上账户与期间 ——
 * 那样一眼能扫完，又不会把「哪个账户」丢在折叠层里。
 */
const suspiciousRows = computed(() => suspiciousRowsOf(suspicious.value?.groups))

/** 一共几张存疑凭证。 */
const suspiciousTotal = computed(() => suspicious.value?.total_suspicious || 0)

/** 邻期未判定的分录数 —— **要说出来**，否则用户以为那些分录不存在。 */
const suspiciousNearCount = computed(
  () => suspicious.value?.total_near_period_uncounted || 0)

/** 内部转账的张数 —— 单独拎出来，免得用户以为漏了。 */
const suspiciousInternalCount = computed(
  () => suspicious.value?.total_internal_transfer || 0)

async function loadSuspicious() {
  try {
    suspicious.value = await api.getSuspiciousVouchers(store.bookId)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '加载存疑凭证失败')
  }
}

/**
 * 切页签。
 *
 * **存疑凭证那一页懒加载**（见 ``suspicious`` 那条注释）。
 */
function onTabChange(name) {
  if (name === 'suspicious') loadSuspicious()
}

/**
 * 去改那张凭证。
 *
 * **这一页只负责「指出问题」，不负责「改」** —— 错账的改法各不相同
 * （改账户、改金额、删凭证），写死一种改法只会改错。给一条路：直接跳到
 * 那张凭证去改。
 */
function openVoucher(id) {
  router.push({ name: 'voucher-edit', params: { id } })
}

/**
 * 改某条规则的摘要模板（规则级盖过内置口径）。
 */
async function saveRuleTemplate(rule, value) {
  const template = value || ''
  const unknown = unknownTemplateFields(template)
  if (unknown.length) {
    ElMessage.warning(`占位符 {${unknown.join('} {')} } 不认识`)
    return
  }
  try {
    await api.updateRule(store.bookId, rule.id, { summary_template: template })
    ElMessage.success('已保存这条规则的摘要模板')
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  }
}

function onFile(file) {
  pickedFile.value = file.raw
}

async function doUpload() {
  if (!canUpload.value) return
  uploading.value = true
  try {
    const created = await api.uploadStatement(
      store.bookId, pickedFile.value, uploadForm.account_subject_id)
    pickedFile.value = null
    uploadForm.account_subject_id = null
    await loadAll()
    current.value = statements.value.find((s) => s.id === created.id) || null
    resetManualMapping()
    ElMessage.success(`已解析 ${created.row_count} 行`)
    tab.value = 'statements'
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '上传失败')
  } finally {
    uploading.value = false
  }
}

async function openRows(statement) {
  if (!statement) return
  current.value = statement
  activeStatementId.value = statement.id
  tab.value = 'rows'
  await loadRows()
}

async function loadRows() {
  if (!activeStatementId.value) return
  loadingRows.value = true
  try {
    rows.value = await api.listRows(store.bookId, activeStatementId.value)
    plan.value = await api.planVouchers(store.bookId, activeStatementId.value)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '加载流水失败')
  } finally {
    loadingRows.value = false
  }
}

async function doReparse(statement) {
  try {
    await ElMessageBox.confirm(
      '重新解析会按最新的规则重跑一遍（已生成凭证的行不会被重建）。继续？',
      '重新解析', { type: 'warning' })
  } catch {
    return
  }
  reparsing.value = true
  try {
    const updated = await api.reparse(store.bookId, statement.id, {})
    await loadAll()
    current.value = statements.value.find((s) => s.id === updated.id) || current.value
    resetManualMapping()
    ElMessage.success('已重新解析')
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '重新解析失败')
  } finally {
    reparsing.value = false
  }
}

async function doDownload(statement) {
  try {
    await api.downloadOriginal(store.bookId, statement.id, statement.filename)
  } catch (e) {
    ElMessage.error(e?.message || '下载失败')
  }
}

async function doDelete(statement) {
  try {
    await ElMessageBox.confirm(
      `删除「${statement.filename}」？它解析出来的流水会一起删掉。`,
      '删除对账单', { type: 'warning' })
  } catch {
    return
  }
  try {
    await api.deleteStatement(store.bookId, statement.id)
    if (current.value?.id === statement.id) current.value = null
    await loadAll()
    ElMessage.success('已删除')
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '删除失败')
  }
}

async function doSetDirection(row, direction) {
  try {
    await api.setRowDirection(store.bookId, row.id, direction)
    await loadRows()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '指定方向失败')
  }
}

/**
 * 打开「对方科目」弹窗。
 *
 * **一行流水可以有好几个对方科目**，所以弹窗里是一张可增删的小表：
 * 每一行一个科目 + 一份金额 + **一句自己的凭证摘要**。
 */
function openPlan(row) {
  planDialog.row = row
  // **摘要框默认带上「按规则算出来的那句」** —— 用户先看见「不改会是什么」，
  // 要改再改。（人工写过的优先；后端给的两样都在这里。）
  const defaultSummary = (row.default_summary || '').trim()
  const items = (row.plan_subjects || []).map((item) => ({
    subject_id: item.subject_id,
    amount: Number(item.amount) || 0,
    summary: planSubjectSummary(item) || defaultSummary,
    defaultSummary,
    /**
     * **用户动过这个框没有。**
     *
     * 没动过 → 保存时**不带** summary → 后端继续按规则算。
     * 动了 → 把框里的字存成这个科目的人工摘要。
     *
     * **为什么不直接每次都存**：没碰过它的科目如果也被存下来，规则改了
     * 之后它们就不跟着变了 —— 而用户根本没碰过它们，凭什么被冻住。
     */
    summaryTouched: false,
  }))
  planDialog.items = items.length
    ? items
    : [{
      subject_id: null,
      amount: Number(row.amount) || 0,
      summary: defaultSummary,
      defaultSummary,
      summaryTouched: false,
    }]
  planDialog.open = true
}

function addPlanItem() {
  const defaultSummary = (planDialog.row?.default_summary || '').trim()
  planDialog.items.push({
    subject_id: null,
    amount: 0,
    summary: defaultSummary,
    defaultSummary,
    summaryTouched: false,
  })
}

function removePlanItem(index) {
  planDialog.items.splice(index, 1)
}

/**
 * **换了科目，摘要就回到默认。**
 *
 * 摘要**跟着科目走** —— 后端那条规则是「同一个科目上那句人工摘要才跟着
 * 走，换了科目就不带走」（``_write_plan_subjects``）。所以换了科目之后，
 * 框里若还留着上一个科目的话，那句话**根本不会被存下来** ——
 * 而界面上还显示着它，等于「显示的没生效」。
 *
 * 让界面与存储对齐：换科目 → 框里回到「按规则算出来的那句」，
 * 并且标记成**没动过**（于是保存时不带 summary，由规则说了算）。
 * 想留着自己那句话的用户，改完科目再重打一遍就是了。
 */
function onPlanSubjectChange(item) {
  item.summary = item.defaultSummary
  item.summaryTouched = false
}

async function savePlan() {
  const row = planDialog.row
  if (!row || !planTotalOk.value) return
  savingPlan.value = true
  try {
    const payload = {
      plan_subjects: planDialog.items.map((item) => {
        const entry = {
          subject_id: item.subject_id,
          amount: String(item.amount),
        }
        // **只有用户动过这个科目的摘要框才带 summary** —— 没动过的继续
        // 跟着规则走（规则改了要跟着变）。带空字符串 = 「交回规则去算」。
        if (item.summaryTouched) {
          entry.summary = (item.summary || '').trim()
        }
        return entry
      }),
    }
    await api.patchRow(store.bookId, row.id, payload)
    planDialog.open = false
    await loadRows()
    ElMessage.success('已保存对方科目')
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '保存对方科目失败')
  } finally {
    savingPlan.value = false
  }
}

/** 打开「改科目」弹窗：账上可能与这一行是同一笔的分录。 */
function openReclassify(row) {
  reclassifyDialog.row = row
  reclassifyDialog.open = true
}

async function doReclassify(item) {
  const row = reclassifyDialog.row
  if (!row) return
  // **跨期的要多问一句。** 日期差得多也可能压根不是另一笔钱 —— 不问就改，
  // 等于把不相干的一笔现金分录挪了科目，那比「没匹配到」糟得多：
  // 错的是账，不是「没提示」。
  if (isCrossPeriod(item)) {
    try {
      await ElMessageBox.confirm(
        reclassifyCrossPeriodConfirm(item),
        '请确认是不是同一笔',
        {
          type: 'warning',
          confirmButtonText: '是同一笔，改科目',
          cancelButtonText: '再想想',
        },
      )
    } catch {
      return   // 用户点了「再想想」—— 什么都不做
    }
  }
  reclassifying.value = true
  try {
    const result = await api.reclassifyRow(store.bookId, row.id, item.entry_id)
    reclassifyDialog.open = false
    await loadRows()
    await loadAll()
    ElMessage.success(
      `已把 ${result.from_subject.code} 改成 ${result.to_subject.code}，这条流水算已入账`)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '改科目失败')
  } finally {
    reclassifying.value = false
  }
}

async function doIgnore(row) {
  const next = row.status === 'IGNORED' ? 'OPEN' : 'IGNORED'
  try {
    await api.patchRow(store.bookId, row.id, { status: next })
    await loadRows()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  }
}

async function doCommit() {
  if (!activeStatementId.value) return
  committing.value = true
  try {
    const result = await api.commitVouchers(store.bookId, activeStatementId.value)
    const failed = result.failures?.length || 0
    if (failed) {
      ElMessage.warning(`生成了 ${result.created_count} 张凭证，${failed} 条没生成（见流水明细里的原因）`)
    } else {
      ElMessage.success(`生成了 ${result.created_count} 张凭证`)
    }
    await loadRows()
    await loadAll()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '生成凭证失败')
  } finally {
    committing.value = false
  }
}

async function saveRule() {
  if (!ruleForm.keyword || !ruleForm.subject_id) {
    ElMessage.warning('关键词与对方科目都要填')
    return
  }
  savingRule.value = true
  try {
    await api.createRule(store.bookId, { ...ruleForm })
    ruleForm.keyword = ''
    ruleForm.subject_id = null
    ruleForm.summary_template = ''
    await loadAll()
    ElMessage.success('已新增规则')
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '新增规则失败')
  } finally {
    savingRule.value = false
  }
}

async function doDeleteRule(row) {
  try {
    await api.deleteRule(store.bookId, row.id)
    await loadAll()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '删除规则失败')
  }
}

async function doDeleteProfile(row) {
  try {
    await api.deleteProfile(store.bookId, row.id)
    await loadAll()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '删除格式失败')
  }
}

function resetManualMapping() {
  Object.keys(manualMapping).forEach((k) => { delete manualMapping[k] })
}

async function applyManualMapping() {
  if (!current.value) return
  const mapping = toColumnMapping(manualMapping)
  reparsing.value = true
  try {
    const updated = await api.reparse(store.bookId, current.value.id, {
      column_mapping: mapping,
    })
    await loadAll()
    current.value = statements.value.find((s) => s.id === updated.id) || current.value
    ElMessage.success('已按指定的列重新解析')
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '重新解析失败')
  } finally {
    reparsing.value = false
  }
}

async function rememberFormat() {
  if (!current.value) return
  const mapping = toColumnMapping(manualMapping)
  try {
    await api.saveProfile(store.bookId, {
      fingerprint: current.value.diagnosis?.fingerprint,
      column_mapping: mapping,
    })
    await loadAll()
    ElMessage.success('已记住这个格式')
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '保存格式失败')
  }
}

onMounted(() => {
  loadAll()
})
</script>

<style scoped>
.flow-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}
.flow-head h2 { margin: 0 0 4px; font-size: 20px; }
.flow-head p { margin: 0; color: var(--el-text-color-secondary); max-width: 760px; line-height: 1.6; }
.section-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  flex-wrap: wrap;
}
.section-head .muted { color: var(--el-text-color-secondary); font-size: 12px; }
.muted { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
.picked { margin-left: 12px; font-size: 12px; }
.flow-upload-form { max-width: 900px; }
/* 窄屏上两个字段会**上下叠起来**（xs=24）—— 叠起来时得留点竖间距，
   否则「现金科目」和「对账单」贴成一片。宽屏并排时不加这个间距。 */
.flow-upload-row :deep(.el-col) { margin-bottom: 18px; }
@media (min-width: 768px) {
  .flow-upload-row :deep(.el-col) { margin-bottom: 0; }
}
.flow-rows-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.flow-select { width: 340px; }
.flow-direction { width: 92px; }
.flow-subject { width: 200px; }

/* 流水明细表要**紧凑**：一行流水一眼能扫过去，而不是一屏放不了十行。 */
.flow-rows-table :deep(.el-table__cell) { padding: 4px 0; }
.flow-rows-table :deep(.cell) { line-height: 1.5; }

/* 「对账信息」是**展开行**（往下展开），不是往右加的列 —— 所以列数恒定，
   横向不会溢出，也就不会有左右滑动条。
   展开行里的内容横向并排，窄了自动换行；换行是往下换，绝不撑宽表格。 */
.flow-extra {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 28px;
  padding: 2px 0;
}
.flow-extra-item {
  display: flex;
  align-items: center;
  /* 换行是**往下**换 —— 对方科目那一格可能有好几个科目标签，
     不许它把这一行撑宽（撑宽就又是左右滑动条）。 */
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
}
.flow-extra-label {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  flex: 0 0 auto;
}
.flow-rows-table :deep(.el-table__expanded-cell) {
  padding: 6px 12px 8px 46px;
  background: var(--el-fill-color-lighter);
}

/* 对方科目：一行可以有好几个，用标签平铺。 */
.flow-plan { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 2px; }
.flow-plan-tag { font-size: 12px; }

/* 摘要模板：占位符 / 示例 / 预览
   ------------------------------------------------------------------
   **这一块是给「看不懂模板」这件事用的** —— 所以占位符能点、示例能套、
   预览能看到真东西长什么样。三样缺一样，用户就只能猜。 */
.flow-template-help {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
}
.flow-placeholder-tag {
  cursor: pointer;
  user-select: none;
}
.flow-placeholder-tag:hover { opacity: 0.75; }
.flow-template-preview {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
  margin-top: 8px;
  padding: 6px 10px;
  border-radius: 4px;
  background: var(--el-fill-color-lighter);
  min-height: 28px;
}
.flow-template-preview-text {
  font-size: 13px;
  color: var(--el-text-color-primary);
  word-break: break-all;
}
.flow-template-values {
  width: 100%;
  word-break: break-all;
  line-height: 1.6;
}

/* 对方科目编辑弹窗：一行 = 科目 + 金额 + 删。 */
/* 一个对方科目 = 一块：上面一行（科目 + 金额 + 删），下面一行（它自己的摘要）。 */
.flow-plan-row {
  margin-bottom: 12px;
  padding: 10px 12px 4px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
}
.flow-plan-line {
  display: flex;
  align-items: center;
  gap: 8px;
}
/* 凭证摘要：**跟着这个科目走** —— 不是整张凭证一句。 */
.flow-plan-summary {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 8px 0 6px;
}
.flow-plan-summary .flow-extra-label {
  flex: 0 0 auto;
  margin: 0;
}
.flow-plan-summary :deep(.el-input) { flex: 1 1 auto; }
/* 展开行里「科目 → 摘要」的对应关系。 */
.flow-plan-summary-row {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  line-height: 1.8;
}
.flow-plan-summary-subject {
  color: var(--el-text-color-regular);
}
.flow-plan-select { flex: 1 1 auto; min-width: 200px; }
.flow-plan-amount { flex: 0 0 140px; }
.flow-plan-total {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--el-border-color-lighter);
  font-size: 13px;
}
.flow-plan-total.is-ok { color: var(--el-color-success); }
.flow-plan-total.is-bad { color: var(--el-color-danger); }

/* 改科目弹窗：一行一条候选。 */
.flow-reclassify-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.flow-reclassify-text { min-width: 0; }
.flow-warn { color: var(--el-color-warning-dark-2); }

/* 存疑凭证那一页顶上的那行统计。 */
.flow-suspicious-summary {
  margin-bottom: 10px;
  line-height: 1.6;
}

.diag-grid {
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
  margin-bottom: 10px;
  font-size: 13px;
}
.diag-notes { margin: 8px 0; }
.diag-note {
  font-size: 13px;
  color: var(--el-color-warning-dark-2);
  line-height: 1.7;
}
.diag-mapping { margin: 10px 0; display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.mapping-tag { margin-right: 4px; }
.diag-collapse { margin-top: 8px; }
.grid-cell {
  font-size: 12px;
  color: var(--el-text-color-regular);
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.mapping-select { width: 100%; margin-top: 4px; }
.diag-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 12px;
  flex-wrap: wrap;
}
.diff-ok { color: var(--el-color-success); }
.diff-warn { color: var(--el-color-warning); }
.diff-bad { color: var(--el-color-danger); }
</style>
