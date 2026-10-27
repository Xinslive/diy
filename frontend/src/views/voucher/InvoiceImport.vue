<template>
  <div class="page invoice-page">
    <div class="invoice-head">
      <div>
        <h2>发票导入</h2>
        <p>解析电子发票后，按科目规则生成未记账凭证。确认前可逐张核对，原始 PDF 会保留在批次中。</p>
      </div>
      <div class="invoice-head-actions">
        <el-button :disabled="!store.canWrite" @click="loadAll">
          <el-icon><Refresh /></el-icon>刷新
        </el-button>
        <el-button v-if="batch" :disabled="!store.canWrite" @click="newBatch">新建批次</el-button>
      </div>
    </div>

    <el-tabs v-model="tab" class="invoice-tabs">
      <el-tab-pane label="发票批次" name="batches">
        <el-card shadow="never" class="invoice-config-card">
          <template #header>
            <div class="section-head">
              <span>自动制证配置</span>
              <span class="muted">本单位税号用于自动判断进项/销项；科目需自行选择，不预填。进项发票不单列税额，价税合计直接计入成本费用。兜底科目：没命中规则的项目落到它上面</span>
            </div>
          </template>
          <!--
            两列栅格，不是 inline 表单。inline 下标签宽度跟着文字走 ——
            「本单位税号」五个字、「销项税额」四个字，两行的输入框起点就对不齐；
            换行之后按钮又落在第二行飘着，整块看着是散的。固定 label-width 加
            两列，输入框自然成一条线。
          -->
          <!--
            标签一律四个字（单位税号 / 销项税额 / 进项贷方 / 销项借方 /
            进项兜底 / 销项兜底），label-width 92 刚好装下 —— 六个字的
            「进项兜底科目」会被挤成两行，标签和输入框的起点就错开了。
          -->
          <el-form :model="config" label-width="92px" class="invoice-config-form">
            <el-row :gutter="'0 28'">
              <el-col :xs="24" :sm="12">
                <el-form-item label="单位税号">
                  <el-input v-model="config.taxpayer_id" :disabled="!store.canWrite" placeholder="用于判断发票类型" />
                </el-form-item>
              </el-col>
              <el-col :xs="24" :sm="12">
                <el-form-item label="销项税额">
                  <el-select v-model="config.output_tax_subject_id" :disabled="!store.canWrite" filterable clearable :persistent="false">
                    <el-option v-for="s in subjects" :key="s.id" :label="subjectLabel(s)" :value="s.id" />
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :xs="24" :sm="12">
                <el-form-item label="进项贷方">
                  <el-select v-model="config.input_credit_subject_id" :disabled="!store.canWrite" filterable clearable :persistent="false">
                    <el-option v-for="s in subjects" :key="s.id" :label="subjectLabel(s)" :value="s.id" />
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :xs="24" :sm="12">
                <el-form-item label="销项借方">
                  <el-select v-model="config.output_debit_subject_id" :disabled="!store.canWrite" filterable clearable :persistent="false">
                    <el-option v-for="s in subjects" :key="s.id" :label="subjectLabel(s)" :value="s.id" />
                  </el-select>
                </el-form-item>
              </el-col>
              <!--
                兜底科目：没命中任何科目规则的发票项目落到这里。
                分进项/销项两个 —— 进项兜底记成本费用、销项兜底记收入，
                混用必有一边记错。留空 = 不兜底（配不出来的项目仍然报错）。
              -->
              <el-col :xs="24" :sm="12">
                <el-form-item label="进项兜底">
                  <el-select v-model="config.input_fallback_subject_id" :disabled="!store.canWrite" filterable clearable :persistent="false" placeholder="未配置则报错">
                    <el-option v-for="s in subjects" :key="s.id" :label="subjectLabel(s)" :value="s.id" />
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :xs="24" :sm="12">
                <el-form-item label="销项兜底">
                  <el-select v-model="config.output_fallback_subject_id" :disabled="!store.canWrite" filterable clearable :persistent="false" placeholder="未配置则报错">
                    <el-option v-for="s in subjects" :key="s.id" :label="subjectLabel(s)" :value="s.id" />
                  </el-select>
                </el-form-item>
              </el-col>
            </el-row>
            <el-form-item>
              <el-button type="primary" :disabled="!store.canWrite" :loading="savingConfig" @click="saveConfig">
                <el-icon><Check /></el-icon>保存配置
              </el-button>
            </el-form-item>
          </el-form>
        </el-card>

        <el-card v-if="!batch" shadow="never" class="invoice-upload-card">
          <template #header>
            <div class="section-head">
              <span>上传电子发票</span>
              <span class="muted">支持多份 PDF、整个文件夹，或一份合并好的 PDF（每页一张发票）</span>
            </div>
          </template>
          <!--
            拖放自己接，不用 el-upload 的。
            `dataTransfer.files` 在拖进来的是**文件夹**时是空的，只有
            `webkitGetAsEntry` 能递归出里面的文件（见 invoice_upload.js）。
            capture + stop 是为了在事件到达拖拽框之前截住它 —— 否则同一批文件
            会被这里和 el-upload 各收一遍，列表里出现重复行。
            dragover 不拦：让 el-upload 继续负责那个高亮反馈。
          -->
          <div @drop.capture.stop.prevent="onDrop">
            <el-upload
              drag multiple :auto-upload="false" :show-file-list="false" accept=".pdf"
              :disabled="!store.canWrite" :on-change="onFile"
            >
              <el-icon class="el-icon--upload"><Upload /></el-icon>
              <div class="el-upload__text">把 PDF（或整个文件夹）拖到此处，或<em>点击选择文件</em></div>
            </el-upload>
          </div>
          <div class="upload-actions">
            <el-button :disabled="!store.canWrite" @click="folderInput?.click()">
              <el-icon><FolderOpened /></el-icon>选择文件夹
            </el-button>
            <!--
              不给目录选择器加 accept：加了浏览器的目录对话框会把非 PDF 直接
              藏起来，用户就看不到「这个文件夹里到底有哪些文件被跳过了」。
              这里宁可全部捞进来，由页面按后端同一套规则挑出 PDF 并列出忽略项。
            -->
            <input ref="folderInput" type="file" webkitdirectory multiple class="hidden-input" @change="onFolderPicked" />
            <span class="muted">最多 {{ uploadLimits.maxFiles }} 个文件，单次合计 {{ humanBytes(uploadLimits.maxTotalBytes) }}</span>
          </div>
          <div v-if="files.length" class="picked-list">
            <div class="picked-head">
              <span>已选 {{ files.length }} 个 PDF，合计 {{ humanBytes(totalBytes) }}</span>
              <el-button link type="primary" :disabled="!store.canWrite" @click="clearFiles">清空</el-button>
            </div>
            <div class="picked-scroll">
              <div v-for="(item, index) in files" :key="itemKey(item, index)" class="picked-row">
                <span class="picked-name" :title="item.webkitRelativePath || item.name">{{ item.name }}</span>
                <span class="muted">{{ humanBytes(item.size) }}</span>
                <el-button link :disabled="!store.canWrite" @click="removeFile(index)">移除</el-button>
              </div>
            </div>
            <div v-if="skipped.length" class="skipped-note">
              {{ skipNote(skipped) }}
            </div>
          </div>
          <div class="upload-footer">
            <span class="muted">{{ files.length ? '' : '尚未选择文件' }}</span>
            <el-button type="primary" :disabled="!files.length || !store.canWrite" :loading="uploading" @click="upload">
              <el-icon><Upload /></el-icon>上传并解析{{ files.length > 1 ? `（${files.length} 个文件）` : '' }}
            </el-button>
          </div>
        </el-card>

        <el-card v-if="!batch && history.length" shadow="never" class="invoice-history-card">
          <template #header><span>处理历史</span></template>
          <el-table :data="history" size="small" border @row-click="loadBatch">
            <el-table-column prop="filename" label="文件" min-width="220" />
            <el-table-column label="时间" width="180"><template #default="{ row }">{{ formatDateTime(row.created_at) }}</template></el-table-column>
            <el-table-column label="状态" width="110">
              <template #default="{ row }"><el-tag size="small" :type="batchType(row.status)">{{ batchText(row.status) }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="total" label="发票" width="75" align="center" />
            <el-table-column prop="committed_count" label="已生成凭证" width="105" align="center" />
            <el-table-column prop="message" label="备注" min-width="220" show-overflow-tooltip />
          </el-table>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="科目规则" name="rules">
        <el-card shadow="never">
          <template #header>
            <div class="section-head">
              <span>供应商 / 项目科目规则</span>
              <el-button type="primary" :disabled="!store.canWrite" @click="openRule">
                <el-icon><Plus /></el-icon>新增规则
              </el-button>
            </div>
          </template>
          <el-alert type="info" :closable="false" show-icon class="rule-help">
            同一优先级下取更长关键词；仍然冲突会在预览中标红，不会静默猜科目。没命中规则的项目落到「自动制证配置」里的兜底科目（进项/销项各一个），没配兜底时不能确认生成凭证。
          </el-alert>
          <el-table :data="rules" size="small" border>
            <el-table-column label="匹配对象" width="110">
              <template #default="{ row }">{{ row.match_type === 'SELLER' ? '供应商/客户' : '项目名称' }}</template>
            </el-table-column>
            <el-table-column label="发票类型" width="100">
              <template #default="{ row }">{{ invoiceTypeText(row.invoice_type) }}</template>
            </el-table-column>
            <el-table-column prop="keyword" label="关键词" min-width="180" />
            <el-table-column label="科目" min-width="210">
              <template #default="{ row }">{{ row.subject_code }} {{ row.subject_name }}</template>
            </el-table-column>
            <el-table-column prop="priority" label="优先级" width="85" align="center" />
            <el-table-column label="状态" width="85" align="center">
              <template #default="{ row }"><el-tag size="small" :type="row.active ? 'success' : 'info'">{{ row.active ? '启用' : '停用' }}</el-tag></template>
            </el-table-column>
            <el-table-column label="操作" width="110" fixed="right">
              <template #default="{ row }">
                <el-button link type="danger" :disabled="!store.canWrite" @click="removeRule(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-if="!rules.length" description="还没有规则，请先新增供应商或项目关键词规则" />
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <template v-if="batch">
      <el-card shadow="never" class="invoice-result-card">
        <template #header>
          <div class="section-head">
            <div class="result-title">
              <span>{{ batch.filename }}</span>
              <el-tag v-if="batch.ocr_used" size="small" type="warning">OCR API 兜底</el-tag>
            </div>
            <div class="result-actions">
              <!--
                只看还没入账的那些，**默认就是开着的**。

                一批几十张票，生成过一轮之后剩下的那几张淹在一堆「已生成」
                里，翻半天找不着 —— 未入账的那批才是待办清单。默认开着，
                打开这个页面看到的就是「还剩多少要做」，不用每次先点一下
                开关才看到正事。

                口径是「这张发票有没有凭证」，不是「状态是什么」：被点了忽略
                的票也没入账，它留在列表里 —— 用户就是靠这张列表把它取消
                忽略、补生成凭证的。
              -->
              <!--
                按钮**不做成主按钮**：这一排里只有「确认生成」是主操作，
                开关要是也染成主色，两个蓝按钮并排，谁是正事就说不清了。
                开关的状态由旁边那一句说明。
              -->
              <el-button @click="onlyUnbooked = !onlyUnbooked">
                {{ onlyUnbooked ? '显示全部发票' : '只显示未入账发票' }}
              </el-button>
              <!--
                开着的时候，当前视野与藏起来的张数都要讲出来：不然
                「共 100 张」而表里只有 51 行，看着像丢了 49 张发票。
              -->
              <span v-if="onlyUnbooked" class="muted result-hint">{{ filterHint }}</span>
              <el-button :disabled="!store.canWrite" :loading="previewing" @click="recalculate">重新计算预览</el-button>
              <!--
                已加载了几份对账单 —— 没有对账单时，发票的结算科目只能回落到
                「自动制证配置」里那个；有对账单时，点一次「重新计算预览」
                就会按「金额 + 收支方向」重新匹配。
              -->
              <span class="muted result-hint">{{ statementHint }}</span>
              <!--
                已生成过的批次不能作废：凭证已经在账上了，作废批次删记录、
                删原件，却不会把那几张凭证撤下来 —— 账上留着、记录没了，
                更难查。所以这里直接不让点。
              -->
              <el-button type="danger" plain :disabled="!store.canWrite || batch.status === 'COMMITTED'" @click="discard">作废批次</el-button>
              <el-button type="primary" :disabled="!store.canWrite || batch.error_count > 0 || !batch.ready_count" :loading="committing" @click="commit">
                确认生成 {{ batch.ready_count || 0 }} 张凭证
              </el-button>
            </div>
          </div>
        </template>
        <el-alert
          :type="batch.error_count ? 'error' : 'success'" :closable="false" show-icon class="mb"
          :title="batchSummaryTitle"
        />
        <el-alert v-if="batch.error_count" type="warning" :closable="false" show-icon class="mb"
                  title="请展开红色发票，修正类型或科目后点击「重新计算预览」；所有异常消除后才能确认。" />
        <el-alert v-if="batch.error_count" type="info" :closable="false" show-icon class="mb"
                  title="合并件里的封面、装订页、重复扫描页都不是发票：展开该行点「忽略这张」，它不会生成凭证，也不再算异常。" />

        <!--
          默认全部展开（default-expand-all）：这个表的价值就是「一眼看到每张发票
          分了哪些项目、进了哪个科目」，默认收起等于把最该看的东西藏起来，
          每次都要点 23 次箭头才看得全。
          手动收起某一行仍然有效 —— 它只在数据换新（重新计算预览）时重置。
        -->
        <el-table :data="visibleInvoices" size="small" border row-key="id" default-expand-all :row-class-name="invoiceRowClass">
          <!--
            整批都已入账的时候，开了筛选的表就是空的 —— 空表本身不说明
            「没有了」还是「筛掉了」，所以这里把话讲全：都入账了，点一下
            就能看全部。
          -->
          <template #empty>
            <div class="invoice-empty">{{ emptyHint }}</div>
          </template>
          <el-table-column type="expand">
            <template #default="{ row }">
              <div class="invoice-detail">
                <!--
                  已被点名忽略的行只留「说明 + 取消忽略」。
                  还摊着一排科目下拉和一张空的明细表，看着像「这张还要填」，
                  而它已经不参与生成凭证了 —— 控件摆在那里只会误导。
                -->
                <div v-if="isIgnored(row)" class="ignored-note">
                  <span>已忽略：这一页不参与生成凭证，也不计为异常；确认时自动跳过。</span>
                  <el-button size="small" :disabled="!store.canWrite" :loading="ignoringId === row.id" @click="toggleIgnored(row)">取消忽略</el-button>
                </div>
                <!--
                  「只显示未入账发票」开着的时候，**被忽略的那几张也要摊开票面
                  明细** —— 它们同样没入账，正是要人工二次核对的那一批。

                  只摊**票面内容**（项目、金额、税额、税率）：被忽略的票还没
                  分科目、没算凭证，摊「入账科目」和「凭证预览」是摊两样还没
                  发生的事；真要入账，点「取消忽略」再「重新计算预览」，那边
                  的科目下拉和凭证预览就都出来了。

                  这里用**原生表格**而不是 el-table：一个批次几十张被忽略的，
                  每张再挂一个 el-table，组件开销会翻倍 —— 上一版把这张表
                  从 47900 个 DOM 节点降到 14372，靠的就是别让组件堆起来。
                -->
                <table v-if="isIgnored(row) && onlyUnbooked" class="ignored-line-table">
                  <thead>
                    <tr>
                      <th class="idx">#</th>
                      <th>发票项目</th>
                      <th class="num">不含税金额</th>
                      <th class="num">税额</th>
                      <th class="mid">税率</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="(line, index) in row.lines" :key="index">
                      <td class="idx">{{ index + 1 }}</td>
                      <td>{{ lineName(line) }}</td>
                      <td class="num">{{ lineAmount(row, index) }}</td>
                      <td class="num">{{ line.tax }}</td>
                      <td class="mid">{{ line.tax_rate }}</td>
                    </tr>
                  </tbody>
                </table>
                <!--
                  这里用**显式的 v-if**而不是 v-else。

                  上一版在这张原生表格后面挂了一个 v-else，结果 v-else 认的
                  是**紧挨着的那张表格**，不是原来那段「已忽略」提示 ——
                  v-else 认的是相邻元素，中间插了东西就认错。被忽略的行在
                  开关关着时反而摊出了整排科目下拉。
                -->
                <template v-if="!isIgnored(row)">
                <!--
                  已入账（已生成）的发票：**这一排控件一个都不能用**。
                  服务端对已生成的记录直接跳过重算，overrides 一律不生效 ——
                  改发票类型、改结算科目、点「忽略这张」，点了都不会发生。

                  那就不摆。摆一排按了没反应的控件，用户只会以为系统坏了。
                -->
                <div v-if="!isCommitted(row)" class="detail-toolbar">
                  <el-form inline size="small" class="detail-toolbar-form">
                    <el-form-item label="发票类型">
                      <el-select :model-value="invoiceTypeFor(row)" :disabled="!store.canWrite" style="width: 74px" @update:model-value="(v) => setInvoiceType(row, v)">
                        <el-option label="进项" value="INPUT" /><el-option label="销项" value="OUTPUT" />
                      </el-select>
                    </el-form-item>
                    <el-form-item label="税额科目" v-if="invoiceTypeFor(row) === 'OUTPUT'">
                      <!--
                        persistent=false：下拉里的选项**只在展开时才创建**。
                        Element Plus 的 el-select 默认 persistent=true ——
                        那一大堆 el-option 组件在**下拉关着的时候就已经渲染好了**
                        （只是用 v-show 藏起来）。几十张发票全部展开时，
                        「科目下拉数 × 科目表行数」个组件一次性堆出来，
                        就是这个页面卡成这样的原因。
                      -->
                      <el-select :model-value="taxSubjectFor(row)" :disabled="!store.canWrite" filterable :persistent="false" style="width: 128px" @update:model-value="(v) => setTaxSubject(row, v)">
                        <el-option v-for="s in subjects" :key="s.id" :label="subjectLabel(s)" :value="s.id" />
                      </el-select>
                    </el-form-item>
                    <el-form-item v-else label="税额">
                      <span class="muted">不单列，并入第一条明细行</span>
                    </el-form-item>
                    <el-form-item label="结算科目">
                      <el-select :model-value="settlementSubjectFor(row)" :disabled="!store.canWrite" filterable :persistent="false" style="width: 142px" @update:model-value="(v) => setSettlementSubject(row, v)">
                        <el-option v-for="s in subjects" :key="s.id" :label="subjectLabel(s)" :value="s.id" />
                      </el-select>
                      <!--
                        结算科目**从哪来**：人工指定 / 对账单识别 / 配置科目。

                        不标出来的话，用户看到科目被自动改过却不知道为什么 ——
                        而这个科目决定的是钱记到哪个银行账户上，改错了要到
                        对不上账时才发现。徽标把这件事摆到明面上。
                      -->
                      <el-tag
                        v-if="settlementSource(row)"
                        size="small"
                        :type="settlementSourceType(row)"
                        class="settlement-badge"
                      >{{ settlementSourceText(row) }}</el-tag>
                    </el-form-item>
                    <!--
                      没有标签：原来那个「这张」是个从两栏布局里剩下的残骸 ——
                      「发票类型 / 税额 / 结算科目 / 这张」读起来像四件并列的事，
                      其实最后那栏只是这一张发票的操作按钮。
                      去掉标签后按钮组自己右对齐，位置比原来更靠边。
                    -->
                    <el-form-item class="row-actions">
                      <el-button size="small" :disabled="!store.canWrite" :loading="ignoringId === row.id" @click="toggleIgnored(row)">忽略这张</el-button>
                      <!--
                        只有**有问题**的发票才给这个按钮：解析成功的发票重跑一遍
                        不会变好，而按钮一多就分不清哪个是真需要点的。
                      -->
                      <el-button
                        v-if="row.errors?.length"
                        size="small" type="warning" plain
                        :loading="reparsingId === row.id" :disabled="!store.canWrite"
                        @click="reparse(row)"
                      >重新解析</el-button>
                    </el-form-item>
                  </el-form>
                </div>
                <!--
                  结算科目的来源说明：命中了哪条流水、命中的是哪个账户，
                  或者为什么没命中（多账户同额 / 没有对账单）。
                -->
                <div v-if="settlementNote(row)" class="settlement-note">
                  <el-icon><InfoFilled /></el-icon>{{ settlementNote(row) }}
                </div>
                <div v-if="row.errors?.length" class="invoice-errors">
                  <div v-for="error in row.errors" :key="error">{{ error }}</div>
                </div>
                <el-table :data="row.lines" size="small" border class="line-table">
                  <el-table-column type="index" label="#" width="48" />
                  <!--
                    显示 display_name（发票上印的项目名，不含粘在后面的规格型号/
                    数量），规则仍按 name 匹配 —— 见 text_parser 的说明。
                  -->
                  <el-table-column label="发票项目" min-width="260" show-overflow-tooltip>
                    <template #default="{ row: line }">{{ lineName(line) }}</template>
                  </el-table-column>
                  <!--
                    金额显示的是**实际入账**的那个数（resolution.line_amounts），
                    不是 payload 里的原数 —— 两者可能差在最后一项上那几分钱
                    （为了和发票合计对齐）。显示原数会让人以为凭证算错了。
                  -->
                  <el-table-column label="不含税金额" width="120" align="right">
                    <template #default="{ row: line, $index }">{{ lineAmount(row, $index) }}</template>
                  </el-table-column>
                  <el-table-column prop="tax" label="税额" width="100" align="right" />
                  <el-table-column prop="tax_rate" label="税率" width="80" align="center" />
                  <el-table-column label="入账科目" min-width="230">
                    <!--
                      注意这里的 ``row``：外层展开列的参数是发票记录，
                      内层 ``{ row: line }`` 只是把内层的行改名成了 line，
                      所以 ``row`` 仍然是**那张发票**（不是发票里的某一行）。
                    -->
                    <template #default="{ row: line, $index }">
                      <!--
                        已入账的发票改不了科目了（服务端跳过重算），
                        摆个下拉框只会让人以为还能改 —— 直接显示
                        「编码 名称」，这一格保持只读。
                      -->
                      <span v-if="isCommitted(row)" class="subject-text">{{ lineSubjectText(row, $index) }}</span>
                      <!--
                        下拉**不占满**整格：占满了「兜底」标记就被挤到第二行，
                        一个格子里两行字看着是散的。留出标记的位置，两样东西
                        同一行。
                      -->
                      <el-select v-else :model-value="lineSubjectFor(row, $index)" :disabled="!store.canWrite" filterable clearable :persistent="false" style="width: 160px" @update:model-value="(v) => setLineSubject(row, $index, v)">
                        <el-option v-for="s in subjects" :key="s.id" :label="subjectLabel(s)" :value="s.id" />
                      </el-select>
                      <!--
                        落到兜底科目的行要标出来：它是「先记着、等人核对」，
                        不是「已经对了」。不标出来的话，界面上看不出哪几行是
                        系统替用户猜的。
                      -->
                      <el-tag v-if="isFallbackLine(row, $index)" size="small" type="warning" class="fallback-tag">兜底</el-tag>
                    </template>
                  </el-table-column>
                </el-table>
                <div class="rounding-note" v-if="row.resolution?.rounding">
                  已有一项按发票合计调整 {{ row.resolution.rounding }}，各项目金额之和与发票金额一致。
                </div>
                <div class="entry-preview" v-if="row.resolution?.entries?.length">
                  <span class="muted">凭证预览：</span>
                  <span v-for="(entry, index) in row.resolution.entries" :key="index" class="entry-chip">
                    {{ entry.direction === 'DEBIT' ? '借' : '贷' }} {{ entrySubjectText(entry) }} {{ entry.amount }}
                    <span v-if="entry.summary" class="entry-summary">{{ entry.summary }}</span>
                  </span>
                </div>
                <!--
                  净额抵成 0 的项目（整额折扣）**不进凭证**：0 金额的分录
                  存不进去，存进去也是一行查不出是哪张票的 0。
                  要说清楚，否则用户看见「凭证预览」里没有这一项，
                  会以为系统漏记了。
                -->
                <div class="zero-note" v-if="row.resolution?.zero_lines?.length">
                  有 {{ row.resolution.zero_lines.length }} 项折扣后净额为 0，未生成分录。
                </div>
                </template>
              </div>
            </template>
          </el-table-column>
          <!-- 分页票（一张发票拆成几页印）合成后跨多页，显示成「1-3」而不是「1」 -->
          <el-table-column prop="page_index" label="页码" width="80" align="center">
            <template #default="{ row }">{{ pageLabel(row) }}</template>
          </el-table-column>
          <el-table-column v-if="batch.sources?.length > 1" prop="source_file" label="来源文件" min-width="170" show-overflow-tooltip />
          <el-table-column prop="invoice_no" label="发票号码" min-width="190" />
          <el-table-column prop="issue_date" label="开票日期" width="120" />
          <el-table-column label="类型" width="85" align="center"><template #default="{ row }">{{ invoiceTypeText(row.invoice_type) }}</template></el-table-column>
          <el-table-column label="状态" width="100" align="center"><template #default="{ row }"><el-tag size="small" :type="invoiceType(row.status)">{{ invoiceStatusText(row.status) }}</el-tag></template></el-table-column>
          <el-table-column label="价税合计" width="120" align="right"><template #default="{ row }">{{ row.resolution?.total || row.meta?.total_incl_tax || '' }}</template></el-table-column>
        </el-table>
      </el-card>
    </template>

    <el-dialog v-model="ruleVisible" title="新增发票科目规则" width="520px">
      <el-form :model="ruleForm" label-width="90px">
        <el-form-item label="匹配对象"><el-radio-group v-model="ruleForm.match_type"><el-radio value="SELLER">供应商/客户</el-radio><el-radio value="ITEM">项目名称</el-radio></el-radio-group></el-form-item>
        <el-form-item label="发票类型"><el-select v-model="ruleForm.invoice_type" style="width: 200px"><el-option label="进项和销项" value="BOTH" /><el-option label="仅进项" value="INPUT" /><el-option label="仅销项" value="OUTPUT" /></el-select></el-form-item>
        <el-form-item label="关键词"><el-input v-model="ruleForm.keyword" placeholder="如：办公用品、某某科技有限公司" /></el-form-item>
        <el-form-item label="入账科目"><el-select v-model="ruleForm.subject_id" filterable :persistent="false" style="width: 100%"><el-option v-for="s in subjects" :key="s.id" :label="subjectLabel(s)" :value="s.id" /></el-select></el-form-item>
        <el-form-item label="优先级"><el-input-number v-model="ruleForm.priority" :min="-1000" :max="1000" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="ruleVisible = false">取消</el-button><el-button type="primary" :loading="savingRule" @click="createRule">保存</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Check, FolderOpened, InfoFilled, Plus, Refresh, Upload } from '@element-plus/icons-vue'
