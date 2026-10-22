<template>
  <!--
    打印模板设计器的**左栏属性检查器**。

    分工约定（和页面主体是分开的，写在这儿免得后面有人改串）：
    - 本组件只管「表单输入 + 列表增删排序」，所有字段都**就地 mutate** props.tpl，
      绝不整体替换 tpl —— 父组件保存、脏标记、撤销都持有同一个对象引用。
    - 新增元素由父组件执行：id 要全局唯一，还要顺带把脏标记点起来，
      所以这里只 emit('add-element', type) 这个意图，不自己造元素。
    - 任何写操作都 emit('change')，父组件自己防抖刷预览；本组件不碰预览。
  -->
  <div class="ti">
    <el-tabs v-model="tab" stretch>
      <!-- ══════════════════════════════ 页面 ══════════════════════════════ -->
      <el-tab-pane label="页面" name="page">
        <div class="ti-page">
          <section class="sec">
            <h4 class="sec-t">纸张</h4>
            <div class="f1">
              <div class="fl">
                <span class="lb">纸型</span>
                <!-- 只读而不是 select：全局只剩 A4（见 utils/paper.js）。留着一个
                     只有一个选项的下拉框，用户会以为还能换，得点开才发现不能 ——
                     而「原来能选、现在不能」这件事该写在标签上，不该让人去点。 -->
                <span class="lb-v">{{ paperLabel(tpl.paper) }}</span>
              </div>
              <div class="fl">
                <span class="lb">方向</span>
                <el-radio-group v-model="tpl.orientation" class="orient" size="small" :disabled="ro" @change="commit">
                  <el-radio-button value="portrait">纵向</el-radio-button>
                  <el-radio-button value="landscape">横向</el-radio-button>
                </el-radio-group>
              </div>
              <!-- 一页几张凭证只在凭证模板里有意义；账簿报表放这句是噪音。 -->
              <div v-if="isVoucher" class="fl">
                <span class="lb">每页凭证数</span>
                <el-select :model-value="slots" size="small" :disabled="ro"
                           class="w-full" @change="setSlots">
                  <el-option v-for="n in SLOTS_OPTIONS" :key="n" :value="n"
                             :label="`每页 ${n} 张`" />
                </el-select>
              </div>
            </div>
          </section>

          <section class="sec">
            <h4 class="sec-t">页边距（mm）</h4>
            <div class="f">
              <div v-for="side in SIDES" :key="side" class="fl">
                <span class="lb">{{ SIDE_CN[side] }}边</span>
                <el-input-number v-model="tpl.margins[side]" :min="0" :max="100" :step="1"
                                 size="small" :controls="false" :disabled="ro" @change="commit" />
              </div>
            </div>
            <p class="tip">也可以直接在右边预览的纸边上拖动，效果一样。</p>
          </section>

          <!-- 凭证的字号跟着每个元素走（元素属性里有 font_size），
               这里只留字体名；正文字号仅非凭证显示 -->
          <section class="sec">
            <h4 class="sec-t">字体</h4>
            <div class="f1">
              <div class="fl">
                <span class="lb">字体</span>
                <el-input v-model="tpl.font.family" size="small" :disabled="ro"
                          @input="touch" @change="commit" />
              </div>
              <div v-if="!isVoucher" class="fl">
                <span class="lb">字号（pt）</span>
                <el-input-number v-model="tpl.font.size" :min="6" :max="24" :step="0.5"
                                 size="small" :disabled="ro" @change="commit" />
              </div>
            </div>
          </section>

          <template v-if="!isVoucher">
            <!-- data-field 是给「在预览纸上直接点区块」用的锚点：
                 点纸上的标题，页面就滚到这里并聚焦对应输入框。 -->
            <section class="sec" data-field="title">
              <h4 class="sec-t">标题</h4>
              <div class="f1">
                <div class="fl">
                  <span class="lb">主标题</span>
                  <el-input v-model="tpl.title.text" size="small" :disabled="ro"
                            @input="touch" @change="commit" />
                </div>
                <div class="f">
                  <div class="fl">
                    <span class="lb">字号（pt）</span>
                    <el-input-number v-model="tpl.title.size" :min="8" :max="40"
                                     size="small" :disabled="ro" @change="commit" />
                  </div>
                  <div class="fl">
                    <span class="lb">字形</span>
                    <el-checkbox v-model="tpl.title.bold" :disabled="ro" @change="commit">加粗</el-checkbox>
                  </div>
                </div>
                <div class="fl" data-field="subtitle">
                  <span class="lb">副标题</span>
                  <el-input v-model="tpl.subtitle.text" size="small" placeholder="留空则显示期间"
                            :disabled="ro" @input="touch" @change="commit" />
                </div>
              </div>
            </section>

            <section class="sec" data-field="meta">
              <h4 class="sec-t">页眉页脚</h4>
              <div class="f1">
                <!-- meta 是 10 个显示项的开关。tpl.meta 存的是 true/false，
                     这里用 computed 在「对象」和「勾中的 key 数组」之间来回翻译，
                     免得每次勾选都手写一遍 for 循环。 -->
                <div class="fl">
                  <span class="lb">页眉显示项</span>
                  <el-checkbox-group v-model="metaOn" :disabled="ro" @change="commit">
                    <el-checkbox v-for="(label, key) in META_CN" :key="key" :value="key">{{ label }}</el-checkbox>
                  </el-checkbox-group>
                </div>
                <div class="fl" data-field="footer">
                  <span class="lb">页脚文字</span>
                  <el-input v-model="tpl.footer.text" size="small" placeholder="自定义页脚文字"
                            :disabled="ro" @input="touch" @change="commit" />
                </div>
                <div class="fl">
                  <span class="lb">页码</span>
                  <div class="ops">
                    <el-checkbox v-model="tpl.footer.show_page_no" :disabled="ro" @change="commit">显示页码</el-checkbox>
                  </div>
                </div>
                <div class="fl">
                  <span class="lb">页码格式</span>
                  <el-input v-model="tpl.footer.format" size="small" :disabled="ro"
                            @input="touch" @change="commit" />
                </div>
              </div>
            </section>
          </template>

          <!-- ══════════════════════════ 账簿版式 ══════════════════════════
               总账与明细账的页眉页脚不走通用那套（meta / footer）—— 用友的
               页眉是「标题 + 右上角账簿信息 + 页号 + 本币名称」，页脚是
               「核算单位 / 制表 / 打印日期」三段。这几段都在这里设，
               别的地方不重复摆控件。 -->
          <section v-if="hasLedgerBook" class="sec" data-field="ledger-book">
            <h4 class="sec-t">账簿版式</h4>
            <div class="f1">
              <div class="fl">
                <span class="lb">本币名称</span>
                <el-input v-model="lb.currency_name" size="small" :disabled="ro"
                          @input="touch" @change="commit" />
              </div>
              <div class="fl">
                <span class="lb">右上角标记</span>
                <el-input v-model="lb.book_label" size="small" placeholder="留空则不印"
                          :disabled="ro" @input="touch" @change="commit" />
              </div>
              <div class="fl">
                <span class="lb">科目标题</span>
                <el-input v-model="lb.subject_caption" size="small" :disabled="ro"
                          @input="touch" @change="commit" />
              </div>
              <div class="fl">
                <span class="lb">页号格式</span>
                <el-input v-model="lb.page_label" size="small" :disabled="ro"
                          @input="touch" @change="commit" />
              </div>
              <div class="fl">
                <span class="lb">页脚左段</span>
                <el-input v-model="lb.footer_left" size="small" :disabled="ro"
                          @input="touch" @change="commit" />
              </div>
              <div class="fl">
                <span class="lb">页脚中段</span>
                <el-input v-model="lb.footer_center" size="small" :disabled="ro"
                          @input="touch" @change="commit" />
              </div>
              <div class="fl">
                <span class="lb">页脚右段</span>
                <el-input v-model="lb.footer_right" size="small" :disabled="ro"
                          @input="touch" @change="commit" />
              </div>
              <div class="f">
                <div class="fl">
                  <span class="lb">每页行数</span>
                  <el-input-number v-model="lb.rows_per_page" :min="1" :max="100" :step="1"
                                   size="small" :controls="false" :disabled="ro" @change="commit" />
                </div>
                <div class="fl">
                  <span class="lb">行高（mm）</span>
                  <el-input-number v-model="lb.row_height_mm" :min="3" :max="20" :step="0.5"
                                   size="small" :controls="false" :disabled="ro" @change="commit" />
                </div>
              </div>
              <!-- 「不打印无发生额科目」**不在模板里**：它是**这一次打印**的开关，
                   住在打印预览弹窗左栏、默认打开（用户 2026-10-22 批注 2）。
                   模板里再摆一份，同一个设置就有两个地方能改 —— 改了一处、
                   另一处不动，纸上出来的是旧值。后端 validate_template
                   也会把老模板里存的那份 skip_empty 清掉。 -->
            </div>
            <p class="tip">
              占位符：<code>{company}</code> 单位、<code>{preparer}</code> 制表人、
              <code>{print_date}</code> 打印日期、<code>{subject}</code> 科目名、
              <code>{subject_code}</code> 科目编码、<code>{page}</code>/<code>{pages}</code> 页码。
            </p>
          </section>

          <section class="sec" data-field="table">
            <h4 class="sec-t">表格</h4>
            <div class="f">
              <div v-if="!isVoucher" class="fl">
                <span class="lb">边框</span>
                <el-switch v-model="tpl.body.border" size="small" :disabled="ro" @change="commit" />
              </div>
              <div class="fl">
                <span class="lb">重复表头</span>
                <el-switch v-model="tpl.body.repeat_header" size="small" :disabled="ro" @change="commit" />
              </div>
            </div>
          </section>

          <section v-if="isVoucher" class="sec">
            <h4 class="sec-t">分页编号</h4>
            <div class="fl">
              <span class="lb">凭证字号格式</span>
              <el-input v-model="tpl.voucher.split_format" size="small" :disabled="ro"
                        @input="touch" @change="commit" />
            </div>
            <p class="tip">
              按这个格式给凭证字号加分页号：<b class="ex">{{ splitExample }}</b>；
              超过 {{ rowCountTip }} 行拆成多张时页码跟着变（如 002/{{ String(pages).padStart(3, '0') }}），
              不拆的是 001/001。占位符
              <code>{page}</code> 本张页码、<code>{pages}</code> 总页数、<code>{first}</code> 起始行、
              <code>{last}</code> 结束行、<code>{lines}</code> 总行数，数字自动补零到三位。
            </p>
          </section>
        </div>
      </el-tab-pane>

      <!-- ══════════════════════════════ 元素 ══════════════════════════════ -->
      <el-tab-pane v-if="isVoucher" label="元素" name="elements">
        <div class="ti-elements">
          <section class="sec">
            <h4 class="sec-t">添加控件</h4>
            <!-- 新增本身交给父组件（唯一 id + 脏标记），这里只发意图 -->
            <div class="pal">
              <button v-for="b in PALETTE" :key="b.key" type="button" class="pal-btn"
                      :disabled="ro" @click="emit('add-element', b.key)">
                <el-icon><component :is="b.icon" /></el-icon>{{ b.label }}
              </button>
            </div>
          </section>

          <section class="sec">
            <h4 class="sec-t">元素<span class="cnt">{{ elements.length }}</span></h4>
            <ul v-if="elements.length" class="elist">
              <li v-for="(el, i) in elements" :key="el.id"
                  :class="{ on: el.id === selectedId, off: !isVisible(el) }"
                  @click="emit('select', el.id)">
                <span class="ename">{{ elementName(el) }}</span>
                <span class="eops">
                  <el-button link :icon="isVisible(el) ? View : Hide" :title="isVisible(el) ? '隐藏' : '显示'"
                             :disabled="ro" @click.stop="toggleVisible(el)" />
                  <el-button link :icon="Top" title="上移" :disabled="ro || i === 0"
                             @click.stop="moveElement(i, -1)" />
                  <el-button link :icon="Bottom" title="下移" :disabled="ro || i === elements.length - 1"
                             @click.stop="moveElement(i, 1)" />
                  <el-button link type="danger" :icon="Delete" title="删除"
                             :disabled="ro" @click.stop="removeElement(i)" />
                </span>
              </li>
            </ul>
            <p v-else class="tip">还没有控件，从上面挑一个加进来。</p>
          </section>

          <section class="sec">
            <h4 class="sec-t">属性</h4>
            <template v-if="cur">
              <!-- 内容字段。表格/线条/矩形没有 bind 可选，显示类型名占位 -->
              <div class="fl">
                <span class="lb">内容字段</span>
                <el-select v-if="cur.type === 'text'" v-model="cur.bind" size="small" filterable
                           :disabled="ro" @change="onBindChange">
                  <el-option v-for="f in FIELDS" :key="f.value" :label="f.label" :value="f.value" />
                </el-select>
                <span v-else class="ro-type">{{ TYPE_CN[cur.type] }}</span>
              </div>

              <template v-if="cur.type === 'text'">
                <div class="fl">
                  <span class="lb">文字</span>
                  <el-input v-model="cur.text" type="textarea" :rows="2" :disabled="ro"
                            placeholder="可直接输入；用 {字段} 插入数据"
                            @input="touch" @change="commit" />
                </div>
                <div class="f">
                  <div class="fl">
                    <span class="lb">字号（pt）</span>
                    <el-input-number v-model="cur.font_size" :min="6" :max="40" :step="1"
                                     size="small" :disabled="ro" @change="commit" />
                  </div>
                  <div class="fl">
                    <span class="lb">对齐</span>
                    <el-select v-model="cur.align" size="small" :disabled="ro" @change="commit">
                      <el-option label="左" value="left" />
                      <el-option label="中" value="center" />
                      <el-option label="右" value="right" />
                    </el-select>
                  </div>
                </div>
                <div class="ops">
                  <el-checkbox v-model="cur.bold" :disabled="ro" @change="commit">加粗</el-checkbox>
                  <!-- 拆页后只在尾页出现。合计大写默认开着：一张凭证拆成几张之后
                       只有最后一张是完整的，每张都印合计会让人以为是整张凭证的合计。 -->
                  <el-checkbox v-model="lastPageOnly" :disabled="ro" @change="commit">仅在尾页显示</el-checkbox>
                </div>
              </template>

              <template v-if="cur.type === 'table'">
                <div class="f">
                  <div class="fl">
                    <span class="lb">行高（mm）</span>
                    <el-input-number :model-value="Math.round(rowMm * 10) / 10" :min="3" :max="60" :step="0.5"
                                     size="small" :controls="false" :disabled="ro"
                                     @change="setRowMm" />
                  </div>
                  <div class="fl">
                    <span class="lb">分录行数</span>
                    <el-input-number :model-value="rowCount" :min="1" :max="5" :step="1"
                                     size="small" :controls="false" :disabled="ro" @change="setRows" />
                  </div>
                </div>
                <p class="tip">分录行与合计行等高，改行高等于改表格高度（最多 5 行）。</p>

                <div class="f">
                  <div class="fl">
                    <span class="lb">显示表头</span>
                    <el-checkbox :model-value="cur.show_header !== false" :disabled="ro"
                                 @change="setShowHeader">显示</el-checkbox>
                  </div>
                  <div class="fl">
                    <span class="lb">列头高（mm）</span>
                    <el-input-number :model-value="Math.round(headerMm * 10) / 10" :min="0" :max="60" :step="0.5"
                                     size="small" :controls="false" :disabled="ro || cur.show_header === false"
                                     @change="setHeaderMm" />
                  </div>
                </div>
                <p class="tip">不显示表头时，剩下的高度全部分给分录行和合计行。</p>

                <div class="fl">
                  <span class="lb">合计文字</span>
                  <el-input v-model="totalLabel" size="small" maxlength="8" :disabled="ro"
                            @input="touch" @change="commit" />
                </div>

                <div class="colw">
                  <div v-for="(c, i) in tpl.columns" :key="i" class="fl cw">
                    <span class="lb">{{ c.label || (c.key === 'line_no' ? '序号' : c.key) }}</span>
                    <el-input-number :model-value="c.width" :min="MIN_COL_PCT" :max="80" :step="0.5"
                                     size="small" :controls="false" :disabled="ro"
                                     @change="(v) => setColWidth(i, v)" />
                  </div>
                </div>
                <p class="tip">列宽是百分比，改完自动摊回 100%；也可以直接在预览上左右拖列分隔线。</p>
              </template>

              <template v-if="cur.type === 'line' || cur.type === 'rectangle'">
                <div class="f">
                  <div class="fl">
                    <span class="lb">线宽（mm）</span>
                    <el-input-number :model-value="cur.line_width ?? 0.3" :min="0.1" :max="3" :step="0.1"
                                     size="small" :controls="false" :disabled="ro" @change="setLineWidth" />
                  </div>
                  <div class="fl">
                    <span class="lb">线型</span>
                    <el-select :model-value="cur.line_style || 'solid'" size="small" :disabled="ro"
                               @change="setLineStyle">
                      <el-option label="实线" value="solid" />
                      <el-option label="虚线" value="dashed" />
                      <el-option label="点线" value="dotted" />
                    </el-select>
                  </div>
                </div>
              </template>

              <h4 class="sec-t">位置与尺寸（%）</h4>
              <div class="f">
                <div class="fl">
                  <span class="lb">X</span>
                  <el-input-number v-model="cur.x" :min="0" :max="100" :step="0.5" size="small"
                                   :controls="false" :disabled="ro" @change="commitGeom" />
                </div>
                <div class="fl">
                  <span class="lb">Y</span>
                  <el-input-number v-model="cur.y" :min="0" :max="100" :step="0.5" size="small"
                                   :controls="false" :disabled="ro" @change="commitGeom" />
                </div>
                <div class="fl">
                  <span class="lb">宽</span>
                  <el-input-number v-model="cur.w" :min="1" :max="100" :step="0.5" size="small"
                                   :controls="false" :disabled="ro" @change="commitGeom" />
                </div>
                <div class="fl">
                  <span class="lb">高</span>
                  <el-input-number v-model="cur.h" :min="1" :max="100" :step="0.5" size="small"
                                   :controls="false" :disabled="ro" @change="commitGeom" />
                </div>
              </div>
              <div class="ops">
                <el-button size="small" :disabled="ro" @click="alignH('left')">靠左</el-button>
                <el-button size="small" :disabled="ro" @click="alignH('center')">水平居中</el-button>
                <el-button size="small" :disabled="ro" @click="alignH('right')">靠右</el-button>
                <el-button size="small" :disabled="ro" @click="alignV('top')">靠上</el-button>
                <el-button size="small" :disabled="ro" @click="alignV('middle')">垂直居中</el-button>
                <el-button size="small" :disabled="ro" @click="alignV('bottom')">靠下</el-button>
              </div>

              <!-- 表格的这个开关管的是框线，不是显不显示：表格要是能被整个藏掉，
                   「显示/隐藏」就得挪到元素列表去，那儿已经有了。 -->
              <div v-if="cur.type === 'table'" class="fl">
                <span class="lb">框线</span>
                <el-switch :model-value="cur.border !== false" size="small" :disabled="ro"
                           @change="setBorder" />
              </div>
              <div v-else class="fl">
                <span class="lb">显示</span>
                <el-switch :model-value="cur.visible !== false" size="small" :disabled="ro"
                           @change="setVisible" />
              </div>

              <el-button class="del-btn" size="small" type="danger" plain :disabled="ro"
                         :icon="Delete" @click="removeElement(curIndex)">
                删除该控件
              </el-button>
            </template>
            <p v-else class="tip">在上面的元素列表里选一个控件，这里调它的属性。</p>
          </section>
        </div>
      </el-tab-pane>

      <!-- ══════════════════════════════ 列 ══════════════════════════════ -->
      <el-tab-pane label="列" name="columns">
        <div class="ti-columns">
          <el-table :data="tpl.columns" size="small" border class="cols">
            <el-table-column label="字段" width="96">
              <template #default="{ row }">
                <el-select v-model="row.key" size="small" filterable :disabled="ro"
                           @change="commit">
                  <el-option v-for="k in COLUMN_KEYS" :key="k" :label="k" :value="k" />
                </el-select>
              </template>
            </el-table-column>
            <el-table-column label="表头" width="84">
              <template #default="{ row }">
                <el-input v-model="row.label" size="small" :disabled="ro"
                          @input="touch" @change="commit" />
              </template>
            </el-table-column>
            <el-table-column label="宽度" width="84">
              <template #default="{ row }">
                <el-input-number v-model="row.width" :min="0.5" :step="0.5" size="small"
                                 :controls="false" :disabled="ro" @change="commit" />
              </template>
            </el-table-column>
            <el-table-column label="对齐" width="72">
              <template #default="{ row }">
                <el-select v-model="row.align" size="small" :disabled="ro" @change="commit">
                  <el-option label="左" value="left" />
                  <el-option label="中" value="center" />
                  <el-option label="右" value="right" />
                </el-select>
              </template>
            </el-table-column>
            <el-table-column width="86" align="center">
              <template #default="{ $index }">
                <div class="ops">
                  <el-button link :icon="Top" title="上移" :disabled="ro || $index === 0"
                             @click="moveColumn($index, -1)" />
                  <el-button link :icon="Bottom" title="下移" :disabled="ro || $index === tpl.columns.length - 1"
                             @click="moveColumn($index, 1)" />
                  <!-- 至少留一列，否则模板是非法的（后端 validate_template 会拒收） -->
                  <el-button link type="danger" :icon="Delete" title="删除" :disabled="ro || tpl.columns.length <= 1"
                             @click="removeColumn($index)" />
                </div>
              </template>
            </el-table-column>
          </el-table>

          <el-button class="add-col" size="small" :icon="Plus" :disabled="ro" @click="addColumn">添加列</el-button>

          <section class="sec">
            <h4 class="sec-t">可用字段</h4>
            <el-collapse v-model="openFieldGroup">
              <el-collapse-item v-for="g in FIELD_DOC" :key="g.title" :name="g.title">
                <template #title>
                  <span class="gt">{{ g.title }}</span>
                  <span class="cnt">{{ g.items.length }}</span>
                </template>
                <dl class="fdoc">
                  <template v-for="f in g.items" :key="f.key">
                    <dt><code>{{ f.key }}</code></dt>
                    <dd>{{ f.desc }}</dd>
                  </template>
                </dl>
              </el-collapse-item>
            </el-collapse>
          </section>
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { Delete, Document, Hide, Minus, Plus, Top, Bottom, Tickets, View, Crop } from '@element-plus/icons-vue'
import { SLOTS_OPTIONS, paperLabel as labelPaper, slotsPerPage } from '@/utils/paper'

