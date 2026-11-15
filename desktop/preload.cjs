const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('msgx', {
  call: (action, payload = {}) => ipcRenderer.invoke('msgx:call', action, payload),
  onClose: callback => ipcRenderer.on('msgx:close', (_event, options) => callback(options)),
  onUpdate: callback => ipcRenderer.on('msgx:update', (_event, state) => callback(state))
})
