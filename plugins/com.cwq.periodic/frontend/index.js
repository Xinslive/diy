const TABS = [
  { key: 'rules', label: '周期规则' },
  { key: 'execute', label: '试算与记账' },
  { key: 'runs', label: '执行记录' },
]

const blankRule = () => ({
  id: null,
  name: '',
  day_of_month: 1,
  amount: '',
  entries: [
    { code: '', direction: 'DEBIT', summary: '' },
    { code: '', direction: 'CREDIT', summary: '' },
  ],
  remark: '',
  attachments: 1,
  enabled: true,
  start_label: '',
  end_label: '',
  note: '',
})

const blankEntry = direction => ({ code: '', direction, summary: '' })

export default function createPeriodic(host) {
  const h = host.h
  const text = value => (value === null || value === undefined ? '' : String(value))

  return {
    name: 'PeriodicVoucher',
    setup() {
      const tab = host.ref('rules')
      const books = host.ref([])
      // 账套由用户自己选：插件是用户级授权而不是绑定某本账，顶部切换不重挂载
      // 插件页面，所以这里保存自己的选择。
      const bookId = host.ref(null)
      const rules = host.ref([])
      const runs = host.ref([])
      const subjects = host.ref([])
      const draft = host.ref(blankRule())
      const editingId = host.ref(null)
      const year = host.ref(new Date().getFullYear())
      const month = host.ref(new Date().getMonth() + 1)
      const preview = host.ref(null)
      const result = host.ref(null)
      const picked = host.ref({})
      // 删除和撤销都用页面内二次确认，不用全局 confirm：插件运行在宿主页面里，
      // 全局 confirm 不保证存在，且会被宿主的安全策略拦下。宁可让用户多点一次。
      const confirmText = host.ref('')
      const askConfirm = (key, text) => { confirmText.value = { key, text } }
      const busy = host.ref(false)
      const message = host.ref('')

      const current = () => books.value.find(book => book.id === bookId.value) || null
      const writable = () => Boolean(current()?.canWrite)
      const ruleName = id => rules.value.find(rule => rule.id === id)?.name || ('规则 ' + id)

      const errorText = error => {
        const detail = error?.response?.data?.detail
        if (typeof detail === 'string') return detail
        if (Array.isArray(detail) && detail[0]?.msg) return detail.map(item => item.msg).join('；')
        if (detail?.message) return detail.message
        if (detail?.problems?.length) return detail.message + '：' + detail.problems.slice(0, 3).join('；')
        return detail?.message || '操作失败，请检查插件授权与账套写权限'
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

      async function loadSubjects() {
        if (!bookId.value || subjects.value.length) return
        const list = (await host.api.get('/chart', { book_id: bookId.value }))
          .filter(subject => subject.active && subject.is_leaf)
        subjects.value = list
      }
      async function loadBook() {
        if (!bookId.value) return
        const data = await host.api.get('/rules', { book_id: bookId.value })
        rules.value = data.rules || []
        runs.value = data.runs || []
        preview.value = null
        result.value = null
        await loadSubjects()
      }
      async function loadBooks() {
        books.value = await host.services.books()
        // 默认落在顶部正在看的那本账；没有就选第一本可写的。
        const preferred = books.value.find(book => book.id === host.bookId())
          || books.value.find(book => book.canWrite)
          || books.value[0]
        if (preferred && !bookId.value) {
          bookId.value = preferred.id
          await loadBook()
        }
      }
      async function switchBook(next) {
        bookId.value = next
        subjects.value = []
        draft.value = blankRule()
        editingId.value = null
        await loadBook()
      }

      function editRule(rule) {
        editingId.value = rule.id
        draft.value = JSON.parse(JSON.stringify({
          id: rule.id,
          name: rule.name,
          day_of_month: rule.day_of_month,
          amount: rule.amount,
          entries: rule.entries?.length ? rule.entries : blankRule().entries,
          remark: rule.remark,
          attachments: rule.attachments,
          enabled: rule.enabled,
          start_label: rule.start_label,
          end_label: rule.end_label,
          note: rule.note,
        }))
        tab.value = 'rules'
      }
      function resetDraft() {
        editingId.value = null
        draft.value = blankRule()
      }
      function addEntry(direction) {
        draft.value = { ...draft.value, entries: [...draft.value.entries, blankEntry(direction)] }
      }
      function patchEntry(index, patch) {
        const entries = draft.value.entries.map((entry, i) => (i === index ? { ...entry, ...patch } : entry))
        draft.value = { ...draft.value, entries }
      }
      function dropEntry(index) {
        const entries = draft.value.entries.filter((_entry, i) => i !== index)
        draft.value = { ...draft.value, entries: entries.length ? entries : blankRule().entries }
      }
      async function saveRule() {
        const saved = await host.api.post('/rules', { ...draft.value, book_id: bookId.value })
        message.value = saved.id ? '规则「' + saved.name + '」已保存' : '规则已保存'
        resetDraft()
        await loadBook()
      }
      async function removeRule(rule) {
        await host.api.post('/rules/' + rule.id + '/delete', { book_id: bookId.value })
        message.value = '规则「' + rule.name + '」已删除'
        if (editingId.value === rule.id) resetDraft()
        await loadBook()
      }

      async function doPreview() {
        const result0 = await host.api.post('/preview', {
          book_id: bookId.value,
          year: Number(year.value) || undefined,
          month: Number(month.value) || undefined,
          rule_ids: Object.keys(picked.value).filter(key => picked.value[key]).map(Number),
        })
        preview.value = result0
        result.value = null
      }
      async function doRun() {
        const runIds = Object.keys(picked.value).filter(key => picked.value[key]).map(Number)
        const done = await host.api.post('/run', {
          book_id: bookId.value,
          year: Number(year.value) || undefined,
          month: Number(month.value) || undefined,
          rule_ids: runIds.length ? runIds : undefined,
        })
        result.value = done
        message.value = done.created.length
          ? '已生成 ' + done.created.length + ' 张凭证：' + done.created.map(item => item.voucher_no).join('、')
          : '没有生成新凭证'
        if (done.failed?.length) {
          message.value += '；' + done.failed.length + ' 条失败，请看下面的原因'
        }
        await loadBook()
      }
      async function undoRun(row) {
        await host.api.post('/runs/' + row.id + '/undo', { book_id: bookId.value })
        message.value = row.voucher_no
          ? '已撤销「' + row.period_label + ' ' + ruleName(row.rule_id) + '」的记账记录（凭证 ' + row.voucher_no + ' 仍在，请到凭证页自行处理）'
          : '已撤销记账记录'
        await loadBook()
      }

      host.onMounted(() => act(loadBooks))

      // ------------------------------------------------------------- 控件
      const button = (label, handler, disabled) => h('button', {
        type: 'button',
        class: 'pd-btn',
        disabled: busy.value || Boolean(disabled),
        onClick: () => act(handler),
      }, label)
      const field = (label, control) => h('label', { class: 'pd-field' }, [label, ' ', control])
      const input = (label, value, onChange, options = {}) => h('input', Object.assign({
        type: 'text',
        value,
        'aria-label': label,
        disabled: busy.value || Boolean(options.locked),
        onInput: event => onChange(event.target.value),
      }, options.attrs || {}))
      const select = (label, value, onChange, options = {}) => h('select', {
        value,
        'aria-label': label,
        disabled: busy.value || Boolean(options.locked),
        onChange: event => onChange(event.target.value),
      }, options.options || [])
      const subjectOptions = () => [h('option', { value: '' }, '请选择科目'),
        ...subjects.value.map(subject => h('option', { key: subject.code, value: subject.code },
          subject.code + ' ' + subject.name))]

      const confirmBar = () => {
        const state = confirmText.value
        if (!state) return null
        const finish = () => { confirmText.value = '' }
        const yes = () => {
          const key = state.key
          finish()
          if (key.startsWith('rule:')) {
            const rule = rules.value.find(item => item.id === Number(key.slice(5)))
            if (rule) act(() => removeRule(rule))
          } else {
            const row = runs.value.find(item => item.id === Number(key.slice(4)))
            if (row) act(() => undoRun(row))
          }
        }
        return h('div', { class: 'pd-confirm', role: 'alert', 'data-testid': 'pd-confirm' }, [
          h('span', {}, state.text),
          ' ',
          h('button', {
            type: 'button', class: 'pd-btn pd-mini pd-danger', disabled: busy.value, onClick: yes,
          }, '确定'),
          ' ',
          h('button', {
            type: 'button', class: 'pd-btn pd-mini', disabled: busy.value, onClick: finish,
          }, '取消'),
        ])
      }

      const bookBar = () => h('div', { class: 'pd-books' }, [
        h('span', { class: 'pd-books-label' }, '账套'),
        select('选择账套', bookId.value || '', value => switchBook(Number(value)), {
          options: [h('option', { value: '' }, '请选择账套'),
            ...books.value.map(book => h('option', {
              key: book.id,
              value: book.id,
              disabled: !book.canWrite,
            }, book.name + (book.company_name ? '（' + book.company_name + '）' : '')
              + (book.canWrite ? '' : ' — 只读')))],
        }),
        current() && !writable()
          ? h('span', { class: 'pd-warn-inline' }, '这个账套是只读的，只能查看规则和记录，不能记账')
          : null,
      ])

      const tabBar = h('nav', { class: 'pd-tabs', 'data-testid': 'pd-tabs' }, TABS.map(item =>
        h('button', {
          key: item.key,
          type: 'button',
          class: 'pd-tab' + (tab.value === item.key ? ' on' : ''),
          disabled: busy.value,
          onClick: () => { tab.value = item.key },
        }, item.label)))

      // ------------------------------------------------------------- 规则页
      const ruleTable = () => {
        if (!rules.value.length) {
          return h('p', { class: 'pd-hint' }, '这个账套还没有周期规则，用下面的表单加一条。')
        }
        return h('table', { class: 'pd-table' }, [
          h('thead', {}, h('tr', {}, ['规则', '每月', '金额', '分录', '起止年月', '启用', '操作']
            .map(title => h('th', { scope: 'col' }, title)))),
          h('tbody', {}, rules.value.map(rule => {
            const entries = rule.entries || []
            return h('tr', { key: rule.id, class: rule.enabled ? '' : 'pd-off' }, [
              h('td', {}, rule.name),
              h('td', {}, '第 ' + rule.day_of_month + ' 天'),
              h('td', {}, Number(rule.amount || 0).toFixed(2)),
              h('td', {}, h('div', {}, entries.map(entry => h('div', { key: entry.code + entry.direction },
                (entry.direction === 'DEBIT' ? '借 ' : '贷 ') + entry.code))),
                h('span', { class: 'pd-sub' }, '共 ' + entries.length + ' 条')),
              h('td', {}, [rule.start_label, rule.end_label].filter(Boolean).join(' ~ ') || '不限'),
              h('td', {}, rule.enabled ? '是' : '否'),
              h('td', {}, [
                h('button', {
                  type: 'button', class: 'pd-btn pd-mini', disabled: busy.value,
                  onClick: () => editRule(rule),
                }, '编辑'),
                ' ',
                h('button', {
                  type: 'button', class: 'pd-btn pd-mini', disabled: busy.value || !writable(),
                  onClick: () => askConfirm('rule:' + rule.id, '删除规则「' + rule.name + '」？已记过账的规则要先撤销执行记录。'),
                }, '删除'),
              ]),
            ])
          })),
        ])
      }

      const entryRows = () => draft.value.entries.map((entry, index) => h('div', {
        key: index, class: 'pd-entry',
      }, [
        select('分录 ' + (index + 1) + ' 方向', entry.direction,
          value => patchEntry(index, { direction: value }), {
            options: [h('option', { value: 'DEBIT' }, '借方'), h('option', { value: 'CREDIT' }, '贷方')],
          }),
        ' ',
        select('分录 ' + (index + 1) + ' 科目', entry.code,
          value => patchEntry(index, { code: value }), { options: subjectOptions() }),
        ' ',
        input('分录 ' + (index + 1) + ' 摘要', entry.summary,
          value => patchEntry(index, { summary: value }), { attrs: { placeholder: '留空用规则名' } }),
        ' ',
        h('button', {
          type: 'button', class: 'pd-btn pd-mini', disabled: busy.value,
          onClick: () => dropEntry(index),
        }, '删除'),
      ]))

      const ruleForm = () => h('fieldset', { class: 'pd-box' }, [
        h('legend', {}, editingId.value ? '编辑规则' : '新增规则'),
        h('div', { class: 'pd-grid' }, [
          field('规则名称', input('规则名称', draft.value.name,
            value => { draft.value = { ...draft.value, name: value } },
            { attrs: { placeholder: '如 办公室租金' } })),
          field('每月第几天', h('input', {
            type: 'number', min: 1, max: 31, value: draft.value.day_of_month,
            'aria-label': '每月第几天', disabled: busy.value,
            onInput: event => { draft.value = { ...draft.value, day_of_month: Number(event.target.value) || 1 } },
          })),
          field('每月金额', h('input', {
            type: 'text', value: draft.value.amount,
            'aria-label': '每月金额', disabled: busy.value,
            onInput: event => { draft.value = { ...draft.value, amount: event.target.value } },
          })),
        ]),
        h('p', { class: 'pd-hint' }, '金额按上面这个数记账，每条分录都用同一个金额；借贷方向由下面每条分录自己选。'),
        h('div', {}, entryRows()),
        h('p', {}, [
          button('加一条借方', () => addEntry('DEBIT')),
          button('加一条贷方', () => addEntry('CREDIT')),
        ]),
        h('div', { class: 'pd-grid' }, [
          field('凭证摘要', input('凭证摘要', draft.value.remark,
            value => { draft.value = { ...draft.value, remark: value } },
            { attrs: { placeholder: '留空自动写「月份 + 规则名」' } })),
          field('起始年月', h('input', {
            type: 'month', value: draft.value.start_label,
            'aria-label': '起始年月', disabled: busy.value,
            onInput: event => { draft.value = { ...draft.value, start_label: event.target.value } },
          })),
          field('结束年月', h('input', {
            type: 'month', value: draft.value.end_label,
            'aria-label': '结束年月', disabled: busy.value,
            onInput: event => { draft.value = { ...draft.value, end_label: event.target.value } },
          })),
        ]),
        h('p', { class: 'pd-hint' },
          '起止年月留空表示长期有效。填了起始年月，结账补记才知道这条规则该从哪个月开始补。'),
        h('p', { class: 'pd-row' }, [
          h('label', {}, [
            h('input', {
              type: 'checkbox', checked: draft.value.enabled,
              'aria-label': '启用这条规则', disabled: busy.value,
              onChange: event => { draft.value = { ...draft.value, enabled: event.target.checked } },
            }),
            ' 启用',
          ]),
          ' ',
          button(editingId.value ? '保存修改' : '添加规则', saveRule, !writable()),
          editingId.value ? button('取消编辑', resetDraft) : null,
        ]),
      ])

      const rulesPane = () => h('section', { class: 'pd-pane', 'data-testid': 'pd-rules' }, [
        ruleTable(),
        ruleForm(),
      ])

      // ------------------------------------------------------------- 执行页
      const monthPicker = () => h('div', { class: 'pd-month' }, [
        field('年份', h('input', {
          type: 'number', value: year.value, min: 1970, max: 2999,
          'aria-label': '目标年份', disabled: busy.value,
          onInput: event => { year.value = Number(event.target.value) || year.value },
        })),
        field('月份', h('select', {
          value: month.value, 'aria-label': '目标月份', disabled: busy.value,
          onChange: event => { month.value = Number(event.target.value) },
        }, Array.from({ length: 12 }, (_item, i) => h('option', { key: i + 1, value: i + 1 }, (i + 1) + ' 月')))),
      ])

      const pickPane = () => h('fieldset', { class: 'pd-box' }, [
        h('legend', {}, '选择要记账的月份和规则'),
        monthPicker(),
        h('table', { class: 'pd-table' }, [
          h('thead', {}, h('tr', {}, ['选', '规则', '每月', '凭证日期', '状态']
            .map(title => h('th', { scope: 'col' }, title)))),
          h('tbody', {}, rules.value.length ? rules.value.map(rule => {
            const done = runs.value.some(row => row.rule_id === rule.id
              && row.period_label === year.value + '年' + month.value + '月')
            return h('tr', { key: rule.id, class: done ? 'pd-off' : '' }, [
              h('td', {}, h('input', {
                type: 'checkbox', checked: Boolean(picked.value[rule.id]),
                disabled: busy.value || done,
                'aria-label': '选择 ' + rule.name,
                onChange: event => { picked.value = { ...picked.value, [rule.id]: event.target.checked } },
              })),
              h('td', {}, rule.name),
              h('td', {}, '第 ' + rule.day_of_month + ' 天'),
              h('td', {}, rule.day_of_month > 28
                ? year.value + '-' + String(month.value).padStart(2, '0') + '（当月最后一天）'
                : year.value + '-' + String(month.value).padStart(2, '0') + '-' + String(rule.day_of_month).padStart(2, '0')),
              h('td', {}, done ? '本月已记账' : (rule.enabled ? '待记账' : '规则已停用')),
            ])
          }) : [h('tr', { key: 'empty' }, h('td', { colspan: 5 }, '这个账套还没有规则，先到「周期规则」添加。'))]),
        ]),
        h('p', { class: 'pd-row' }, [
          button('试算', doPreview, !bookId.value),
          button('按选中规则记账', doRun, !writable() || !bookId.value),
        ]),
        h('p', { class: 'pd-hint' },
          '不勾选任何规则时，点「记账」会把当月所有到期的规则都记一遍。同一规则同一月份只会记一次，重复点不会多记。'),
      ])

      const previewPane = () => {
        const data = preview.value
        if (!data) return null
        const rows = data.plans || []
        return h('div', { class: 'pd-result', 'data-testid': 'pd-preview' }, [
          h('h3', {}, '试算结果（' + data.year + '年' + data.month + '月）'),
          data.problems?.length ? h('ul', { class: 'pd-err' }, data.problems.map((item, i) => h('li', { key: i }, item))) : null,
          rows.length ? h('table', { class: 'pd-table' }, [
            h('thead', {}, h('tr', {}, ['规则', '凭证日期', '借方合计', '凭证摘要']
              .map(title => h('th', { scope: 'col' }, title)))),
            h('tbody', {}, rows.map(row => h('tr', { key: row.rule_id }, [
              h('td', {}, row.name),
              h('td', {}, row.voucher_date),
              h('td', {}, Number(row.debit_total).toFixed(2)),
              h('td', {}, row.remark || '—'),
            ]))),
          ]) : h('p', { class: 'pd-hint' }, '没有需要生成的凭证。'),
          data.skipped?.length ? h('details', { class: 'pd-skip' }, [
            h('summary', {}, '跳过 ' + data.skipped.length + ' 条'),
            h('ul', {}, data.skipped.map((item, i) => h('li', { key: i }, item.name + '：' + item.reason))),
          ]) : null,
        ])
      }

      const resultPane = () => {
        const data = result.value
        if (!data) return null
        return h('div', { class: 'pd-result', 'data-testid': 'pd-result' }, [
          h('h3', {}, '记账结果'),
          data.created?.length ? h('table', { class: 'pd-table' }, [
            h('thead', {}, h('tr', {}, ['规则', '凭证号', '凭证日期', '金额']
              .map(title => h('th', { scope: 'col' }, title)))),
            h('tbody', {}, data.created.map(item => h('tr', { key: item.voucher_id || item.name }, [
              h('td', {}, item.name),
              h('td', {}, item.voucher_no || '—'),
              h('td', {}, item.voucher_date),
              h('td', {}, Number(item.amount).toFixed(2)),
            ]))),
          ]) : h('p', { class: 'pd-hint' }, '这次没有生成新凭证。'),
          data.failed?.length ? h('div', { class: 'pd-err' }, [
            h('p', {}, data.failed.length + ' 条没有记上：'),
            h('ul', {}, data.failed.map((item, i) => h('li', { key: i }, item.name + '：' + item.reason))),
          ]) : null,
          data.skipped?.length ? h('details', { class: 'pd-skip' }, [
            h('summary', {}, '跳过 ' + data.skipped.length + ' 条'),
            h('ul', {}, data.skipped.map((item, i) => h('li', { key: i }, item.name + '：' + item.reason))),
          ]) : null,
        ])
      }

      const executePane = () => h('section', { class: 'pd-pane', 'data-testid': 'pd-execute' }, [
        pickPane(),
        previewPane(),
        resultPane(),
      ])

      // ------------------------------------------------------------- 记录页
      const runsPane = () => h('section', { class: 'pd-pane', 'data-testid': 'pd-runs' }, [
        h('p', { class: 'pd-hint' },
          '这里记录每条规则每个月是否记过账。每条规则每个自然月最多生成一张凭证：'
          + '手动执行和定时任务同时开着也不会记两遍。撤销只删除这条记录，凭证本身仍在，需要请到凭证页自行处理。'),
        runs.value.length ? h('table', { class: 'pd-table' }, [
          h('thead', {}, h('tr', {}, ['月份', '规则', '凭证号', '凭证日期', '金额', '触发方式', '操作']
            .map(title => h('th', { scope: 'col' }, title)))),
          h('tbody', {}, runs.value.map(row => h('tr', { key: row.id }, [
            h('td', {}, row.period_label),
            h('td', {}, ruleName(row.rule_id)),
            h('td', {}, row.voucher_no || '—'),
            h('td', {}, row.voucher_date),
            h('td', {}, Number(row.amount || 0).toFixed(2)),
            h('td', {}, ({ catchup: '结账补记', scheduled: '定时任务', manual: '手动执行' })[row.trigger] || row.trigger),
            h('td', {}, h('button', {
              type: 'button', class: 'pd-btn pd-mini', disabled: busy.value || !writable(),
              onClick: () => askConfirm('run:' + row.id,
                '撤销「' + row.period_label + ' ' + ruleName(row.rule_id) + '」的记账记录？凭证 ' + (row.voucher_no || '') + ' 不会被删除。'),
            }, '撤销记录')),
          ]))),
        ]) : h('p', { class: 'pd-hint' }, '还没有执行记录。'),
      ])

      return () => h('section', { class: 'pd-root', 'data-testid': 'periodic' }, [
        h('h1', {}, '周期记账'),
        h('p', { class: 'pd-hint' },
          '为每个账套登记每月固定发生的支出（房租、管理费、社保等），按规则生成凭证。'
          + '每条规则每个自然月最多记一次账；结账时会补上漏记的月份。'
          + '自动记账需要管理员在「扩展管理 → 计划任务」里启用本插件的 monthly_posting 并指定执行身份，'
          + '没启用时不影响你在这里手动执行。'),
        bookBar(),
        tabBar,
        message.value ? h('p', { role: 'alert', class: 'pd-msg' }, message.value) : null,
        confirmBar(),
        tab.value === 'rules' ? rulesPane()
          : tab.value === 'execute' ? executePane() : runsPane(),
      ])
    },
  }
}