const props = defineProps({
  activeTab: { type: String, default: 'page' },
  /** 草稿模板，就地 mutate。父组件保证永远是完整骨架，所以模板里可以放心写 tpl.margins.top。 */
  tpl: { type: Object, required: true },
  isVoucher: { type: Boolean, default: false },
  /** 当前选中的凭证元素 id，可为 null。id 的生成在父组件，本组件只读。 */
  selectedId: { type: String, default: null },
  canWrite: { type: Boolean, default: true },
  /** [{ key, w, h }]，w/h 毫米，来自后端 meta.papers（经 utils/paper 收敛）。 */
  paperOptions: { type: Array, default: () => [] },
})
const emit = defineEmits(['update:activeTab', 'change', 'select', 'add-element'])

/** A4 的毫米尺寸。meta 没加载出来时 slotMm 也得有尺寸可用。 */
const PAPER_SPEC = { A4: { w: 210, h: 297 } }

const SIDES = ['top', 'right', 'bottom', 'left']
const SIDE_CN = { top: '上', right: '右', bottom: '下', left: '左' }
const META_CN = {
  company: '单位', book: '账套', period: '期间', accountant: '制单',
  checker: '审核', cashier: '出纳', voucher_no: '凭证号', date: '日期',
  attachments: '附单据', remark: '备注',
}
/** 字段说明：key 必须和 COLUMN_KEYS / 后端渲染器认识的字段一致。 */
const FIELD_DOC = [
  {
    title: '凭证分录',
    items: [
      { key: 'line_no', desc: '分录行号' },
      { key: 'summary', desc: '摘要' },
      { key: 'subject', desc: '会计科目（编码 + 名称）' },
      { key: 'debit', desc: '借方金额' },
      { key: 'credit', desc: '贷方金额' },
    ],
  },
  {
    title: '账簿通用',
    items: [
      { key: 'date', desc: '凭证日期' },
      { key: 'month', desc: '月份' },
      { key: 'day', desc: '日期' },
      { key: 'voucher_no', desc: '凭证字号' },
      { key: 'direction', desc: '借贷方向' },
      { key: 'balance', desc: '该科目余额' },
    ],
  },
  {
    title: '科目余额表',
    items: [
      { key: 'code', desc: '科目编码' },
      { key: 'name', desc: '科目名称' },
      { key: 'level', desc: '科目级次' },
      { key: 'opening_debit', desc: '期初借方' },
      { key: 'opening_credit', desc: '期初贷方' },
      { key: 'period_debit', desc: '本期借方发生额' },
      { key: 'period_credit', desc: '本期贷方发生额' },
      { key: 'closing_debit', desc: '期末借方' },
      { key: 'closing_credit', desc: '期末贷方' },
      { key: 'opening', desc: '期初余额' },
      { key: 'closing', desc: '期末余额' },
    ],
  },
  {
    title: '报表',
    items: [
      { key: 'label', desc: '项目名称' },
      { key: 'value0', desc: '本期金额（第 1 列）' },
      { key: 'value1', desc: '本期金额（第 2 列）' },
      { key: 'value2', desc: '本期金额（第 3 列）' },
    ],
  },
]
const COLUMN_KEYS = [
  'line_no', 'summary', 'subject', 'debit', 'credit',
  'date', 'month', 'day', 'voucher_no', 'direction', 'balance',
  'code', 'name', 'level',
  'opening_debit', 'opening_credit', 'period_debit', 'period_credit',
  'closing_debit', 'closing_credit', 'opening', 'closing',
  'label', 'value0', 'value1', 'value2',
]

