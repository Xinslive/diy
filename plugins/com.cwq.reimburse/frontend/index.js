const CONFIG_TABLE = 'plugin_f869d8c6ad46_sheetmap'
const MARK = 'cwq_reimburse'
const TABS = [
  { key: 'dispatch', label: '识别与分发' },
  { key: 'preview', label: '预览与记账' },
  { key: 'rules', label: '科目规则' },
  { key: 'sheetmap', label: '表页与账套' },
]

export default function createReimburse(host) {
  const h = host.h
  const text = value => (value === null || value === undefined ? '' : String(value))

  return {
    name: 'ReimburseVoucher',
    setup() {
      const tab = host.ref('dispatch')
      const books = host.ref([])
      // 各账套的科目编码一致，所以科目规则全插件一份，不再按账套拆分，
      // 页面上也不需要选账套；只有「表页与账套」要指明表页归哪本账。
      const config = host.ref(null)
      const configVersion = host.ref(0)
      // 科目表按账套缓存：一次分发会同时命中多本账套，预览时每本账套的 subject_id
      // 都要用自己那张表来换算编码，不能共用当前打开的那一本。
      const subjectsByBook = host.ref({})
      const suggests = host.ref([])
      const keywordFilter = host.ref('')
      const sheetRules = host.ref([])
      const newKeyword = host.ref('')
      const newBook = host.ref('')
      const file = host.ref(null)
      const analysis = host.ref(null)
      const picked = host.ref({})
      const voucherDate = host.ref('')
      const batches = host.ref({})
      const openBook = host.ref(null)
      const drafts = host.ref([])
      const busy = host.ref(false)
      const message = host.ref('')
      const dirty = host.ref(false)

      // 规则存在全局表里，宿主 SDK 不把用户身份交给插件，前端无法预判是否管理员，
      // 所以这里不预先置灰；非管理员点保存会收到后端 403「全局配置写入需要管理员」。
      const canWrite = () => true
      const subjectsOf = bookId => subjectsByBook.value[bookId] || []



      const errorText = error => {
        const detail = error?.response?.data?.detail
        if (typeof detail === 'string') return detail
        if (detail?.problems?.length) return detail.message + '：' + detail.problems.slice(0, 3).join('；')
        return detail?.message || '操作失败，请检查插件授权与账套权限'
      }
      async function act(work) {
        if (busy.value) return
        busy.value = true
        message.value = ''
        try {
          await work()
        } catch (error) {
          message.value = errorText(error)
        } finally {
          busy.value = false
        }
      }
      const editing = () => {
        const batch = batches.value[openBook.value]
        return batch && !['SUBMITTED', 'CANCELLED'].includes(batch.status)
      }
      const pendingOf = entry => Boolean(entry.aux_ref && entry.aux_ref[MARK] && entry.aux_ref[MARK].pending)
      const clearPending = entry => {
        if (entry.aux_ref && entry.aux_ref[MARK]) entry.aux_ref[MARK].pending = false
      }
      const reasonOf = entry => (entry.aux_ref?.[MARK]?.reasons || []).join('；')
      const entryTotal = entries => entries.filter(e => e.direction === 'DEBIT')
        .reduce((sum, e) => sum + Number(e.amount || 0), 0)
      const creditTotal = entries => entries.filter(e => e.direction === 'CREDIT')
        .reduce((sum, e) => sum + Number(e.amount || 0), 0)

      async function loadChart(bookId) {
        if (subjectsByBook.value[bookId]) return subjectsByBook.value[bookId]
        const list = (await host.api.get('/chart', { book_id: bookId }))
          .filter(subject => subject.active && subject.is_leaf)
        subjectsByBook.value = { ...subjectsByBook.value, [bookId]: list }
        return list
      }
      // 规则页和科目建议借当前浏览的账套取科目表；各账套科目一致，借哪本结果都一样。
      const chartBook = () => books.value.find(book => book.id === host.bookId())
        || books.value.find(book => book.can_write) || books.value[0]
      async function loadBooks() {
        books.value = await host.services.books()
        const book = chartBook()
        if (!book) return
        const data = await host.api.get('/rules')
        config.value = data.config
        configVersion.value = data.config_version
        sheetRules.value = JSON.parse(JSON.stringify(data.sheetmap.rules || []))
        voucherDate.value = voucherDate.value || text(data.config.default_date)
        await loadChart(book.id)
        suggests.value = await host.api.get('/suggest', { book_id: book.id })
      }
      async function saveRules() {
        const saved = await host.api.post('/rules', { config: config.value })
        configVersion.value = saved.version
        config.value = saved.value
        message.value = '科目规则已保存（全部账套共用）；已有预览需要重新解析并确认'
      }

      async function saveSheetmap() {
        const saved = await host.api.post('/sheetmap', { rules: sheetRules.value })
        sheetRules.value = saved.sheetmap.rules
        message.value = '表页映射已保存（' + saved.count + ' 条），重新识别后生效'
      }
      function addSheetRule() {
        const keyword = newKeyword.value.trim()
        const book = newBook.value
        if (!keyword || !book) {
          message.value = '请填写表页关键词并选择账套'
          return
        }
        if (sheetRules.value.some(rule => rule.keyword === keyword)) {
          message.value = '这个关键词已经配过了'
          return
        }
        sheetRules.value = [...sheetRules.value, { keyword, book, enabled: true }]
        newKeyword.value = ''
      }
      function dropSheetRule(index) {
        sheetRules.value = sheetRules.value.filter((_rule, i) => i !== index)
      }

      async function analyze() {
        if (!file.value) {
          message.value = '请先选择报销明细工作簿'
          return
        }
        const result = await host.api.upload('/analyze', [file.value], {})
        analysis.value = result
        const next = {}
        result.sheets.forEach(sheet => {
          if (sheet.status === 'matched' && sheet.can_write) next[sheet.name] = true
        })
        picked.value = next
        const matched = result.sheets.filter(s => s.status === 'matched').length
        const unmatched = result.sheets.filter(s => s.status === 'unmatched')
        const readonly = result.sheets.filter(s => s.status === 'matched' && !s.can_write)
        message.value = '识别完成：' + matched + ' 个表页可生成凭证'
        if (unmatched.length) {
          message.value += '；' + unmatched.length + ' 个表页没匹配到账套，请到「表页与账套」补关键词后重新识别'
        }
        if (readonly.length) {
          message.value += '；' + readonly.length + ' 个表页对应的账套是只读，不能记账'
        }
      }
      function grouped() {
        const groups = {}
        analysis.value.sheets.forEach(sheet => {
          if (!picked.value[sheet.name] || sheet.status !== 'matched') return
          if (!groups[sheet.book_id]) groups[sheet.book_id] = []
          groups[sheet.book_id].push(sheet.name)
        })
        return groups
      }
      async function dispatch() {
        if (!voucherDate.value) {
          message.value = '请先填写本次统一的凭证日期'
          return
        }
        const groups = grouped()
        if (!Object.keys(groups).length) {
          message.value = '请至少勾选一个表页'
          return
        }
        const done = []
        for (const bookId of Object.keys(groups)) {
          const book = books.value.find(item => item.id === Number(bookId))
          const batch = await host.services.imports.upload(Number(bookId), [file.value])
          if (['SUBMITTED', 'CANCELLED'].includes(batch.status)) {
            message.value = '「' + book.name + '」已有' + batch.status + '的批次，未重复解析'
            continue
          }
          const parsed = await host.api.post('/parse/' + batch.id, {
            revision: batch.revision,
            sheets: groups[bookId],
            voucher_date: voucherDate.value,
          })
          batches.value = { ...batches.value, [bookId]: parsed }
          done.push(book.name)
        }
        if (done.length) {
          tab.value = 'preview'
          openBook.value = Number(Object.keys(groups)[0])
          await loadChart(openBook.value)
          showDraft()
          message.value = '已为 ' + done.join('、') + ' 生成预览'
        }
      }
      async function refresh(key) {
        const current = batches.value[key]
        const batch = await host.services.imports.read(current.id)
        batches.value = { ...batches.value, [key]: batch }
        showDraft()
        return batch
      }
      function showDraft() {
        const batch = batches.value[openBook.value]
        drafts.value = batch ? JSON.parse(JSON.stringify(batch.preview || [])) : []
        dirty.value = false
      }
      const bookNameOf = bookId => books.value.find(book => book.id === bookId)?.name || ('账套 ' + bookId)
      async function open(bookId) {
        openBook.value = bookId
        await loadChart(bookId)
        return refresh(bookId)
      }
      async function saveDraft() {
        const batch = await host.services.imports.edit(batches.value[openBook.value].id,
          batches.value[openBook.value].revision, drafts.value)
        batches.value = { ...batches.value, [openBook.value]: batch }
        showDraft()
        message.value = '预览已保存'
      }
      async function confirmBatch() {
        const current = batches.value[openBook.value]
        const batch = await host.services.imports.confirm(current.id, current.revision)
        batches.value = { ...batches.value, [openBook.value]: batch }
        showDraft()
        message.value = '已确认，可以记账'
      }
      async function submitBatch() {
        const current = batches.value[openBook.value]
        const batch = await host.services.imports.submit(current.id, current.revision)
        batches.value = { ...batches.value, [openBook.value]: batch }
        showDraft()
        message.value = '已生成凭证：' + (batch.result || []).map(item => item.voucher_no).join('、')
      }
      async function cancelBatch() {
        const current = batches.value[openBook.value]
        const batch = await host.services.imports.cancel(current.id, current.revision)
        batches.value = { ...batches.value, [openBook.value]: batch }
        showDraft()
      }

      host.onMounted(() => act(loadBooks))

      // ------------------------------------------------------------- 控件
      const button = (label, handler, disabled) => h('button', {
        type: 'button',
        class: 'rb-btn',
        disabled: busy.value || Boolean(disabled),
        onClick: () => act(handler),
      }, label)
      const textInput = (label, value, onChange, options = {}) => h('input', Object.assign({
        type: 'text',
        value,
        'aria-label': label,
        disabled: busy.value || Boolean(options.locked),
        onInput: event => onChange(event.target.value),
      }, options.attrs || {}))
      const subjectSelect = (label, code, onChange, locked) => h('select', {
        value: code || '',
        'aria-label': label,
        disabled: busy.value || Boolean(locked),
        onChange: event => onChange(event.target.value),
      }, [h('option', { value: '' }, '请选择科目'),
        ...subjectsOf(chartBook()?.id).map(subject => h('option', { key: subject.code, value: subject.code },
          subject.code + ' ' + subject.name))])

      const tabBar = h('nav', { class: 'rb-tabs', 'data-testid': 'rb-tabs' }, TABS.map(item =>
        h('button', {
          key: item.key,
          type: 'button',
          class: 'rb-tab' + (tab.value === item.key ? ' on' : ''),
          disabled: busy.value,
          onClick: () => { tab.value = item.key },
        }, item.label)))

      // ------------------------------------------------------- 识别与分发
      const sheetRow = sheet => {
        const labels = {
          matched: '已关联', unmatched: '未关联账套', skipped: '跳过', empty: '无金额',
        }
        const canPick = sheet.status === 'matched' && sheet.can_write
        return h('tr', { key: sheet.name, class: 'rb-' + sheet.status }, [
          h('td', {}, h('input', {
            type: 'checkbox',
            checked: Boolean(picked.value[sheet.name]),
            disabled: busy.value || !canPick,
            'aria-label': '选择 ' + sheet.name,
            onChange: event => picked.value = { ...picked.value, [sheet.name]: event.target.checked },
          })),
          h('td', {}, sheet.name),
          h('td', {}, text(sheet.title)),
          h('td', {}, sheet.layout === 'packed' ? '摘要型' : sheet.layout === 'item' ? '明细型' : '—'),
          h('td', {}, sheet.status === 'ready' || sheet.status === 'matched'
            ? sheet.count + ' 条 / ' + sheet.total : '—'),
          h('td', {}, sheet.book_name || '—'),
          h('td', {}, h('span', { class: 'rb-tag rb-' + sheet.status },
            (labels[sheet.status] || sheet.status) + (sheet.reason ? '：' + sheet.reason : ''))),
        ])
      }
      const dispatchPane = () => {
        const rows = analysis.value ? analysis.value.sheets : []
        const warnings = rows.flatMap(sheet => (sheet.warnings || []).map(w => ({
          sheet: sheet.name, line: w.line, text: w.text, message: w.message,
        })))
        return h('section', { class: 'rb-pane', 'data-testid': 'rb-dispatch' }, [
          h('fieldset', { class: 'rb-box' }, [
            h('legend', '第 1 步 选择工作簿'),
            h('p', { class: 'rb-hint' },
              '不用选目标账套：每个表页按「表页与账套」里的关键词自动归账套。支持「品名/金额/备注」和'
              + '「日期/摘要/合计」两种表头，隐藏表页、没有金额的表页和系统表页会自动跳过。'),
            h('input', {
              type: 'file',
              accept: '.xlsx',
              'aria-label': '报销明细工作簿',
              disabled: busy.value,
              onChange: event => { file.value = event.target.files[0] || null },
            }),
            button('识别工作簿', analyze, !file.value),
          ]),
          h('fieldset', { class: 'rb-box' }, [
            h('legend', '第 2 步 统一凭证日期'),
            h('p', { class: 'rb-hint' }, '本次生成的所有凭证都用这个日期，不再按各账套配置分别取值。'),
            h('label', { class: 'rb-field' }, ['凭证日期 ',
              h('input', {
                type: 'date',
                value: voucherDate.value,
                'aria-label': '凭证日期',
                disabled: busy.value,
                onInput: event => { voucherDate.value = event.target.value },
              })]),
          ]),
          rows.length ? h('fieldset', { class: 'rb-box' }, [
            h('legend', '第 3 步 确认表页归属并分发'),
            h('table', { class: 'rb-table' }, [
              h('thead', {}, h('tr', {}, ['选', '表页', '标题', '版式', '明细', '账套', '状态']
                .map(title => h('th', { scope: 'col' }, title)))),
              h('tbody', {}, rows.map(sheetRow)),
            ]),
            warnings.length ? h('div', { class: 'rb-warn' }, [
              h('p', {}, '解析提示（不影响记账，请知悉）：'),
              h('ul', {}, warnings.slice(0, 30).map((item, index) => h('li', { key: index },
                item.sheet + (item.line ? ' 第 ' + item.line + ' 行 ' : ' ') + item.message))),
            ]) : null,
            button('生成预览凭证', dispatch, !voucherDate.value || !file.value),
          ]) : null,
        ])
      }

      // --------------------------------------------------------- 预览与记账
      const entryTable = (voucher, voucherIndex) => h('div', { class: 'rb-voucher' }, [
        h('h3', {}, '凭证 ' + (voucherIndex + 1) + ' · ' + voucher.voucher_date
          + ' · ' + voucher.remark),
        h('table', { class: 'rb-table rb-entries' }, [
          h('thead', {}, h('tr', {}, ['方向', '摘要', '科目', '金额', '提示']
            .map(title => h('th', { scope: 'col' }, title)))),
          h('tbody', {}, (voucher.entries || []).map((entry, entryIndex) => {
            const locked = !editing()
            const chart = subjectsOf(openBook.value)
            const code = chart.find(subject => subject.id === entry.subject_id)
            return h('tr', {
              key: entryIndex,
              class: pendingOf(entry) ? 'rb-pending' : '',
            }, [
              h('td', {}, entry.direction === 'DEBIT' ? '借' : '贷'),
              h('td', {}, text(entry.summary)),
              h('td', {}, locked
                ? text(code ? code.code + ' ' + code.name : entry.subject_id)
                : h('select', {
                  value: code ? code.code : '',
                  'aria-label': '科目 ' + (voucherIndex + 1) + ' 行 ' + (entryIndex + 1),
                  disabled: busy.value,
                  onChange: event => {
                    const pickedSubject = chart.find(s => s.code === event.target.value)
                    entry.subject_id = pickedSubject ? pickedSubject.id : entry.subject_id
                    if (pickedSubject) clearPending(entry)
                    dirty.value = true
                  },
                }, chart.map(subject => h('option', { key: subject.code, value: subject.code },
                  subject.code + ' ' + subject.name)))),
              h('td', {}, locked ? text(entry.amount) : h('input', {
                type: 'text',
                value: entry.amount,
                'aria-label': '金额 ' + (voucherIndex + 1) + ' 行 ' + (entryIndex + 1),
                disabled: busy.value,
                onInput: event => { entry.amount = event.target.value; dirty.value = true },
              })),
              h('td', {}, pendingOf(entry) ? '待确认：' + reasonOf(entry) : ''),
            ])
          })),
        ]),
        h('p', { class: 'rb-hint' }, '借方合计 ' + entryTotal(voucher.entries || []).toFixed(2)
          + '，贷方合计 ' + creditTotal(voucher.entries || []).toFixed(2)
          + '，差 ' + (entryTotal(voucher.entries || []) - creditTotal(voucher.entries || [])).toFixed(2)),
      ])
      const previewPane = () => {
        const list = Object.keys(batches.value).map(key => batches.value[key])
        if (!list.length) return h('section', { class: 'rb-pane' }, [
          h('p', { class: 'rb-hint' }, '还没有批次。先在「识别与分发」里生成预览。')])
        const current = batches.value[openBook.value]
        const warnings = (current?.configuration?.metas || []).flatMap(meta =>
          (meta.warnings || []).map(item => ({ sheet: meta.sheet, message: item.message })))
        return h('section', { class: 'rb-pane', 'data-testid': 'rb-preview' }, [
          h('h3', {}, '本次已生成的批次'),
          h('ul', { class: 'rb-batches' }, list.map(batch => h('li', { key: batch.id }, [
            h('button', {
              type: 'button',
              class: 'rb-btn' + (batch.book_id === openBook.value ? ' on' : ''),
              disabled: busy.value,
              onClick: () => act(() => open(batch.book_id)),
            }, bookNameOf(batch.book_id) + ' · ' + batch.status
              + ' · ' + batch.revision),
            ' ',
            h('button', {
              type: 'button', class: 'rb-btn', disabled: busy.value,
              onClick: () => act(() => refresh(batch.book_id)),
            }, '重新读取'),
          ]))),
          current ? h('div', {}, [
            h('p', {}, '批次 ' + current.id + ' · 状态 ' + current.status
              + ' · 修订 ' + current.revision
              + '（共 ' + (current.preview || []).length + ' 张凭证）'),
            h('ul', { class: 'rb-hint' }, (current.configuration?.metas || []).map(meta =>
              h('li', { key: meta.sheet }, meta.sheet + '：' + meta.count + ' 条借方、'
                + meta.pending + ' 条待确认'
                + (meta.defaulted ? '（其中 ' + meta.defaulted + ' 条走了'
                  + (meta.fallback_mode === 'default' ? '默认科目' : '待确认科目') + '）' : '')))),
            warnings.length ? h('ul', { class: 'rb-warn' }, warnings.map((item, index) =>
              h('li', { key: index }, item.sheet + '：' + item.message))) : null,
            (current.validation_errors || []).length ? h('ul', { class: 'rb-warn' },
              current.validation_errors.map((item, index) => h('li', { key: index },
                '第 ' + item.voucher + ' 张：' + item.message))) : null,
            drafts.value.map((voucher, index) => entryTable(voucher, index)),
            h('div', { class: 'rb-actions' }, [
              button('保存调整后的预览', saveDraft, !dirty.value || !editing()),
              button('确认当前预览', confirmBatch, dirty.value
                || !['PREVIEW_READY', 'EDITING'].includes(current.status)
                || (current.validation_errors || []).length > 0 || !editing()),
              button('整批记账', submitBatch, dirty.value || current.status !== 'CONFIRMED' || !editing()),
              button('取消批次', cancelBatch, !editing()),
            ]),
            (current.result || []).length ? h('p', { class: 'rb-ok' },
              '已生成凭证：' + current.result.map(item => item.voucher_no).join('、')) : null,
          ]) : null,
        ])
      }

      // ------------------------------------------------------------ 科目规则
      const visibleRules = () => {
        const needle = keywordFilter.value.trim()
        const rules = config.value?.subject_rules || []
        if (!needle) return rules.map((rule, index) => ({ rule, index }))
        return rules.map((rule, index) => ({ rule, index }))
          .filter(item => item.rule.all.join('+').includes(needle) || text(item.rule.note).includes(needle))
      }
      const rulesPane = () => {
        if (!config.value) return h('section', { class: 'rb-pane' }, [h('p', {}, '正在读取科目规则')])
        const blank = visibleRules().filter(item => !item.rule.code).length
        const mode = config.value.unmatched_mode || 'pending'
        return h('section', { class: 'rb-pane', 'data-testid': 'rb-rules' }, [
          h('p', { class: 'rb-hint' },
            '这份规则全部账套共用，不需要选账套。只有管理员能保存。'),
          h('fieldset', { class: 'rb-box' }, [
            h('legend', '凭证参数'),
            h('div', { class: 'rb-grid' }, [
              h('label', { class: 'rb-field' }, ['对方科目（贷方） ',
                subjectSelect('对方科目', config.value.counterparty_code,
                  value => { config.value.counterparty_code = value }, !canWrite())]),
              h('label', { class: 'rb-field' }, ['未命中的明细 ',
                h('select', {
                  value: mode,
                  'aria-label': '未命中的明细',
                  disabled: busy.value || !canWrite(),
                  onChange: event => { config.value.unmatched_mode = event.target.value },
                }, [
                  h('option', { value: 'pending' }, '进待确认科目，人工复核'),
                  h('option', { value: 'default' }, '直接记入默认科目'),
                ])]),
              mode === 'pending'
                ? h('label', { class: 'rb-field' }, ['待确认科目 ',
                  subjectSelect('待确认科目', config.value.pending_code,
                    value => { config.value.pending_code = value }, !canWrite())])
                : h('label', { class: 'rb-field' }, ['默认科目 ',
                  subjectSelect('默认科目', config.value.default_code,
                    value => { config.value.default_code = value }, !canWrite())]),
              mode === 'default' ? h('label', { class: 'rb-field' }, [h('input', {
                type: 'checkbox',
                checked: config.value.default_mark_pending !== false,
                disabled: busy.value || !canWrite(),
                onChange: event => { config.value.default_mark_pending = event.target.checked },
              }), ' 落到默认科目后仍在预览中高亮']) : null,
              h('label', { class: 'rb-field' }, ['默认凭证日期 ',
                h('input', {
                  type: 'date', value: text(config.value.default_date), 'aria-label': '默认凭证日期',
                  disabled: busy.value || !canWrite(),
                  onInput: event => { config.value.default_date = event.target.value },
                })]),
              h('label', { class: 'rb-field' }, ['摘要模板 ',
                textInput('摘要模板', text(config.value.remark_template),
                  value => { config.value.remark_template = value }, { locked: !canWrite() })]),
              h('label', { class: 'rb-field' }, ['附单据数 ',
                textInput('附单据数', text(config.value.attachments),
                  value => { config.value.attachments = Number(value) || 0 }, { locked: !canWrite() })]),
              h('label', { class: 'rb-field' }, [h('input', {
                type: 'checkbox', checked: Boolean(config.value.merge_same_subject),
                disabled: busy.value || !canWrite(),
                onChange: event => { config.value.merge_same_subject = event.target.checked },
              }), ' 同科目合并成一条分录']),
            ]),
            h('p', { class: 'rb-hint' },
              '摘要模板可用占位符：{sheet} 表页名、{title} 标题、{month} 月份。未命中的明细指既没匹配到关键词、'
              + '或匹配到的规则还没填科目编码的行；它按上面的选择进待确认科目或默认科目。'),
          ]),
          h('fieldset', { class: 'rb-box' }, [
            h('legend', '关键词 → 科目'),
            h('p', { class: 'rb-hint' },
              '多条关键词用「+」表示需同时命中，命中的词越长越优先。' + (blank
                ? '还有 ' + blank + ' 条规则没有编码，它们会走待确认科目。'
                : '所有规则都已配置编码。')),
            h('label', { class: 'rb-field' }, ['筛选 ',
              h('input', {
                type: 'text', value: keywordFilter.value, 'aria-label': '筛选关键词',
                onInput: event => { keywordFilter.value = event.target.value },
              })]),
            h('table', { class: 'rb-table' }, [
              h('thead', {}, h('tr', {}, ['关键词', '科目', '建议', '操作']
                .map(title => h('th', { scope: 'col' }, title)))),
              h('tbody', {}, visibleRules().map(({ rule, index }) => {
                const suggestion = suggests.value.find(item => item.index === index)
                const candidates = suggestion?.candidates || []
                return h('tr', { key: index, class: rule.code ? '' : 'rb-pending' }, [
                  h('td', {}, rule.all.join('+') + (rule.not.length ? '（排除 ' + rule.not.join('、') + '）' : '')),
                  h('td', {}, subjectSelect('科目 ' + rule.all.join('+'), rule.code,
                    value => { config.value.subject_rules[index].code = value }, !canWrite())),
                  h('td', {}, candidates.length ? candidates.map(candidate =>
                    h('button', {
                      key: candidate.code, type: 'button', class: 'rb-btn rb-mini',
                      disabled: busy.value || !canWrite(),
                      onClick: () => { config.value.subject_rules[index].code = candidate.code },
                    }, candidate.code + ' ' + candidate.name)) : '—'),
                  h('td', {}, h('button', {
                    type: 'button', class: 'rb-btn rb-mini',
                    disabled: busy.value || !canWrite(),
                    onClick: () => { config.value.subject_rules.splice(index, 1) },
                  }, '删除')),
                ])
              })),
            ]),
            h('div', { class: 'rb-field' }, [
              h('input', {
                type: 'text', value: newKeyword.value, placeholder: '新关键词，如 布草',
                'aria-label': '新关键词',
                onInput: event => { newKeyword.value = event.target.value },
              }),
              ' ',
              button('新增规则', () => {
                const keyword = newKeyword.value.trim()
                if (!keyword) return
                config.value.subject_rules.unshift({ all: [keyword], any: [], not: [], code: '', counterparty_code: '', note: '' })
                newKeyword.value = ''
              }),
            ]),
          ]),
          button('保存科目规则', saveRules, !canWrite()),
        ])
      }

      // -------------------------------------------------------- 表页与账套
      const sheetmapPane = () => {
        const known = analysis.value?.books || books.value
        return h('section', { class: 'rb-pane', 'data-testid': 'rb-sheetmap' }, [
          h('p', { class: 'rb-hint' },
            '表页名里包含这里的关键词，就归到对应账套。留空或停用的规则不生效。这份配置是全账套共用的，只有管理员能保存。'),
          h('table', { class: 'rb-table' }, [
            h('thead', {}, h('tr', {}, ['表页关键词', '账套', '启用', '操作']
              .map(title => h('th', { scope: 'col' }, title)))),
            h('tbody', {}, sheetRules.value.map((rule, index) => h('tr', { key: index }, [
              h('td', {}, textInput('关键词 ' + index, rule.keyword,
                value => { sheetRules.value[index].keyword = value })),
              h('td', {}, h('select', {
                value: rule.book,
                'aria-label': '账套 ' + index,
                onChange: event => { sheetRules.value[index].book = event.target.value },
              }, [h('option', { value: '' }, '请选择账套'),
                ...known.map(book => h('option', { key: book.id, value: book.name },
                  book.name + (book.company_name ? '（' + book.company_name + '）' : '')))])),
              h('td', {}, h('input', {
                type: 'checkbox', checked: rule.enabled !== false, 'aria-label': '启用 ' + index,
                onChange: event => { sheetRules.value[index].enabled = event.target.checked },
              })),
              h('td', {}, h('button', {
                type: 'button', class: 'rb-btn rb-mini', disabled: busy.value,
                onClick: () => dropSheetRule(index),
              }, '删除')),
            ]))),
          ]),
          h('div', { class: 'rb-field' }, [
            h('input', {
              type: 'text', value: newKeyword.value, placeholder: '新表页关键词，如 优舍',
              'aria-label': '新表页关键词',
              onInput: event => { newKeyword.value = event.target.value },
            }),
            ' ',
            h('select', {
              value: newBook.value, 'aria-label': '新表页对应账套',
              onChange: event => { newBook.value = event.target.value },
            }, [h('option', { value: '' }, '请选择账套'),
              ...known.map(book => h('option', { key: book.id, value: book.name },
                book.name + (book.company_name ? '（' + book.company_name + '）' : '')))]),
            ' ',
            button('新增映射', addSheetRule),
          ]),
          button('保存表页映射', saveSheetmap),
        ])
      }

      return () => h('section', { class: 'rb-root', 'data-testid': 'reimburse' }, [
        h('h1', {}, '民宿报销'),
        h('p', { class: 'rb-hint' },
          '上传一个门店报销工作簿，按表页名自动关联账套，按关键词映射科目，统一日期后逐个账套生成并记账。'),
        tabBar,
        message.value ? h('p', { role: 'alert', class: 'rb-msg' }, message.value) : null,
        tab.value === 'dispatch' ? dispatchPane()
          : tab.value === 'preview' ? previewPane()
            : tab.value === 'rules' ? rulesPane() : sheetmapPane(),
      ])
    },
  }
}