import * as invoiceApi from '@/api/invoice'
import * as statementApi from '@/api/bankstatement'
import * as subjectApi from '@/api/subject'
import {
  DEFAULT_UPLOAD_LIMITS, filesFromDataTransfer, humanBytes, isCommittedRow, isIgnoredRow, partitionFiles, skipNote,
} from './invoice_upload'
import { formatDateTime } from '@/utils/datetime'
import { useSessionStore } from '@/stores/session'

const store = useSessionStore()
const tab = ref('batches')
const subjects = ref([])
const rules = ref([])
const history = ref([])
const batch = ref(null)
const files = ref([])
const skipped = ref([])
const folderInput = ref(null)
// 上限由后端给（/invoices/limits）。兜底值只是让接口没回来之前界面也有个数，
// 不能当成契约 —— 真超了后端会拒，兜底值偏小的话只是提前少选几个文件。
const uploadLimits = ref({ ...DEFAULT_UPLOAD_LIMITS })
const totalBytes = computed(() => files.value.reduce((sum, file) => sum + (file.size || 0), 0))
const uploading = ref(false)
const committing = ref(false)
const previewing = ref(false)
// 正在重新解析的那一张（按钮上的 loading）。用 id 而不是布尔量 ——
// 一批几十张发票，哪一张转圈要能看出来。
const reparsingId = ref(null)
// 正在点名忽略/取消忽略的那一行。用来挡重复点击，也是按钮上的转圈。
const ignoringId = ref(null)
const savingConfig = ref(false)
const savingRule = ref(false)
// 兜底科目也在这里：saveConfig 整个 config 提交，少一个字段就等于把那一项清空。
const config = reactive({
  taxpayer_id: '',
  output_tax_subject_id: null,
  input_credit_subject_id: null,
  output_debit_subject_id: null,
  input_fallback_subject_id: null,
  output_fallback_subject_id: null,
})
const overrides = reactive({})
// 已加载的对账单份数 —— 页头要讲清楚「结算科目能不能自动认出来」，
// 取决于有没有对账单可匹配。
const statementCount = ref(0)
const ruleVisible = ref(false)
const ruleForm = reactive({ match_type: 'SELLER', invoice_type: 'BOTH', keyword: '', subject_id: null, priority: 0, active: true })