const PALETTE = [
  { key: 'text', label: '文本', icon: Document },
  { key: 'table', label: '表格', icon: Tickets },
  { key: 'line', label: '线条', icon: Minus },
  { key: 'rectangle', label: '矩形', icon: Crop },
]
const TYPE_CN = { text: '文本', table: '表格', line: '线条', rectangle: '矩形' }
const FIELDS = [
  { value: 'company', label: '单位' }, { value: 'book', label: '账套' },
  { value: 'period', label: '期间' }, { value: 'voucher_no', label: '凭证字号' },
  { value: 'date', label: '日期' }, { value: 'attachments', label: '附单据' },
  { value: 'accountant', label: '制单' }, { value: 'checker', label: '审核' },
  { value: 'cashier', label: '出纳' }, { value: 'remark', label: '备注' },
  { value: 'capital', label: '合计大写' }, { value: 'page', label: '页码' },
  { value: 'total', label: '总页数' }, { value: 'custom', label: '自定义' },
]
const FIELD_CN = Object.fromEntries(FIELDS.map((f) => [f.value, f.label]))

/** 表头占表格高度的比例，与后端 VOUCHER_HEAD_PCT 保持一致；
 *  不一致的话这里算出的行高和打印出来的对不上。 */
const HEAD_PCT = 14
const MIN_COL_PCT = 4
/** 两张凭证同页时中间留的缝（mm），与后端 render_voucher_sheet 一致 */
const SLOT_GAP = 3

