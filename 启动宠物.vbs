' Desktop pet launcher - runs with no console window.
'
' Why a .vbs instead of pointing the shortcut straight at dev.cmd:
' a .cmd shortcut keeps a black console window open for as long as the process
' runs, and the pet runs all day. WScript.Shell.Run with window style 0 hides it
' completely.
'
' NOTE: keep this file ASCII-only.
' VBScript reads .vbs using the system ANSI codepage (GBK on Chinese Windows).
' A file saved as UTF-8 with non-ASCII comments fails to compile with
' "invalid character" at the first such line -- so any Chinese text here would
' break the launcher outright. Chinese shown to the user is built with ChrW().
'
' When you need build output or an error message, use the "(Debug)" shortcut;
' it keeps the console.

Option Explicit

Dim shell, fso, root, cmd, rc, title
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Dialog title, built from code points so this file stays ASCII.
' U+684C U+9762 U+5BA0 U+7269 = "desktop pet"
title = ChrW(26700) & ChrW(38754) & ChrW(23456) & ChrW(29289)

' This file lives in the project root, so derive the path from itself
' instead of hardcoding a drive letter.
root = fso.GetParentFolderName(WScript.ScriptFullName)
cmd = Chr(34) & root & "\dev.cmd" & Chr(34)

If Not fso.FileExists(root & "\dev.cmd") Then
  MsgBox "dev.cmd not found:" & vbCrLf & root & vbCrLf & vbCrLf & _
         "This shortcut must sit next to the project root to work.", 16, title
  WScript.Quit 1
End If

' 0 = hidden window, True = wait for exit (so we get the real exit code)
rc = shell.Run(cmd, 0, True)

If rc <> 0 Then
  MsgBox "Launch failed (exit code " & rc & ")." & vbCrLf & vbCrLf & _
         "For the actual error, use the debug shortcut," & vbCrLf & _
         "which keeps the console output.", 16, title
End If