const subjectLabel = (s) => `${s.code} ${s.name}`

/**
 * 结算科目的来源。
 *
 * 三种：MANUAL 人工指定 / STATEMENT 对账单识别 / CONFIG 自动制证配置。
 * **界面上必须标出来** —— 结算科目决定钱记到哪个银行账户，
 * 「这个科目为什么跟我配的不一样」是用户第一个会问的问题。
 */
const settlementSource = (row) => row?.resolution?.settlement_source || ''
const settlementSourceText = (row) => ({
  MANUAL: '人工指定',
  STATEMENT: '对账单识别',
  CONFIG: '配置科目',
}[settlementSource(row)] || settlementSource(row))
const settlementSourceType = (row) => ({
  MANUAL: 'info',
  STATEMENT: 'success',
  CONFIG: 'warning',
}[settlementSource(row)] || 'info')
const settlementNote = (row) => row?.resolution?.settlement_note || ''

const statementHint = computed(() => {
  if (!statementCount.value) return '未加载对账单，结算科目取自动制证配置里的科目'
  return `已加载 ${statementCount.value} 份对账单，点「重新计算预览」按金额与收支方向重新匹配结算科目`
})
const invoiceTypeText = (v) => ({ INPUT: '进项', OUTPUT: '销项', BOTH: '进项和销项', UNKNOWN: '未判断' }[v] || v || '未判断')
const invoiceStatusText = (v) => ({ READY: '可生成', REJECTED: '有问题', COMMITTED: '已生成', IGNORED: '已忽略', DISCARDED: '已作废' }[v] || v)
const invoiceType = (v) => ({ READY: 'success', REJECTED: 'danger', COMMITTED: 'success', IGNORED: 'info', DISCARDED: 'info' }[v] || 'warning')
const itemKey = (file, index) => `${file.name}|${file.size}|${file.lastModified || index}`
// 分页票跨了几页就显示成「起-止」；普通单页发票仍显示一个页码。
const pageLabel = (row) => {
  const span = Number(row.page_span || 1)
  const first = Number(row.page_index || 0) + 1
  return span > 1 ? `${first}-${first + span - 1}` : String(first)
}
// 一句话把「几张可生成 / 几张有问题 / 几张被忽略」讲全 —— 少了「已忽略」这一项
// 的话，被合并进来的封面页会让人以为批次比预想的多。
// 「只显示未入账发票」：开着的时候，表里只剩**还没生成凭证**的那些。
//
// 默认**开着**（用户 2026-10-26 要求）：这个页面是「还剩多少票要入账」
// 的待办清单，默认把已入账的藏起来，打开就看到正事。已入账的那些并没有
// 消失 —— 点「显示全部发票」随时能翻出来，数目也会在旁边讲清楚。
//
// 口径是「有没有凭证」，不是「状态是什么」：一张被点了忽略的发票也没
// 入账，它该留在列表里 —— 用户就是靠这个列表把它取消忽略、补生成凭证的。
const onlyUnbooked = ref(true)
const allInvoices = computed(() => batch.value?.invoices || [])
const visibleInvoices = computed(() => {
  const rows = allInvoices.value
  if (!onlyUnbooked.value) return rows
  return rows.filter(row => !row.voucher_id)
})
// 被筛掉的那几张（已生成凭证的）—— 用来把「藏了多少」讲清楚。
const bookedCount = computed(() => allInvoices.value.filter(row => row.voucher_id).length)
// 开关开着时讲清楚当前视野：看得到几张、藏起来几张。
const filterHint = computed(() => {
  const visible = visibleInvoices.value.length
  const hidden = bookedCount.value
  if (!hidden) return `当前显示未入账的 ${visible} 张`
  return `当前显示未入账的 ${visible} 张，已隐藏 ${hidden} 张已入账发票`
})
// 空表时要讲清楚是「筛掉了」还是「本来就没有」—— 空表本身不说明任何事。
const emptyHint = computed(() => {
  if (onlyUnbooked.value && bookedCount.value) {
    return `这批 ${bookedCount.value} 张发票都已入账 —— 点「显示全部发票」看全部记录。`
  }
  return '这个批次还没有发票记录。'
})
const batchSummaryTitle = computed(() => {
  const current = batch.value
  if (!current) return ''
  const parts = [`共 ${current.total} 张发票`]
  if (current.ready_count) parts.push(`${current.ready_count} 张可生成`)
  // 已入账的张数要讲出来：补生成（取消忽略后再确认）之后，
  // 用户得知道这一批到底生成了多少张，别把两轮加起来算重了。
  if (current.committed_count) parts.push(`${current.committed_count} 张已入账`)
  if (current.error_count) parts.push(`${current.error_count} 张有问题`)
  if (current.ignored_count) parts.push(`${current.ignored_count} 张已忽略`)
  return parts.join('，')
})
const batchText = (v) => ({ PENDING: '待确认', COMMITTED: '已生成', DISCARDED: '已作废' }[v] || v)
const batchType = (v) => ({ PENDING: 'warning', COMMITTED: 'success', DISCARDED: 'info' }[v] || '')