const openFieldGroup = ref(['凭证分录'])
/** 只读态：所有写控件禁用，但仍照常显示 —— 用户得看得见自己这份版式长什么样 */
const ro = computed(() => !props.canWrite)

const tab = computed({
  get: () => props.activeTab,
  set: (v) => emit('update:activeTab', v),
})
// 切到非凭证范围时「元素」页签不存在（元素是凭证专属结构），
// 停在一个不存在的页签上会显示一片空白，悄悄退回收面板页。
watch(() => props.isVoucher, (v) => { if (!v && props.activeTab === 'elements') tab.value = 'page' })

const elements = computed(() => props.tpl.elements || [])
const cur = computed(() => elements.value.find((e) => e.id === props.selectedId) || null)
const curIndex = computed(() => elements.value.findIndex((e) => e.id === props.selectedId))

/**
 * 账簿版式（总账 / 明细账）的那一组配置。
 *
 * 模板里有没有 ``ledger_book`` 就是「这是不是账簿范围」的唯一判据 ——
 * 后端给总账与明细账的默认模板都带它，凭证/余额表/报表都不带，
 * 而已保存的老模板载入时也会被后端 merge_defaults 补上。
 */
const hasLedgerBook = computed(() => !!props.tpl.ledger_book)
/** 账簿版式的那块配置本身（就地 mutate，不整体替换 tpl）。 */
const lb = computed(() => props.tpl.ledger_book || {})
const rowCount = computed(() => {
  const raw = (props.tpl.voucher || {}).rows_per_page
  return Math.max(1, Math.min(5, Number(raw) || 5))
})
const rowCountTip = computed(() => rowCount.value)
const pages = computed(() => Math.ceil(12 / (rowCount.value || 5)))

