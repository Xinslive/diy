export default function createPeriodNote(host) {
  return {
    name: 'PeriodNote',
    setup() {
      const notes = host.ref([])
      const label = host.ref('')
      const message = host.ref('')
      const busy = host.ref(false)
      async function load() {
        const id = host.bookId()
        if (!id) { message.value = '请先选择账套'; return }
        try { notes.value = await host.api.get('/notes', { book_id: id }); message.value = '' }
        catch { message.value = '笔记暂不可用，请检查授权或联系管理员' }
      }
      async function save() {
        if (busy.value) return
        busy.value = true
        try {
          await host.api.post('/notes', { book_id: host.bookId(), label: label.value })
          label.value = ''
          await load()
        } catch { message.value = '保存失败，请检查账套权限与输入' }
        finally { busy.value = false }
      }
      host.onMounted(load)
      host.watch(() => host.bookId(), load)
      return () => host.h('section', { class: 'cwq-periodnote', 'data-testid': 'plugin-note' }, [
        host.h('h1', '期间工作笔记'),
        host.h('p', '笔记属于当前账套，读写均经过宿主账套权限检查。'),
        host.h('textarea', { value: label.value, maxlength: 120, placeholder: '填写工作笔记', 'aria-label': '工作笔记', onInput: e => { label.value = e.target.value } }),
        host.h('button', { type: 'button', disabled: busy.value, onClick: save }, busy.value ? '保存中…' : '保存笔记'),
        message.value ? host.h('p', { role: 'alert' }, message.value) : null,
        host.h('ul', notes.value.map(note => host.h('li', { key: note.id }, note.label))),
      ])
    },
  }
}
