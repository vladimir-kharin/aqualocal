' Тихий запуск AquaLocal без консольного окна.
' Путь берётся относительно расположения самого .vbs — ничего не хардкодим.
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = fso.BuildPath(scriptDir, ".venv\Scripts\pythonw.exe")
app = fso.BuildPath(scriptDir, "app.py")

shell.CurrentDirectory = scriptDir
shell.Run """" & pythonw & """ """ & app & """", 0, False
