Unregister-ScheduledTask -TaskName "claude-usage-daemon" -Confirm:$false

$action = New-ScheduledTaskAction `
  -Execute "wscript.exe" `
  -Argument "C:\code\HermannBjorgvin\Clawdmeter\daemon\start_daemon_hidden.vbs" `
  -WorkingDirectory "C:\code\HermannBjorgvin\Clawdmeter\daemon"

$trigger = New-ScheduledTaskTrigger -AtLogon
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0

$principal = New-ScheduledTaskPrincipal `
  -UserId "$env:USERDOMAIN\$env:USERNAME" `
  -LogonType Interactive `
  -RunLevel Limited

Register-ScheduledTask `
  -TaskName "claude-usage-daemon" `
  -Action $action `
  -Trigger $trigger `
  -Settings $settings `
  -Principal $principal `
  -Description "Clawdmeter BLE usage daemon"