function paperLabel(key) {
  return labelPaper(key, props.isVoucher)
}

/** 用一张 12 行的凭证当例子，把分页编号格式串渲染成看得懂的样子。 */
const splitExample = computed(() => {
  const fmt = props.tpl?.voucher?.split_format || '{voucher_no} - {page}/{pages}'
  const total = pages.value
  const width = Math.max(3, String(total).length)
  const z = (n) => String(n).padStart(width, '0')
  return fmt.replace(/\{(\w+)\}/g, (_, k) =>
    ({ voucher_no: '记0001号', page: z(1), pages: z(total),
       first: z(1), last: z(rowCount.value), lines: z(12) }[k] ?? ''))
})

// ------------------------------------------------------------------ 改动通知
/**
 * 两种提交：文字类输入每敲一下就 touch（父组件自己防抖刷预览），
 * 结构性改动（加列、换纸型、调行高）走 commit，顺手做一次归一化。
 * 只读态直接吞掉 —— 控件本来就禁用了，这里是兜底。
 */
function touch() {
  if (!props.canWrite) return
  emit('change')
}
function commit() {
  if (!props.canWrite) return
  ensure()
  normalizeWidths()
  emit('change')
}
function round(n) { return Math.round(n * 10000) / 10000 }
function clamp(n, lo, hi) { return Math.round(Math.max(lo, Math.min(hi, n || 0)) * 10) / 10 }

