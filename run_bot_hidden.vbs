Option Explicit

Dim shell, fso, processEnv, scriptDir, launcherPath, logDir, logPath, previousLogPath, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
Set processEnv = shell.Environment("PROCESS")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
launcherPath = fso.BuildPath(scriptDir, "run_bot.bat")
logDir = fso.BuildPath(scriptDir, "logs")
logPath = fso.BuildPath(logDir, "bot_runtime.log")
previousLogPath = fso.BuildPath(logDir, "bot_runtime.previous.log")

If Not fso.FileExists(launcherPath) Then
    WScript.Quit 1
End If
If Not fso.FolderExists(logDir) Then
    fso.CreateFolder logDir
End If

' Keep one previous 5 MB log so hidden startup failures remain diagnosable without unbounded growth.
If fso.FileExists(logPath) Then
    If fso.GetFile(logPath).Size >= 5242880 Then
        If fso.FileExists(previousLogPath) Then
            fso.DeleteFile previousLogPath, True
        End If
        fso.MoveFile logPath, previousLogPath
    End If
End If

processEnv("AUTOMOD_HEADLESS") = "1"
command = "cmd.exe /d /c call """ & launcherPath & """ >> """ & logPath & """ 2>&1"
shell.Run command, 0, False
WScript.Quit 0