// 选文件、拖文件、选文件夹都汇到这一处：追加、去重、挑出 PDF，
// 非 PDF 进 skipped（会被提示出来，不无声消失）。
function addFiles(list) {
  const { pdfs, skipped: ignored } = partitionFiles(list)
  if (ignored.length) skipped.value = [...new Set([...skipped.value, ...ignored])]
  files.value = [...files.value, ...pdfs]
  const { maxFiles, maxTotalBytes } = uploadLimits.value
  if (files.value.length > maxFiles) {
    ElMessage.warning(`一次最多上传 ${maxFiles} 个文件，多出来的没有加入`)
    files.value = files.value.slice(0, maxFiles)
  }
  if (totalBytes.value > maxTotalBytes) {
    ElMessage.warning(`已选文件合计 ${humanBytes(totalBytes.value)}，超过单次上限 ${humanBytes(maxTotalBytes)}，请先移除几个`)
  }
  return files.value.length
}
function onFile(_value, list) { addFiles(list.map((item) => item.raw || item)) }
function onFolderPicked(event) {
  addFiles(Array.from(event.target.files || []))
  // 清空 value：同一个文件夹连选两次时，否则 change 不会再触发。
  event.target.value = ''
}
async function onDrop(event) {
  const picked = await filesFromDataTransfer(event.dataTransfer)
  if (picked.length) addFiles(picked)
}
function removeFile(index) { files.value.splice(index, 1) }
function clearFiles() { files.value = []; skipped.value = [] }
function ensureOverride(row) {
  if (!overrides[row.id]) overrides[row.id] = { line_subjects: {} }
  overrides[row.id].line_subjects ||= {}
  return overrides[row.id]
}
function invoiceTypeFor(row) { return overrides[row.id]?.invoice_type || row.invoice_type || 'UNKNOWN' }
function lineSubjectFor(row, index) {
  return overrides[row.id]?.line_subjects?.[String(index)] ?? row.resolution?.line_resolution?.[index]?.subject_id ?? null
}
function taxSubjectFor(row) {
  // 进项不单列税额（小微企业不做进项抵扣），只有销项才需要税额科目。
  if (invoiceTypeFor(row) !== 'OUTPUT') return null
  return overrides[row.id]?.tax_subject_id || config.output_tax_subject_id
}
function settlementSubjectFor(row) {
  const input = invoiceTypeFor(row) === 'INPUT'
  const manual = overrides[row.id]?.settlement_subject_id
  if (manual) return manual
  const resolution = row.resolution
  const effectiveType = invoiceTypeFor(row)
  if (resolution && (!resolution.invoice_type || resolution.invoice_type === effectiveType)) {
    const direction = input ? 'CREDIT' : 'DEBIT'
    const entry = resolution.entries?.find((item) => item.direction === direction)
    if (entry?.subject_id) return entry.subject_id
  }
  return input ? config.input_credit_subject_id : config.output_debit_subject_id
}
function setInvoiceType(row, value) { ensureOverride(row).invoice_type = value }
function setTaxSubject(row, value) { ensureOverride(row).tax_subject_id = value }
function setSettlementSubject(row, value) { ensureOverride(row).settlement_subject_id = value }
function setLineSubject(row, index, value) { ensureOverride(row).line_subjects[String(index)] = value }
function invoiceRowClass({ row }) { return row.status === 'REJECTED' ? 'invoice-row-bad' : '' }
// 发票上印的项目名：display_name 是干净的（规格型号/数量没粘进来），没有就退回 name。
function lineName(line) { return line?.display_name || line?.name || '' }
// 这行是不是落到了兜底科目上（服务端算的，不是前端猜的）。
function isFallbackLine(row, index) { return row.resolution?.line_resolution?.[index]?.fallback === true }
// 科目 id → 「编码 名称」。
//
// 服务端返回的 resolution 里已经带了编码和名称；这里再兜一层，
// 是因为科目表按年度各有一份（同一套编码、两套 id），跨年度时前端
// 手里的科目表可能对不上 —— 对不上就只显示 id，不编一个名字上去。
function subjectText(id) {
  if (id === null || id === undefined || id === '') return ''
  const hit = subjects.value.find(item => item.id === id)
  return hit ? subjectLabel(hit) : `科目 #${id}`
}
// 某一行最终记到了哪个科目（已入账的发票只能显示结果，不能改）。
function lineSubjectText(row, index) {
  const item = row.resolution?.line_resolution?.[index]
  if (!item) return ''
  if (item.subject_code) return `${item.subject_code} ${item.subject_name || ''}`.trim()
  return subjectText(item.subject_id)
}
// 凭证预览里那条分录的科目：服务端给了就用服务端的，没给再查本地科目表。
function entrySubjectText(entry) {
  if (entry?.subject_code) return `${entry.subject_code} ${entry.subject_name || ''}`.trim()
  return subjectText(entry?.subject_id)
}
// 这一行**实际入账**的金额：服务端算好的（含与发票合计对齐的那几分钱）。
function lineAmount(row, index) {
  const effective = row.resolution?.line_amounts?.[String(index)]
  return effective ?? row.lines?.[index]?.amount ?? ''
}

