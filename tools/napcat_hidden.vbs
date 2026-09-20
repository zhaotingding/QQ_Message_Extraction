Option Explicit
' msgx: launch NapCat (launcher.bat) with NO visible console window.
' argv(0) = NapCat install dir (contains launcher.bat)
' NOTE: this file must stay PURE ASCII -- wscript.exe reads .vbs as ANSI,
' so any non-ASCII byte here is a latent encoding bug.
Dim sh, fso, dir, rc
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
If WScript.Arguments.Count < 1 Then
    WScript.Quit 1
End If
dir = WScript.Arguments(0)
If Not fso.FileExists(dir & "\launcher.bat") Then
    MsgBox "launcher.bat not found in:" & vbCrLf & dir, 16, "msgx - NapCat"
    WScript.Quit 2
End If
sh.CurrentDirectory = dir
' 0 = hidden window, True = wait for it to finish
rc = sh.Run("""" & dir & "\launcher.bat""", 0, True)
If rc <> 0 Then
    MsgBox "NapCat launcher.bat exited with code " & rc & vbCrLf & vbCrLf & _
           "Run tools\start_napcat.bat manually to see the full output.", 16, "msgx - NapCat"
End If
WScript.Quit rc
