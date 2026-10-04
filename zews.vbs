' Starts Zews without a console window: tray -> hidden Claude session + panel.
' Safe to run twice: tray and panel never start a second copy.
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & root & "\zews-tray.ps1"" -Minimized", 0, False