async function loadAll() {
  if (!store.bookId) return
  const [cfg, flat, ruleRows, batches, limits, statements] = await Promise.all([
    invoiceApi.getConfig(store.bookId),
    // **年度必带**：科目表每个年度各有一份（后端迁移 0032）
    subjectApi.subjectFlat(store.bookId, { year: store.currentYear }),
    invoiceApi.listRules(store.bookId),
    invoiceApi.listBatches(store.bookId), invoiceApi.getLimits(store.bookId).catch(() => null),
    // 已加载的对账单份数。**拉不到不算错** —— 没建过流水对账的账套拿不到
    // 什么，页头上写一句「未加载对账单」就够了，不该弹一个红条。
    statementApi.listStatements(store.bookId).catch(() => [])
  ])
  if (limits) uploadLimits.value = { maxFiles: limits.max_files, maxTotalBytes: limits.max_total_bytes }
  statementCount.value = (statements || []).length
  Object.assign(config, cfg)
  subjects.value = flat
  rules.value = ruleRows
  history.value = batches
}
async function saveConfig() {
  savingConfig.value = true
  try { Object.assign(config, await invoiceApi.saveConfig(store.bookId, { ...config })); ElMessage.success('发票制证配置已保存') } finally { savingConfig.value = false }
}
async function upload() {
  if (!files.value.length) return
  uploading.value = true
  try {
    batch.value = await invoiceApi.uploadBatch(store.bookId, files.value)
    clearFiles()
    await loadAll()
    ElMessage.success(`解析完成：${batch.value.total} 张发票`)
    if (batch.value.ignored_count) ElMessage.warning(`其中 ${batch.value.ignored_count} 张被忽略`)
    if (batch.value.error_count) ElMessage.warning(`${batch.value.error_count} 张需要核对后才能确认`)
  } finally { uploading.value = false }
}
async function loadBatch(row) {
  batch.value = await invoiceApi.getBatch(store.bookId, row.id || row)
  tab.value = 'batches'
}
function newBatch() { batch.value = null; clearFiles(); Object.keys(overrides).forEach(key => delete overrides[key]) }
function isIgnored(row) { return isIgnoredRow(row, overrides) }
// 已入账（已生成）的发票：服务端跳过重算，这一行**没有可执行的操作**。
function isCommitted(row) { return isCommittedRow(row) }
function overridePayload() { return JSON.parse(JSON.stringify(overrides)) }

