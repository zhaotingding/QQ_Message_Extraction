' 消息萃取：无控制台窗口启动图形界面
' start_gui.cmd 会再拉起 pythonw.exe，所以全程看不到黑框。
Option Explicit

Dim sh, fso, here, cmd
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

here = fso.GetParentFolderName(WScript.ScriptFullName)
cmd = """" & here & "\start_gui.cmd"""

' 第二个参数 0 = 隐藏窗口，第三个参数 False = 不等待返回
sh.Run cmd, 0, False
