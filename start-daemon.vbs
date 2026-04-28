Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "T:\Projects\redbridge\daemon"
shell.Run "cmd /c uv run uvicorn main:app --host 127.0.0.1 --port 47337 > ""%USERPROFILE%\.redbridge-daemon.log"" 2>&1", 0, False