/**
 * 把服务端算完的这一批**就地**合并进当前批次。
 *
 * 为什么不整批换掉：整批换掉 → el-table 的 data 变成新数组 →
 * ``default-expand-all`` 重新摊开几十张票（那一堆 DOM 节点就是页面卡
 * 的原因）。用户点「忽略这张」想看的就是这张的说明，换完它却给收起来了。
 *
 * 计数、状态这些整体换；发票行按 id 换 —— 展开状态留着。
 */
function mergeBatchPayload(payload) {
  const current = batch.value
  if (!current) { batch.value = payload; return }
  const { invoices, ...rest } = payload
  Object.assign(current, rest)
  const fresh = new Map((invoices || []).map(row => [row.id, row]))
  for (const row of current.invoices || []) {
    const updated = fresh.get(row.id)
    if (updated) Object.assign(row, updated)
  }
}

/**
 * 点名忽略（或取消忽略）一张发票，**立刻落库**。
 *
 * 以前这一步只是翻一个本地标记：状态那一列还停在「可生成」/「有问题」，
 * 用户得再点一次「重新计算预览」才看得出改没改成。而忽略是用户**已经
 * 做好**的决定 —— 合并件里的封面页、装订页、重复扫描页，一眼就知道不是
 * 发票，不该逼着人再点一次才能看到结果。
 *
 * 落库失败要把本地意图收回来：界面显示的状态不能和库里对不上。
 */
