const TABLE = 'plugin_25ac53a1cf75_config'
export default function createCsvVoucher(host) {
  const h = host.h
  return {
    name: 'CsvVoucher',
    setup() {
      const books = host.ref([]), target = host.ref(null), subjects = host.ref([])
      const config = host.ref(null), debit = host.ref(''), credit = host.ref('')
      const batches = host.ref([]), batch = host.ref(null), drafts = host.ref([])
      const files = host.ref([]), busy = host.ref(false), message = host.ref(''), dirty = host.ref(false), page = host.ref(0)
      const editable = () => batch.value && !['SUBMITTED', 'CANCELLED'].includes(batch.value.status)
      const canWrite = () => books.value.find(b => b.id === Number(target.value))?.can_write
      const errorText = error => {
        const detail = error?.response?.data?.detail
        return typeof detail === 'string' ? detail : detail?.message || '操作失败，请重新读取批次并检查权限'
      }
      async function act(work) {
        if (busy.value) return
        busy.value = true; message.value = ''
        try { await work() } catch (error) { message.value = errorText(error) }
        finally { busy.value = false }
      }
      function show(value) {
        batch.value = value
        drafts.value = JSON.parse(JSON.stringify(value.preview || []))
        dirty.value = false; page.value = 0
      }
      async function recent() { batches.value = (await host.services.imports.list(Number(target.value))).items }
      async function chooseBook() {
        batch.value = null; drafts.value = []; files.value = []; config.value = null
        if (!target.value) return
        const id = Number(target.value)
        subjects.value = (await host.api.get('/chart', { book_id: id })).filter(s => s.active && s.is_leaf)
        config.value = await host.services.config.get(TABLE, 'mapping', id)
        debit.value = config.value.value.debit_code || ''
        credit.value = config.value.value.credit_code || ''
        await recent()
      }
      async function upload() {
        show(await host.services.imports.upload(Number(target.value), files.value))
        files.value = []; await recent()
      }
      async function parse() { show(await host.api.post('/parse/' + batch.value.id, { revision: batch.value.revision })); await recent() }
      async function save() { show(await host.services.imports.edit(batch.value.id, batch.value.revision, drafts.value)); await recent() }
      async function confirm() { show(await host.services.imports.confirm(batch.value.id, batch.value.revision)); await recent() }
      async function submit() { show(await host.services.imports.submit(batch.value.id, batch.value.revision)); await recent() }
      async function cancel() { show(await host.services.imports.cancel(batch.value.id, batch.value.revision)); await recent() }
      async function saveConfig() {
        config.value = await host.services.config.put(TABLE, 'mapping', Number(target.value), config.value.version,
          { debit_code: debit.value, credit_code: credit.value })
        message.value = '账套科目配置已保存；已有预览需要重新解析并确认'
      }
      host.onMounted(() => act(async () => {
        books.value = await host.services.books()
        target.value = books.value.find(b => b.id === host.bookId() && b.can_write)?.id || books.value.find(b => b.can_write)?.id || books.value[0]?.id
        await chooseBook()
      }))
      const button = (text, fn, disabled = false) => h('button', { type: 'button', disabled: busy.value || disabled, onClick: () => act(fn) }, text)
      function field(label, value, change, type = 'text') {
        return h('input', { type, value, 'aria-label': label, disabled: busy.value || !editable() || !canWrite(),
          onInput: e => { change(e.target.value); dirty.value = true } })
      }
      const option = s => h('option', { value: s.code }, s.code + ' ' + s.name)
      return () => h('section', { class: 'cwq-csvvoucher', 'data-testid': 'csv-voucher' }, [
        h('h1', 'CSV 转凭证'),
        h('p', '选择目标账套，上传 CSV，检查并保存预览，确认后整批提交。全局账套切换不改变这里的目标账套。'),
        h('label', ['目标账套 ', h('select', { value: target.value, disabled: busy.value, 'aria-label': '目标账套',
          onChange: e => { target.value = Number(e.target.value); act(chooseBook) } }, books.value.map(b => h('option', { value: b.id }, b.name + (b.can_write ? '' : '（只读）'))))]),
        h('fieldset', { disabled: busy.value || !canWrite() || !config.value }, [h('legend', '本账套的科目配置'),
          h('label', ['借方科目 ', h('select', { value: debit.value, 'aria-label': '借方科目', onChange: e => { debit.value = e.target.value } }, [h('option', { value: '' }, '请选择'), ...subjects.value.map(option)])]),
          h('label', ['贷方科目 ', h('select', { value: credit.value, 'aria-label': '贷方科目', onChange: e => { credit.value = e.target.value } }, [h('option', { value: '' }, '请选择'), ...subjects.value.map(option)])]),
          button('保存账套配置', saveConfig, !debit.value || !credit.value),
        ]),
        h('p', 'CSV 列：date,amount,summary；日期 YYYY-MM-DD，金额为正数且最多两位小数，文件编码 UTF-8。'),
        h('input', { type: 'file', accept: '.csv,text/csv', multiple: true, disabled: busy.value || !canWrite(), 'aria-label': '上传 CSV 文件',
          onChange: e => { files.value = Array.from(e.target.files) } }),
        button('上传文件', upload, !files.value.length || !canWrite()),
        h('label', ['恢复批次 ', h('select', { value: batch.value?.id || '', disabled: busy.value, 'aria-label': '恢复批次',
          onChange: e => { if (e.target.value) act(async () => show(await host.services.imports.read(e.target.value))) } }, [h('option', { value: '' }, '选择已保存批次'), ...batches.value.map(b => h('option', { value: b.id }, b.created_at + ' · ' + b.status + ' · ' + b.files.map(f => f.filename).join('、')))])]),
        message.value ? h('p', { role: 'alert' }, message.value) : null,
        batch.value ? h('div', { 'data-testid': 'csv-batch' }, [
          h('p', { 'data-testid': 'csv-state' }, '批次 ' + batch.value.id + ' · ' + batch.value.status + ' · 修订 ' + batch.value.revision),
          button('重新读取批次', async () => show(await host.services.imports.read(batch.value.id))),
          button('解析并生成预览', parse, !editable() || !canWrite()),
          h('ul', batch.value.validation_errors.map(e => h('li', '第 ' + e.voucher + ' 张' + (e.line ? '第 ' + e.line + ' 行' : '') + '：' + e.message))),
          drafts.value.length ? h('div', [
            h('p', '共 ' + drafts.value.length + ' 张。下方修改需先保存预览，再确认；金额使用十进制文本。'),
            h('table', [h('thead', [h('tr', ['序号', '日期', '金额', '摘要', '借方科目', '贷方科目'].map(t => h('th', { scope: 'col' }, t)))]),
              h('tbody', drafts.value.slice(page.value * 20, (page.value + 1) * 20).map((v, i) => {
                const n = page.value * 20 + i + 1
                return h('tr', { key: n }, [h('td', String(n)),
                  h('td', field('日期 ' + n, v.voucher_date, value => { v.voucher_date = value }, 'date')),
                  h('td', field('金额 ' + n, v.entries[0].amount, value => { v.entries.forEach(e => { e.amount = value }) })),
                  h('td', field('摘要 ' + n, v.entries[0].summary, value => { v.entries.forEach(e => { e.summary = value }) })),
                  ...v.entries.map(e => h('td', subjects.value.find(s => s.id === e.subject_id)?.code || String(e.subject_id)))])
              }))]),
            button('上一页', async () => { page.value -= 1 }, page.value === 0),
            h('span', ' 第 ' + (page.value + 1) + ' 页 '),
            button('下一页', async () => { page.value += 1 }, (page.value + 1) * 20 >= drafts.value.length),
          ]) : null,
          button('保存调整后的预览', save, !dirty.value || !editable() || !canWrite()),
          button('确认当前预览', confirm, dirty.value || !['PREVIEW_READY', 'EDITING'].includes(batch.value.status) || batch.value.validation_errors.length > 0 || !canWrite()),
          button('整批提交', submit, dirty.value || batch.value.status !== 'CONFIRMED' || !canWrite()),
          button('取消批次', cancel, !editable() || !canWrite()),
          batch.value.result.length ? h('p', { 'data-testid': 'csv-result' }, '已生成凭证：' + batch.value.result.map(v => v.voucher_no).join('、')) : null,
        ]) : null,
      ])
    },
  }
}
