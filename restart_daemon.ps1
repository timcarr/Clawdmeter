Get-WmiObject Win32_Process | Where-Object { $_.CommandLine -like "*claude_usage_daemon*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-ScheduledTask "claude-usage-daemon"