async function toggleIgnored(row) {
  const next = !isIgnored(row)
  const had = Object.prototype.hasOwnProperty.call(overrides[row.id] || {}, 'ignored')
  const before = overrides[row.id]?.ignored
  ensureOverride(row).ignored = next
  ignoringId.value = row.id
  try {
    const payload = await invoiceApi.setRecordIgnored(
      store.bookId, batch.value.id, row.id, next, overridePayload())
    mergeBatchPayload(payload)
    ElMessage.success(next ? '已忽略这张发票' : '已取消忽略')
  } catch (err) {
    if (had) overrides[row.id].ignored = before
    else delete overrides[row.id].ignored
    ElMessage.error(err?.response?.data?.detail || (next ? '忽略失败' : '取消忽略失败'))
  } finally {
    ignoringId.value = null
  }
}
async function recalculate() {
  previewing.value = true
  try { batch.value = await invoiceApi.previewBatch(store.bookId, batch.value.id, overridePayload()); ElMessage.success('预览已重新计算') } finally { previewing.value = false }
}
async function reparse(row) {
  reparsingId.value = row.id
  try {
    batch.value = await invoiceApi.reparseRecord(store.bookId, batch.value.id, row.id, overridePayload())
    const stillBad = batch.value.error_count
    if (stillBad) ElMessage.warning('已重新解析，仍有发票需要核对')
    else ElMessage.success('重新解析完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || '重新解析失败')
  } finally {
    reparsingId.value = null
  }
}
async function commit() {
  await ElMessageBox.confirm(`确认根据 ${batch.value.ready_count} 张发票生成未记账凭证吗？生成后仍可在凭证页面修改。`, '确认生成凭证', { type: 'warning' })
  committing.value = true
  try { await invoiceApi.commitBatch(store.bookId, batch.value.id); ElMessage.success('凭证已生成'); await loadAll(); batch.value = null } finally { committing.value = false }
}
async function discard() {
  await ElMessageBox.confirm(
    '作废后本批次不会生成凭证，<strong>发票记录与原始 PDF 会一并删除</strong>（不保留原文件），省下磁盘空间。历史里仍能看到这批曾经传过。确认作废吗？',
    '作废批次', { type: 'warning', dangerouslyUseHTMLString: true })
  await invoiceApi.discardBatch(store.bookId, batch.value.id)
  ElMessage.success('批次已作废'); newBatch(); await loadAll()
}
function openRule() { Object.assign(ruleForm, { match_type: 'SELLER', invoice_type: 'BOTH', keyword: '', subject_id: null, priority: 0, active: true }); ruleVisible.value = true }
async function createRule() {
  if (!ruleForm.keyword.trim() || !ruleForm.subject_id) { ElMessage.warning('请填写关键词并选择科目'); return }
  savingRule.value = true
  try { await invoiceApi.createRule(store.bookId, { ...ruleForm }); rules.value = await invoiceApi.listRules(store.bookId); ruleVisible.value = false; ElMessage.success('规则已保存') } finally { savingRule.value = false }
}
async function removeRule(row) {
  await ElMessageBox.confirm(`确认删除规则「${row.keyword}」吗？`, '删除规则', { type: 'warning' })
  await invoiceApi.deleteRule(store.bookId, row.id); rules.value = await invoiceApi.listRules(store.bookId)
}

onMounted(loadAll)
</script>

<style scoped>
.invoice-page { padding: 22px 24px 36px; }
.invoice-head, .section-head, .upload-footer, .result-actions, .result-title { display: flex; align-items: center; }
.invoice-head { justify-content: space-between; gap: 18px; margin-bottom: 14px; }
.invoice-head h2 { margin: 0; color: var(--cwq-chrome-strong); font-size: 22px; font-weight: 600; }
.invoice-head p { margin: 6px 0 0; color: var(--cwq-muted); font-size: 13px; }
.invoice-head-actions, .result-actions { gap: 8px; }
.invoice-tabs :deep(.el-tabs__content) { overflow: visible; }
.invoice-config-card, .invoice-upload-card, .invoice-history-card, .invoice-result-card { margin-bottom: 14px; }
.section-head { justify-content: space-between; gap: 14px; font-weight: 600; }
.section-head .muted { font-size: 12px; font-weight: 400; }
/* 控件铺满各自列宽，两列的输入框左边缘才对得齐。inline + 固定 width 是靠像素
   凑的，改一处宽度就散；交给栅格之后不再依赖字面标签有几个字。 */
.invoice-config-form { max-width: 900px; margin-bottom: 0; }
.invoice-config-form :deep(.el-form-item) { margin-bottom: 16px; }
.invoice-config-form :deep(.el-select),
.invoice-config-form :deep(.el-input) { width: 100%; }
.invoice-upload-card :deep(.el-upload-dragger) { padding: 26px 0; }
.upload-actions { display: flex; align-items: center; gap: 12px; margin-top: 12px; }
.upload-actions .muted { font-size: 12px; }
.hidden-input { display: none; }
/* 选中的文件多到几十份时不给它无限长高：内部滚动，标题与上传按钮留在原位。 */
.picked-list { margin-top: 10px; border: 1px solid #e4e7ed; background: #fafbfc; }
.picked-head { display: flex; align-items: center; justify-content: space-between; padding: 6px 10px; border-bottom: 1px solid #e4e7ed; font-size: 12px; color: #475467; }
.picked-scroll { max-height: 180px; overflow-y: auto; }
.picked-row { display: flex; align-items: center; gap: 10px; padding: 4px 10px; font-size: 12px; }
.picked-row .muted { font-size: 12px; }
.picked-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.skipped-note { padding: 6px 10px; border-top: 1px dashed #e4e7ed; color: #8a6d1f; font-size: 12px; }
.upload-footer { justify-content: space-between; margin-top: 14px; }
/* 已入账的行里，「入账科目」是只读文字，不是下拉框 */
.subject-text { color: #303133; font-size: 12px; }
.ignored-note { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 8px 10px; background: #f4f5f7; color: #475467; font-size: 12px; }
.result-title { gap: 10px; }
/* 筛选提示：紧挨着开关，小字灰 —— 别跟按钮抢注意力。 */
.result-hint { font-size: 12px; white-space: nowrap; }
.invoice-empty { color: var(--el-text-color-secondary); padding: 8px 0; }
.mb { margin-bottom: 12px; }
.rule-help { margin-bottom: 12px; }
.invoice-detail { padding: 2px 10px 12px 44px; }
.detail-toolbar { overflow-x: auto; padding: 4px 0 8px; border-bottom: 1px solid #edf0f4; }
.detail-toolbar :deep(.detail-toolbar-form) { display: flex; flex-wrap: nowrap; align-items: center; width: max-content; min-width: 100%; gap: 4px; }
.detail-toolbar :deep(.el-form-item) { flex: 0 0 auto; margin: 0; }
.detail-toolbar :deep(.el-form-item__label) { width: 52px; padding-right: 4px; white-space: nowrap; }
.detail-toolbar :deep(.el-form-item__content) { display: flex; min-width: 0; align-items: center; gap: 4px; }
/* 去掉「这张」那个标签之后，按钮组自己站到这一行的最右边 —— 它是这一张
   发票的操作，不是跟「发票类型 / 税额 / 结算科目」并列的一项。 */
.detail-toolbar :deep(.el-form-item.row-actions) { margin-left: auto; margin-right: 0; }
.detail-toolbar :deep(.el-form-item.row-actions .el-form-item__content) { gap: 8px; }
.invoice-errors { margin: 10px 0; padding: 8px 10px; border-left: 3px solid #d93025; background: #fff5f4; color: #b42318; font-size: 12px; line-height: 1.8; }
.line-table { margin-top: 8px; }
/* 被忽略的发票摊出来的票面明细。见上面的说明：为什么不用 el-table。 */
.ignored-line-table { margin-top: 8px; width: 100%; border-collapse: collapse; font-size: 12px; }
.ignored-line-table th, .ignored-line-table td { border: 1px solid #ebeef5; padding: 5px 8px; text-align: left; }
.ignored-line-table th { background: #f5f7fa; font-weight: 600; color: #606266; }
.ignored-line-table .idx { width: 48px; }
.ignored-line-table .num { text-align: right; }
.ignored-line-table .mid { text-align: center; }
.entry-preview { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; align-items: center; font-size: 12px; }
.entry-summary { color: #667085; }
.rounding-note { margin-top: 8px; font-size: 12px; color: #b45309; }
.zero-note { margin-top: 6px; font-size: 12px; color: #667085; }
.fallback-tag { margin-left: 6px; }
.entry-chip { padding: 4px 7px; border: 1px solid #dfe6ef; background: #f8fafc; color: #344054; }
:deep(.invoice-row-bad) { background: #fff5f4; }
@media (max-width: 900px) {
  .invoice-head { align-items: flex-start; flex-direction: column; }
  .section-head { align-items: flex-start; flex-direction: column; }
  .result-actions { width: 100%; flex-wrap: wrap; }
  .invoice-detail { padding-left: 8px; }
}
.settlement-badge { margin-left: 0; }
.settlement-note {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  padding: 2px 0 8px;
}
</style>