/** 老模板可能整个子对象都没有（写进去之前它是省掉的），先补齐再改。 */
function ensure() {
  const t = props.tpl
  if (!Array.isArray(t.elements)) t.elements = []
  if (!t.voucher) t.voucher = { rows_per_page: 5, split_number: true, fill_rows: true }
  // meta 的 10 个显示项缺键时按「不显示」补。凭证范围不补 ——
  // 凭证的显隐归元素自己的 visible（老模板里的 meta 开关在载入时就折进 visible 了），
  // 这时候再按缺键补一遍 false，等于凭空关掉一堆用户明明看得见的东西。
  if (!props.isVoucher) {
    if (!t.meta) t.meta = {}
    for (const k of Object.keys(META_CN)) if (!(k in t.meta)) t.meta[k] = false
  }
}

/** 列宽是百分比，改完必须重新摊到 100%，否则打印出来总宽就对不上（后端也这么做一遍）。 */
function normalizeWidths() {
  const cols = props.tpl.columns || []
  const total = cols.reduce((s, c) => s + (Number(c.width) || 0), 0) || 1
  cols.forEach((c) => { c.width = round((Number(c.width) || 0) / total * 100) })
}

function setSlots(n) {
  // 改「一页几张」要同时落到 voucher.slots_per_page —— 渲染层只读这一个字段
  // （slotMm 的毫米换算也跟着它走），写到别处等于改了没改。
  props.tpl.voucher = { ...(props.tpl.voucher || {}), slots_per_page: n }
  commit()
}

/** meta 存的是 {key: true/false}，复选组要的是「勾了哪些 key」。 */
const metaOn = computed({
  get: () => Object.keys(props.tpl?.meta || {}).filter((k) => props.tpl.meta[k]),
  set: (keys) => {
    for (const k of Object.keys(props.tpl.meta || {})) props.tpl.meta[k] = keys.includes(k)
  },
})

// ------------------------------------------------------------------ 列
function moveColumn(i, d) {
  const arr = props.tpl.columns
  const j = i + d
  if (j < 0 || j >= arr.length) return
  ;[arr[i], arr[j]] = [arr[j], arr[i]]
  commit()
}
function addColumn() {
  props.tpl.columns.push({ key: 'summary', label: '新列', width: 10, align: 'left', indent: 0 })
  commit()
}
function removeColumn(i) {
  if (props.tpl.columns.length <= 1) return   // 至少留一列，否则模板是非法的
  props.tpl.columns.splice(i, 1)
  commit()
}

// ------------------------------------------------------------------ 元素列表
const NAME_BY_ID = { title: '标题', subtitle: '副标题', footer: '页脚', table: '分录表格' }
function elementName(el) {
  if (NAME_BY_ID[el.id]) return NAME_BY_ID[el.id]
  if (el.type === 'table') return '分录表格'
  if (el.type === 'line') return '线条'
  if (el.type === 'rectangle') return '矩形'
  if (el.bind && el.bind !== 'custom') return FIELD_CN[el.bind] || el.bind
  return (el.text || '文本').slice(0, 8)
}
/** 老数据没有 visible 字段，后端也是按「有就看得见」处理，这里保持一致。 */
function isVisible(el) { return el.visible !== false }
function setVisible(v) { if (cur.value) { cur.value.visible = !!v; commit() } }
function toggleVisible(el) { el.visible = !isVisible(el); commit() }
function moveElement(i, d) {
  const arr = elements.value
  const j = i + d
  if (j < 0 || j >= arr.length) return
  ;[arr[i], arr[j]] = [arr[j], arr[i]]
  commit()
}
/**
 * 删除就地 splice。父组件自己持有的 selectedId 会因此指向一个不存在的元素，
 * cur 取不到就退化成 null、属性区显示引导文案 —— 不在这儿 emit 一个没在契约里的
 * remove 事件，免得父组件多一套要维护的同步逻辑。
 */
function removeElement(i) {
  const arr = elements.value
  if (i < 0 || i >= arr.length) return
  arr.splice(i, 1)
  commit()
}

// ------------------------------------------------------------------ 元素属性
function onBindChange(bind) {
  // 换字段时把占位符一起换掉，否则文字里会留着上一个字段的 {…}。
  const def = { voucher_no: '凭证字号：{voucher_no}', date: '日期：{date}', remark: '备注：{remark}',
                company: '单位：{company}', period: '期间：{period}', attachments: '附单据：{attachments}',
                capital: '合计大写：{capital}', page: '第 {page} 页', total: '共 {total} 页' }[bind]
  if (def) cur.value.text = def
  else if (bind === 'custom') cur.value.text = '自定义文字'
  commit()
}

/** 老模板里没有这个字段，按绑定字段给默认值 —— 与后端 validate_template 同一套规则。 */
const lastPageOnly = computed({
  get: () => cur.value?.last_page_only ?? (cur.value?.bind === 'capital'),
  set: (v) => { if (cur.value) { cur.value.last_page_only = !!v; commit() } },
})
const totalLabel = computed({
  get: () => cur.value?.total_label ?? '合　计',
  set: (v) => { if (cur.value) { cur.value.total_label = String(v ?? ''); commit() } },
})

