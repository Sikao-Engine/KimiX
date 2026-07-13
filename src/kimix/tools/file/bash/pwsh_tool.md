Run a simple PowerShell command. Prefer Python for complex or stateful tasks. Start a persistent session with interactive=True, then reuse the same tool with task_id=<id> to send input and read output in one step. Use wait_for_pattern to wait for a prompt. TaskOutput remains available as a fallback for listing/monitoring tasks. Send 'exit' to close the session.

PowerShell quick reference:
- Cmdlets use Verb-Noun names: Get-ChildItem (list files), Get-Content (read file), Set-Location (cd), Copy-Item, Move-Item, Remove-Item, New-Item, Select-String (grep), Get-Command, Get-Help.
- The pipeline `|` passes .NET objects, not plain text; shape results with Where-Object, Select-Object, ForEach-Object, Sort-Object, Measure-Object.
- Comparison operators: -eq -ne -gt -ge -lt -le, -like (wildcard), -match (regex), -contains (collection membership), -replace (regex replace). Logical operators: -and -or -not (alias `!`).
- Chain commands with `;` (always run next) or `&&` / `||` (PowerShell 7+: run next only on success / only on failure).
- Strings: 'single quotes' are literal; "double quotes" expand $variables and $(subexpressions).
- Environment variables: Use `$env:NAME` for session-scoped read/write. Use `[Environment]::SetEnvironmentVariable('NAME', 'value', 'Scope')` for persistence (Scope: User or Machine). Resolution priority: Process > User > Machine. List with `Get-ChildItem Env:`. Append PATH with `$env:PATH += ';new\path'` — never overwrite, check for duplicates first.
- $LASTEXITCODE holds the exit code of the last native command; $? is $true if the last command succeeded.
