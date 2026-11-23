export function el(tag, properties = {}, ...children) {
  const node = document.createElement(tag)
  for (const [name, value] of Object.entries(properties)) {
    if (value === undefined || value === null) continue
    if (name in node) node[name] = value
    else node.setAttribute(name, String(value))
  }
  for (const child of children.flat(Infinity)) {
    if (child !== null && child !== undefined) node.append(child instanceof Node ? child : String(child))
  }
  return node
}

export function button(text, onclick, properties = {}) {
  return el('button', { type: 'button', textContent: text, onclick, ...properties })
}

export function field(label, control) {
  return el('label', { className: 'field' }, el('span', {}, label), control)
}

export function table(headers, rows = []) {
  return el('table', {},
    el('thead', {}, el('tr', {}, headers.map(title => el('th', { scope: 'col' }, title)))),
    el('tbody', {}, rows.map(row => el('tr', {}, row.map(value => el('td', {}, value))))))
}

export function confirmAction(title, message) {
  if (document.querySelector('dialog.action-confirm[open]')) return Promise.resolve(false)
  return new Promise(resolve => {
    const dialog = el('dialog', { className: 'action-confirm' })
    dialog.setAttribute('aria-label', title)
    const cancel = button('取消', () => dialog.close('cancel'), { className: 'confirm-no' })
    const accept = button('确认', () => dialog.close('confirm'), { className: 'confirm-yes' })
    dialog.append(el('h2', {}, title), el('p', {}, message), el('div', { className: 'confirm-actions' }, accept, cancel))
    dialog.addEventListener('close', () => {
      dialog.remove()
      resolve(dialog.returnValue === 'confirm')
    }, { once: true })
    document.body.append(dialog)
    dialog.showModal()
    cancel.focus()
  })
}
