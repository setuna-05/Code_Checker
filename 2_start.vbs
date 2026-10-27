' Code Checker launcher (no black window while the app is running).
' Runs 2_start.bat hidden and saves its messages to start_log.txt.
' Only when the app stops with an error, the messages are shown in a black window.
Option Explicit

Dim sh, fso, q, dir, bat, logf, rc
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
q = Chr(34)
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
bat = dir & "\2_start.bat"
logf = dir & "\start_log.txt"

rc = sh.Run("cmd /c " & q & q & bat & q & " hidden > " & q & logf & q & " 2>&1" & q, 0, True)

If rc <> 0 Then
    sh.Run "cmd /c " & q & "type " & q & logf & q & " & echo. & pause" & q, 1, False
End If
