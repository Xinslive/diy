/** Bounded, searchable controls. The original select remains the value/event source. */
export function enhanceSharedCardSelects(root) {
  let serial = Number(root.dataset.comboboxSerial || 0);
  root.querySelectorAll('select.scv-select').forEach(select => {
    if (select.hidden || (select.options.length < 7 && !['lineFrom', 'lineTo'].includes(select.dataset.field))) return;
    const wrap = document.createElement('span');
    wrap.className = 'scv-combobox';
    const input = document.createElement('input');
    const list = document.createElement('div');
    const label = select.getAttribute('aria-label') || '选择';
    const id = 'scv-options-' + (++serial);
    input.type = 'text'; input.className = 'scv-select scv-combobox-input';
    input.setAttribute('role', 'combobox'); input.setAttribute('aria-label', label);
    input.setAttribute('aria-autocomplete', 'list'); input.setAttribute('aria-controls', id);
    input.setAttribute('aria-expanded', 'false'); input.autocomplete = 'off';
    input.disabled = select.disabled;
    list.id = id; list.className = 'scv-combobox-options'; list.setAttribute('role', 'listbox'); list.hidden = true;
    const options = Array.from(select.options).map(option => ({ value: option.value, label: option.textContent, disabled: option.disabled }));
    let visible = options, active = -1, open = false;
    const selected = () => options.find(option => option.value === select.value)?.label || '';
    const close = () => { open = false; list.hidden = true; input.value = selected(); input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant'); };
    const choose = option => { if (!option || option.disabled) return; select.value = option.value; close(); select.dispatchEvent(new Event('change', { bubbles: true })); };
    const paint = () => {
      list.replaceChildren();
      if (!visible.length) { const empty = document.createElement('div'); empty.className = 'scv-combobox-empty'; empty.textContent = '没有匹配的选项'; list.append(empty); }
      visible.forEach((option, index) => {
        const item = document.createElement('div'); item.id = id + '-' + index;
        item.setAttribute('role', 'option'); item.setAttribute('aria-selected', String(option.value === select.value));
        item.className = 'scv-combobox-option' + (index === active ? ' is-active' : ''); item.textContent = option.label;
        if (option.disabled) item.setAttribute('aria-disabled', 'true');
        item.addEventListener('pointerdown', event => event.preventDefault());
        item.addEventListener('click', () => choose(option)); list.append(item);
      });
      if (active >= 0) { input.setAttribute('aria-activedescendant', id + '-' + active); list.children[active]?.scrollIntoView({ block: 'nearest' }); }
      else input.removeAttribute('aria-activedescendant');
    };
    const show = () => { if (open) return; open = true; input.value = ''; visible = options; active = options.findIndex(option => option.value === select.value); list.hidden = false; input.setAttribute('aria-expanded', 'true'); paint(); };
    input.value = selected(); input.placeholder = '输入年份 / 月份搜索';
    input.addEventListener('focus', show); input.addEventListener('click', show);
    input.addEventListener('input', () => { const query = input.value.trim().toLowerCase(); visible = options.filter(option => option.label.toLowerCase().includes(query) || option.value.toLowerCase().includes(query)); active = visible.length ? 0 : -1; paint(); });
    input.addEventListener('keydown', event => {
      if (event.key === 'Escape') { close(); event.preventDefault(); return; }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp' || event.key === 'Home' || event.key === 'End') {
        event.preventDefault(); show();
        active = event.key === 'Home' ? 0 : event.key === 'End' ? visible.length - 1 : Math.max(0, Math.min(visible.length - 1, active + (event.key === 'ArrowDown' ? 1 : -1))); paint();
      } else if (event.key === 'Enter' && open) { event.preventDefault(); choose(visible[active]); }
    });
    input.addEventListener('blur', () => { const exact = visible.find(option => option.label === input.value.trim() || option.value === input.value.trim()); if (open && input.value.trim() && exact) choose(exact); else close(); });
    select.after(wrap); select.hidden = true; wrap.append(input, list);
  });
  root.dataset.comboboxSerial = String(serial);
}