/** 坐标和尺寸互为约束：改了 x 就不能超出右边界。两个一起夹，不去记是哪一项被改的。 */
function commitGeom() {
  const el = cur.value
  if (!el) return
  el.x = clamp(el.x, 0, 100 - (Number(el.w) || 0))
  el.y = clamp(el.y, 0, 100 - (Number(el.h) || 0))
  el.w = clamp(el.w, 1, 100 - el.x)
  el.h = clamp(el.h, 1, 100 - el.y)
  commit()
}
function alignH(pos) {
  if (!cur.value) return
  cur.value.x = pos === 'left' ? 0 : pos === 'right' ? 100 - cur.value.w : (100 - cur.value.w) / 2
  commit()
}
function alignV(pos) {
  if (!cur.value) return
  cur.value.y = pos === 'top' ? 0 : pos === 'bottom' ? 100 - cur.value.h : (100 - cur.value.h) / 2
  commit()
}
function setBorder(v) { if (cur.value) { cur.value.border = !!v; commit() } }
/** 线条/矩形的线宽线型。老模板没这两个字段，按 0.3mm 实线处理。 */
function setLineWidth(v) {
  const n = Number(v)
  if (cur.value) { cur.value.line_width = Math.min(3, Math.max(0.1, n || 0.3)); commit() }
}
function setLineStyle(v) { if (cur.value) { cur.value.line_style = v || 'solid'; commit() } }

// ------------------------------------------------------------------ 表格毫米换算
/**
 * 一页几张凭证由模板自己说（``voucher.slots_per_page``），缺省 2 ——
 * 与后端 ``validate_template`` 取同一个缺省，见 utils/paper.js::slotsPerPage。
 */
const slots = computed(() => slotsPerPage(props.tpl))
const slotMm = computed(() => {
  // 纸型恒为 A4；paperOptions 为空时（比如 meta 还没回来）退回 A4 尺寸，
  // 不能让 slotMm 变成 NaN —— 毫米换算一旦出 NaN，整张凭证的元素高度全废。
  const spec = props.paperOptions.find((p) => p.key === props.tpl.paper) || PAPER_SPEC.A4
  const pw = spec?.w || 210
  const ph = spec?.h || 297
  const land = props.tpl.orientation === 'landscape'
  const w = land ? ph : pw
  const h = land ? pw : ph
  const m = props.tpl.margins || {}
  const cw = w - (Number(m.left) || 0) - (Number(m.right) || 0)
  const ch = h - (Number(m.top) || 0) - (Number(m.bottom) || 0)
  // **槽位尺寸与「放几张」无关。** 「每页一张」= 两张里去掉一张，不是把剩下
  // 那张拉到整页高：行高是从槽位高度反推的（rowMm），槽位一变高行高跟着变，
  // 一张凭证就被纵向抻开，而模板里一个坐标都没改过。这里恒按两张算。
  const gap = SLOT_GAP
  return land ? { w: (cw - gap) / 2, h: ch, gap } : { w: cw, h: (ch - gap) / 2, gap }
})
/**
 * 分录行高（mm）不是独立配置项，而是由表格元素的高度反推出来的 ——
 * 两者必须是同一个数，存两份必然会打架。
 * 表头有没有、占多高也是模板的一部分，所以按**元素**算而不是按全局算：
 * 一张凭证上摆两个表格，各自定义表头高度时才对得上。
 */
function headPctOf(el) {
  if (!el || el.show_header === false) return 0
  const v = Number(el.header_height)
  return Number.isFinite(v) && v >= 0 ? clamp(v, 0, 100) : HEAD_PCT
}
function rowPctOf(el) { return (100 - headPctOf(el)) / (rowCount.value + 1) }
/** 表格元素占槽位多高（mm）—— 毫米和百分比之间就靠它换算。 */
function tableMm(el) { return slotMm.value.h * (Number(el?.h) || 0) / 100 }
const rowMm = computed(() => (cur.value ? tableMm(cur.value) * rowPctOf(cur.value) / 100 : 0))
const headerMm = computed(() => (cur.value ? tableMm(cur.value) * headPctOf(cur.value) / 100 : 0))

function setRowMm(mm) {
  const c = cur.value
  const v = Number(mm)
  if (!c || c.type !== 'table' || !(v > 0)) return
  // 行 mm = 槽位高 × 元素高% × 行占比%，反解出元素高度百分比
  c.h = clamp((v * 10000) / (slotMm.value.h * rowPctOf(c)), 5, 100 - c.y)
  commit()
}
/**
 * 列头高度是「占表格高度的比例」，改它只在这张表格内部重新分配 ——
 * 表格本身不缩放，所以不会把别的元素挤出槽位，分录行会自动补上差的那截。
 */
function setHeaderMm(mm) {
  const c = cur.value
  const v = Number(mm)
  const tableH = tableMm(c)
  if (!c || c.type !== 'table' || !(v >= 0) || !(tableH > 0)) return
  c.header_height = clamp((v * 100) / tableH, 0, 100)
  commit()
}
function setShowHeader(v) { if (cur.value) { cur.value.show_header = !!v; commit() } }
function setRows(v) {
  const n = Math.round(Number(v))
  if (!n || n < 1) return
  ensure()
  props.tpl.voucher.rows_per_page = clamp(n, 1, 5)
  commit()
}
function setColWidth(i, v) {
  const cols = props.tpl.columns
  const n = Number(v)
  if (!cols[i] || !(n > 0)) return
  cols[i].width = clamp(n, MIN_COL_PCT, 80)
  commit()
}
</script>

