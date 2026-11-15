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
