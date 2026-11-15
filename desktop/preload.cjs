const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('msgx', {
  call: (action, payload = {}) => ipcRenderer.invoke('msgx:call', action, payload),
  onClose: callback => ipcRenderer.on('msgx:close', () => callback())
})