<style scoped>
.ti {
  height: 100%;
  min-width: 0;          /* 300px 的左栏里，长文本必须能被压断而不是把整栏撑开 */
  overflow: auto;
  padding: 4px 12px 20px;
  font-size: 13px;
  color: var(--cwq-text);
}
.ti :deep(.el-tabs__header) { margin-bottom: 12px; }
.ti :deep(.el-tabs__item) { font-size: 13px; }
.ti :deep(.el-input-number),
.ti :deep(.el-select) { width: 100%; }
.ti :deep(.el-input-number .el-input__inner) { text-align: left; }
/* 左栏窄，Element 默认的 25px 内容下边距会把折叠项撑得很散 */
.ti :deep(.el-collapse-item__header) { height: 34px; line-height: 34px; font-size: 13px; }
.ti :deep(.el-collapse-item__content) { padding-bottom: 10px; }
.ti :deep(.el-table .cell) { padding: 0 4px; }
.ti :deep(.el-table th.el-table__cell > .cell) { padding: 3px 4px; }

.sec { margin-bottom: 18px; }
/* 分区用小标题，不用一堆分割线 —— 300px 宽的栏里分割线太多就成了筛子 */
.sec-t {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 0 9px;
  font-size: 12px;
  font-weight: 600;
  color: var(--cwq-muted);
  letter-spacing: .5px;
}
.sec-t::before {
  content: '';
  width: 3px;
  height: 11px;
  border-radius: 2px;
  background: var(--cwq-primary);
  opacity: .5;
}
/* 元素页的「属性」小标题嵌在表单中间，给它留出上边距 */
.ti-elements .sec > .sec-t:not(:first-child) { margin-top: 16px; }
.cnt {
  min-width: 16px;
  padding: 0 5px;
  border-radius: 8px;
  background: var(--cwq-chrome-2, #f7f8fa);
  font-size: 11px;
  font-weight: 400;
  text-align: center;
}

.f { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 10px; }
/* ── 方向：整行分段控件 ──────────────────────────────────────
   两个 46px 的小块缩在左边缘、右边空一大片，本身就像没画完。
   更要紧的是 Element 默认边框 #dcdfe6 太浅，紧贴白底时左边那截线几乎看不见，
   读起来就是「框线显示不全」。这里换成一个看得见外框的轨道：两格等分填满整行，
   左边那道线由轨道自身的边框画出来，不再依赖那一格自己的 1px 边框。 */
.orient {
  display: flex !important;
  width: 100%;
  gap: 2px;
  padding: 2px;
  background: #f2f4f7;
  border: 1px solid #d3d9e2;
  border-radius: 7px;
}
.orient :deep(.el-radio-button) { flex: 1 1 0; min-width: 0; }
.orient :deep(.el-radio-button__inner) {
  width: 100%;
  border: 0 !important;
  border-radius: 5px !important;
  box-shadow: none !important;
  background: transparent;
  transition: background .15s, color .15s;
}
.orient :deep(.el-radio-button.is-active .el-radio-button__inner) {
  background: #fff;
  color: var(--cwq-chrome-active, #2563eb);
  box-shadow: 0 1px 2px rgba(15, 23, 42, .10);
}
.orient :deep(.el-radio-button.is-disabled .el-radio-button__inner) { color: var(--cwq-muted); }

.f1 { display: grid; gap: 8px; }
.fl { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.lb { font-size: 12px; color: var(--cwq-muted); }
/* 纸型只有一个值，按可编辑控件的尺寸摆出来，标签和控件左边缘才对得齐。 */
.lb-v { font-size: 13px; line-height: 24px; color: var(--cwq-text); }
.w-full { width: 100%; }
.ops { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; }
.tip {
  margin: 6px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--cwq-muted);
  overflow-wrap: anywhere;     /* {page} 这种长占位符不换行会横向撑破左栏 */
}
.tip b.ex { color: var(--cwq-primary); }
.tip code {
  padding: 0 3px;
  border-radius: 3px;
  background: var(--cwq-chrome-2, #f7f8fa);
  font-size: 11px;
}
.ro-type { font-size: 13px; color: var(--cwq-muted); }

/* 控件调色板：四个一排，图标 + 文字 */
.pal { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 6px; }
.pal-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 3px;
  height: 30px;
  padding: 0 4px;
  border: 1px solid var(--cwq-border);
  border-radius: 6px;
  background: #fff;
  font-size: 12px;
  color: var(--cwq-text);
  cursor: pointer;
  transition: border-color .15s, color .15s, background .15s;
}
.pal-btn:hover:not(:disabled) { border-color: var(--cwq-primary); color: var(--cwq-primary); background: #f5f8ff; }
.pal-btn:disabled { opacity: .5; cursor: not-allowed; }

/* 元素列表 */
.elist { margin: 0; padding: 0; list-style: none; border: 1px solid var(--cwq-border); border-radius: 6px; overflow: hidden; }
.elist li {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 3px 6px 3px 8px;
  border-bottom: 1px solid var(--cwq-border);
  cursor: pointer;
  transition: background .15s;
}
.elist li:last-child { border-bottom: 0; }
.elist li:hover { background: var(--cwq-chrome-2, #f7f8fa); }
.elist li.on { background: #e7efff; }
.elist li.on:hover { background: #e7efff; }
/* 隐藏掉的元素：名字划掉，一眼看出「这块版式上有，但它不印」 */
.elist li.off .ename { color: var(--cwq-muted); text-decoration: line-through; }
.ename { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.eops { display: flex; flex: 0 0 auto; align-items: center; }
.eops :deep(.el-button) { margin-left: 0; padding: 2px 4px; }
.colw { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 10px; margin-top: 8px; }
.del-btn { width: 100%; margin-top: 12px; }

/* 列页：表格自带横向滚动，这里只把它压紧一点 */
.cols { width: 100%; }
.cols :deep(.el-button) { margin-left: 0; }
.add-col { width: 100%; margin: 8px 0 16px; }
.gt { flex: 1; font-weight: 500; }
.fdoc {
  display: grid;
  grid-template-columns: minmax(0, auto) minmax(0, 1fr);
  gap: 4px 8px;
  margin: 4px 0 8px;
}
.fdoc dt, .fdoc dd { margin: 0; min-width: 0; }
.fdoc code {
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--cwq-chrome-2, #f7f8fa);
  font-size: 11px;
  overflow-wrap: anywhere;
}
.fdoc dd { font-size: 12px; color: var(--cwq-muted); }
</style>
