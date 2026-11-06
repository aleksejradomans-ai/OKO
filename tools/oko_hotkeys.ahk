#Requires AutoHotkey v2.0
#SingleInstance Force

; Управление службой бота (OkoBot) через NSSM.
;   Win+F10 - запуск бота
;   Win+F9  - остановка бота
; Ollama и OkoWatcher эти клавиши НЕ трогают.
; Каждое нажатие записывается в C:\oko\logs\hotkeys.log (время, действие, код nssm).

nssm := "C:\oko\nssm.exe"
service := "OkoBot"
logFile := "C:\oko\logs\hotkeys.log"

; Запуск и остановка службы требуют прав администратора:
; если скрипт запущен без них, он перезапускает сам себя с запросом UAC.
if !A_IsAdmin {
    Run('*RunAs "' . A_ScriptFullPath . '"')
    ExitApp()
}

WriteLog(text) {
    global logFile
    line := FormatTime(A_Now, "yyyy-MM-dd HH:mm:ss") . "  " . text . "`n"
    ; лог может быть занят другим процессом: 3 попытки, потом строка пропускается,
    ; горячие клавиши продолжают работать
    Loop 3 {
        try {
            DirCreate("C:\oko\logs")
            FileAppend(line, logFile, "UTF-8")
            return
        }
        Sleep(200)
    }
}

WriteLog("hotkeys запущены (Win+F10 = start, Win+F9 = stop)")

; Состояние службы берётся у Windows (WMI): Running, Stopped, Start Pending...
; Код nssm ненадёжен: при медленном старте он возвращает 1, хотя служба запускается.
ServiceState() {
    global service
    try {
        for s in ComObjGet("winmgmts:").ExecQuery("SELECT State FROM Win32_Service WHERE Name='" . service . "'")
            return s.State
    }
    return "Unknown"
}

; Ждёт нужное состояние до seconds секунд; возвращает то, что получилось.
WaitState(want, seconds) {
    Loop seconds * 2 {
        st := ServiceState()
        if (st = want)
            return st
        Sleep(500)
    }
    return ServiceState()
}

#F10:: {
    TrayTip("Запускаю бота...", "OkoBot")
    code := RunWait('"' . nssm . '" start ' . service, , "Hide")
    st := WaitState("Running", 20)
    WriteLog("Win+F10: nssm start " . service . ", код nssm " . code . ", итог " . st)
    TrayTip(st = "Running" ? "Бот запущен" : "Бот НЕ запустился: " . st, "OkoBot")
}

#F9:: {
    TrayTip("Останавливаю бота...", "OkoBot")
    code := RunWait('"' . nssm . '" stop ' . service, , "Hide")
    st := WaitState("Stopped", 20)
    WriteLog("Win+F9: nssm stop " . service . ", код nssm " . code . ", итог " . st)
    TrayTip(st = "Stopped" ? "Бот остановлен" : "Бот НЕ остановился: " . st, "OkoBot")
}
