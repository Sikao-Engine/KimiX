"""Vendored pure-Python mirrors of the kimi-agent bash/pwsh scanners.

Copied (with attribution) from the kimi-agent repo's kimi-cli/src/kimi_cli/tools/file/bash/
so the shim has a bit-exact fallback when the reference checkout is not
importable. Only the self-contained scanner code is vendored (no tool I/O).

Reference files: bash_fix.py (the _Scanner + fallback data), bash_tool.py
(_process_unquoted + helpers, lines 440-756), pwsh_fix.py, process_pwsh.py.
"""

from __future__ import annotations

# ======================================================================
# bash_fix.py (kimi-agent) - Windows Git Bash compatibility scanner
# ======================================================================
import sys
import tempfile
from dataclasses import dataclass
from typing import Callable

try:
    import regex as re
except ImportError:  # pragma: no cover
    import re  # stdlib: the used patterns are all stdlib-compatible

_REV_PERL = (
    "perl '-Mopen=:std,:encoding(UTF-8)' -e '"
    "my $zero = shift @ARGV; my $failed = 0; "
    "sub reverse_fh { my ($fh, $zero) = @_; "
    "local $/ = $zero ? qq(\\0) : qq(\\n); "
    "while (my $record = <$fh>) { "
    "my $ended = $zero ? $record =~ s/\\0\\z// : $record =~ s/\\r?\\n\\z//; "
    "print scalar reverse($record); "
    "print($zero ? qq(\\0) : qq(\\n)) if $ended } } "
    "if (@ARGV) { for my $file (@ARGV) { "
    "if (open my $fh, q(<:encoding(UTF-8)), $file) { reverse_fh($fh, $zero); close $fh } "
    "else { warn qq(rev: $file: $!\\n); $failed = 1 } } } "
    "else { reverse_fh(*STDIN, $zero) } exit $failed'"
)

_NATIVE_DELEGATE = (
    "local __kimix_native=''; __kimix_native=$(type -P {name}) || :; "
    "if [[ -n $__kimix_native ]]; then \"$__kimix_native\" \"$@\"; return; fi; "
)

# Fallbacks whose ``command -v`` hit can be a non-functional placeholder: the
# Microsoft Store App Execution Alias stubs in ``WindowsApps`` print an
# install prompt instead of running the tool.  They get a stub-aware guard
# (define the fallback even when ``command -v`` succeeds) and a delegate
# that refuses stub paths.
_STUB_AWARE_FALLBACKS = frozenset({"pip3", "python3"})

_PGREP_PS_NAME = (
    "$m = Get-Process | Where-Object { $_.Name -match $env:__KIMIX_PAT }; "
    "if ($m) { $m | ForEach-Object { if ($env:__KIMIX_LIST -eq \"1\") { "
    "\"$($_.Id) $($_.Name)\" } else { $_.Id } }; exit 0 } else { exit 1 }"
)
_PGREP_PS_FULL = (
    "$m = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match $env:__KIMIX_PAT }; "
    "if ($m) { $m | ForEach-Object { if ($env:__KIMIX_LIST -eq \"1\") { "
    "\"$($_.ProcessId) $($_.Name)\" } else { $_.ProcessId } }; exit 0 } else { exit 1 }"
)
_PKILL_PS_NAME = (
    "$m = Get-Process | Where-Object { $_.Name -match $env:__KIMIX_PAT }; "
    "if ($m) { $m | Stop-Process -Force; exit 0 } else { exit 1 }"
)
_PKILL_PS_FULL = (
    "$m = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match $env:__KIMIX_PAT }; "
    "if ($m) { $m | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; "
    "exit 0 } else { exit 1 }"
)

_ZIP_PS = (
    "Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem; "
    "$level = [System.IO.Compression.CompressionLevel]$env:__KIMIX_ZIP_LEVEL; "
    "$dest = $env:__KIMIX_ZIP_DEST; "
    "if (Test-Path -LiteralPath $dest) { Remove-Item -LiteralPath $dest -Force }; "
    "$zip = [System.IO.Compression.ZipFile]::Open($dest, [System.IO.Compression.ZipArchiveMode]::Create); "
    "foreach ($p in ($env:__KIMIX_ZIP_PATHS -split \"`n\")) { "
    "$item = Get-Item -LiteralPath $p; $base = $item.Name; "
    "if ($item.PSIsContainer) { $root = $item.FullName; "
    "Get-ChildItem -LiteralPath $root -Recurse -Force | ForEach-Object { "
    "$rel = $_.FullName.Substring($root.Length).TrimStart(\"\\\") -replace \"\\\\\", \"/\"; "
    "if ($_.PSIsContainer) { $zip.CreateEntry($base + \"/\" + $rel + \"/\") | Out-Null } "
    "else { [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $_.FullName, $base + \"/\" + $rel, $level) | Out-Null } } } "
    "else { [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $item.FullName, $base, $level) | Out-Null } }; "
    "$zip.Dispose(); if (Test-Path -LiteralPath $dest) { exit 0 } else { exit 1 }"
)

_TREE_PERL = (
    "perl -e '"
    "my ($maxdepth,$showall,$dirsonly,$noreport,$top)=@ARGV; "
    "print qq($top\\n); "
    "my ($ndirs,$nfiles)=(0,0); "
    "sub walk { my ($path,$prefix,$depth)=@_; "
    "return if $maxdepth && $depth>$maxdepth; "
    "opendir(my $dh,$path) or return; "
    "my @e = grep { ! /^[.][.]?$/ } readdir($dh); closedir($dh); "
    "@e = grep { $showall || ! /^[.]/ } @e; "
    "@e = grep { ! $dirsonly || -d qq($path/$_) } @e; "
    "@e = sort { lc($a) cmp lc($b) } @e; "
    "my $n=@e; my $i=0; "
    "for my $e (@e) { $i++; my $last = $i==$n; my $full = qq($path/$e); "
    "my $isdir = -d $full; "
    "if ($isdir) { $ndirs++ } else { $nfiles++ } "
    "print $prefix, ($last ? qq(`-- ) : qq(|-- )), $e, qq(\\n); "
    "walk($full, $prefix . ($last ? qq(    ) : qq(|   )), $depth+1) "
    "if $isdir && ! -l $full } } "
    "walk($top,q(),1); "
    "my $dw = $ndirs==1 ? q(directory) : q(directories); "
    "my $fw = $nfiles==1 ? q(file) : q(files); "
    "print qq(\\n$ndirs $dw, $nfiles $fw\\n) unless $noreport'"
)

_POWERSHELL_PASTE = (
    "powershell.exe -NoProfile -NonInteractive -Command "
    "'[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
    "[Console]::Out.Write((Get-Clipboard -Raw))'"
)

# Windows cmd-style command fallbacks.  These names are not part of the Git
# Bash POSIX userland and are commonly emitted by agents accustomed to cmd.exe
# or cross-platform documentation.  Each fallback is only installed when Git
# Bash cannot already resolve the command via ``command -v``.

_TASKLIST_PS = (
    "Get-Process | Select-Object Name, Id, CPU, WorkingSet | Format-Table -AutoSize"
)

_TASKKILL_PS = (
    # ``[System.Management.Automation.ActionPreference]`` (not the string ``'Stop'``)
    # because the whole script lives inside a single-quoted bash ``-Command`` word
    # that cannot carry quotes.  ``Stop`` turns cmdlet failures (unknown PID,
    # access denied) into catchable exceptions so a failed kill exits non-zero
    # like real ``taskkill.exe`` instead of unconditionally reporting success.
    "$ErrorActionPreference = [System.Management.Automation.ActionPreference]::Stop; "
    "$force = $env:__KIMIX_FORCE -eq '1'; "
    "$ok = $true; "
    # Process *both* operands when combined (``/IM x /PID 1``): a failing PID
    # kill must not be masked by ``exit 0``, and ``/IM`` must not be silently
    # dropped when ``/PID`` is also present.
    "if ($env:__KIMIX_PID) { "
    "try { Stop-Process -Id $env:__KIMIX_PID -Force:$force } catch { $ok = $false } "
    "} "
    # taskkill's /IM operand is the image name *with* its extension (``notepad.exe``),
    # but Get-Process ``.Name`` excludes it (``notepad``); normalise so both spellings
    # and mixed case match.  ``[IO.Path]`` avoids quotes, which the surrounding
    # single-quoted bash ``-Command`` string cannot carry.
    "$im = [IO.Path]::GetFileNameWithoutExtension($env:__KIMIX_IM); "
    "$procs = Get-Process | Where-Object { $_.Name -eq $im }; "
    "if ($procs) { try { $procs | Stop-Process -Force:$force } catch { $ok = $false } } "
    "elseif ($env:__KIMIX_IM) { $ok = $false }; "
    "if ($ok) { exit 0 } else { exit 1 }"
)

_SYSTEMINFO_PS = "Get-ComputerInfo | Format-List"

_KILLALL_PS = (
    "$procs = Get-Process | Where-Object { $_.Name -eq $env:__KIMIX_NAME }; "
    "if ($procs) { $procs | Stop-Process -Force; exit 0 } else { exit 1 }"
)

_PIDOF_PS = (
    "$ids = (Get-Process | Where-Object { $_.Name -eq $env:__KIMIX_NAME }).Id; "
    "if ($ids) { $ids -join \" \"; exit 0 } else { exit 1 }"
)

_COLUMN_PERL = (
    "perl -e '"
    "my $sep = shift @ARGV; $sep = qr/\\s+/ if $sep eq \"DEFAULT\"; "
    "my @rows; my @max; "
    "while (<>) { chomp; my @c = split $sep; push @rows, \\@c; "
    "for my $i (0..$#c) { $max[$i] = length($c[$i]) if !defined $max[$i] || length($c[$i]) > $max[$i]; } } "
    "for my $r (@rows) { print join(\"  \", map { sprintf(\"%-*s\", $max[$_]//0, $r->[$_]) } 0..$#$r), \"\\n\"; }'"
)

_FALLBACK_BODIES = {
    "gtimeout": "timeout \"$@\"",
    "rev": (
        "local __kimix_zero=0; while (( $# )); do case $1 in "
        "-0|--zero) __kimix_zero=1; shift;; "
        "--) shift; break;; "
        "-*) printf '%s\\n' \"rev: unsupported option: $1\" >&2; return 1;; "
        "*) break;; esac; done; "
        + _REV_PERL
        + " -- \"$__kimix_zero\" \"$@\""
    ),
    "xdg-open": "start \"$@\"",
    "open": "start \"$@\"",
    "pbcopy": "clip.exe \"$@\"",
    "pbpaste": _POWERSHELL_PASTE + " \"$@\"",
    "wget": (
        "local __kimix_url='' __kimix_out='' __kimix_stdout=0; "
        "local -a __kimix_args=(); "
        "while (( $# )); do case $1 in "
        "-O|--output-document) [[ $# -ge 2 ]] || { printf '%s\\n' \"wget: option requires an argument -- '${1//[![:alnum:]]/}'\" >&2; return 1; }; __kimix_out=$2; shift 2;; "
        "-O?*) __kimix_out=${1#-O}; shift;; "
        "--output-document=*) __kimix_out=${1#*=}; shift;; "
        "-q|--quiet) __kimix_args+=(-s); shift;; "
        "-c|--continue) __kimix_args+=(-C -); shift;; "
        "--no-check-certificate) __kimix_args+=(-k); shift;; "
        "-T|--timeout) [[ $# -ge 2 ]] || { printf '%s\\n' \"wget: option requires an argument -- '${1//[![:alnum:]]/}'\" >&2; return 1; }; __kimix_args+=(--max-time \"$2\"); shift 2;; "
        "--timeout=*) __kimix_args+=(--max-time \"${1#*=}\"); shift;; "
        "-*) printf '%s\\n' \"wget: unsupported option for curl fallback: $1\" >&2; return 1;; "
        "*) __kimix_url=$1; shift;; esac; done; "
        "if [[ -z $__kimix_url ]]; then "
        "printf '%s\\n' 'wget: missing URL' >&2; return 1; fi; "
        "if [[ $__kimix_out == '-' ]]; then __kimix_stdout=1; fi; "
        "if [[ -z $__kimix_out && $__kimix_stdout -eq 0 ]]; then "
        "__kimix_out=${__kimix_url##*/}; "
        "[[ -n $__kimix_out ]] || __kimix_out=index.html; fi; "
        "if (( __kimix_stdout )); then "
        "curl -fSL \"${__kimix_args[@]}\" -- \"$__kimix_url\"; "
        "else curl -fSL \"${__kimix_args[@]}\" -o \"$__kimix_out\" -- \"$__kimix_url\"; fi"
    ),
    "xclip": (
        "local __kimix_out=0; while (( $# )); do case $1 in "
        "-o|-out) __kimix_out=1; shift;; "
        "-i|-in) shift;; "
        "-selection|-d|-display) [[ $# -ge 2 ]] || { printf '%s\\n' \"xclip: option requires an argument -- '${1//[![:alnum:]]/}'\" >&2; return 1; }; shift 2;; "
        "-selection*|-display*) shift;; "
        "-*) printf '%s\\n' \"xclip: unsupported option for clipboard fallback: $1\" >&2; return 1;; "
        "*) shift;; esac; done; "
        "if (( __kimix_out )); then " + _POWERSHELL_PASTE + "; else clip.exe; fi"
    ),
    "xsel": (
        "local __kimix_out=0; while (( $# )); do case $1 in "
        "--output) __kimix_out=1; shift;; "
        "--input|--clipboard|--primary|--secondary) shift;; "
        "--*) printf '%s\\n' \"xsel: unsupported option for clipboard fallback: $1\" >&2; return 1;; "
        "-*) case $1 in *o*) __kimix_out=1;; esac; shift;; "
        "*) shift;; esac; done; "
        "if (( __kimix_out )); then " + _POWERSHELL_PASTE + "; else clip.exe; fi"
    ),
    "wl-copy": (
        "while (( $# )); do case $1 in "
        "-*) printf '%s\\n' \"wl-copy: unsupported option for clipboard fallback: $1\" >&2; return 1;; "
        "*) shift;; esac; done; clip.exe"
    ),
    "wl-paste": (
        "while (( $# )); do case $1 in "
        "-n|--no-newline) shift;; "
        "-*) printf '%s\\n' \"wl-paste: unsupported option for clipboard fallback: $1\" >&2; return 1;; "
        "*) shift;; esac; done; " + _POWERSHELL_PASTE
    ),
    "zip": (
        "local __kimix_archive='' __kimix_level=Optimal __kimix_p='' "
        "__kimix_combo='' __kimix_i=0; "
        "local -a __kimix_paths=() __kimix_wpaths=() __kimix_split=(); "
        "while (( $# )); do "
        "if [[ $1 == -[!-]* && ${#1} -gt 2 ]]; then "
        "__kimix_combo=${1#-}; __kimix_split=(); shift; "
        "for (( __kimix_i=0; __kimix_i<${#__kimix_combo}; __kimix_i++ )); do "
        "__kimix_split+=(-${__kimix_combo:__kimix_i:1}); done; "
        "set -- \"${__kimix_split[@]}\" \"$@\"; continue; fi; "
        "case $1 in "
        "-r|-R|--recurse-paths|-q|--quiet) shift;; "
        "-0) __kimix_level=NoCompression; shift;; "
        "-1) __kimix_level=Fastest; shift;; "
        "-[2-9]) shift;; "
        "-*) printf '%s\\n' \"zip: unsupported option for Compress-Archive fallback: $1\" >&2; return 1;; "
        "*) if [[ -z $__kimix_archive ]]; then __kimix_archive=$1; "
        "else __kimix_paths+=(\"$1\"); fi; shift;; esac; done; "
        "if [[ -z $__kimix_archive || ${#__kimix_paths[@]} -eq 0 ]]; then "
        "printf '%s\\n' 'zip: missing archive name or input paths' >&2; return 1; fi; "
        "for __kimix_p in \"${__kimix_paths[@]}\"; do "
        "__kimix_wpaths+=(\"$(cygpath -w -- \"$__kimix_p\")\"); done; "
        "__kimix_archive=$(cygpath -w -- \"$__kimix_archive\"); "
        "__KIMIX_ZIP_LEVEL=$__kimix_level __KIMIX_ZIP_DEST=$__kimix_archive "
        "__KIMIX_ZIP_PATHS=$(printf '%s\\n' \"${__kimix_wpaths[@]}\") "
        "powershell.exe -NoProfile -NonInteractive -Command '" + _ZIP_PS + "'"
    ),
    "nc": (
        "local __kimix_z=0 __kimix_v=0 __kimix_w='' __kimix_host='' __kimix_port=''; "
        "while (( $# )); do case $1 in "
        "-z) __kimix_z=1; shift;; "
        "-v) __kimix_v=1; shift;; "
        "-zv|-vz) __kimix_z=1; __kimix_v=1; shift;; "
        "-w) [[ $# -ge 2 ]] || { printf '%s\\n' \"nc: option requires an argument -- 'w'\" >&2; return 1; }; __kimix_w=$2; shift 2;; "
        "-w?*) __kimix_w=${1#-w}; shift;; "
        "-*) printf '%s\\n' \"nc: unsupported option for /dev/tcp fallback: $1\" >&2; return 1;; "
        "*) if [[ -z $__kimix_host ]]; then __kimix_host=$1; "
        "elif [[ -z $__kimix_port ]]; then __kimix_port=$1; "
        "else printf '%s\\n' 'nc: too many arguments' >&2; return 1; fi; "
        "shift;; esac; done; "
        "if (( ! __kimix_z )); then "
        "printf '%s\\n' 'nc: only -z (zero-I/O scan) mode is supported by this fallback' >&2; "
        "return 1; fi; "
        "if [[ -z $__kimix_host || -z $__kimix_port ]]; then "
        "printf '%s\\n' 'nc: missing host or port' >&2; return 1; fi; "
        "if [[ -n $__kimix_w ]]; then "
        "timeout \"$__kimix_w\" bash -c 'exec 3<>/dev/tcp/$1/$2' _ "
        "\"$__kimix_host\" \"$__kimix_port\" 2>/dev/null; "
        "else (exec 3<>/dev/tcp/\"$__kimix_host\"/\"$__kimix_port\") 2>/dev/null; fi; "
        "local __kimix_rc=$?; "
        "(( __kimix_rc != 0 )) && __kimix_rc=1; "
        "if (( __kimix_rc == 0 )); then "
        "(( __kimix_v )) && printf '%s\\n' \"Connection to $__kimix_host $__kimix_port port [tcp/*] succeeded!\" >&2; "
        "else "
        "(( __kimix_v )) && printf '%s\\n' \"nc: connect to $__kimix_host port $__kimix_port (tcp) failed\" >&2; fi; "
        "return $__kimix_rc"
    ),
    "pgrep": (
        "local __kimix_list=0 __kimix_full=0 __kimix_pat=''; "
        "while (( $# )); do case $1 in "
        "-l) __kimix_list=1; shift;; "
        "-f) __kimix_full=1; shift;; "
        "-lf|-fl) __kimix_list=1; __kimix_full=1; shift;; "
        "--) shift; break;; "
        "-*) printf '%s\\n' \"pgrep: unsupported option for Get-Process fallback: $1\" >&2; return 1;; "
        "*) __kimix_pat=$1; shift;; esac; done; "
        "if [[ -z $__kimix_pat && $# -gt 0 ]]; then __kimix_pat=$1; fi; "
        "if [[ -z $__kimix_pat ]]; then "
        "printf '%s\\n' 'pgrep: missing pattern' >&2; return 1; fi; "
        "if (( __kimix_full )); then "
        "__KIMIX_PAT=$__kimix_pat __KIMIX_LIST=$__kimix_list "
        "powershell.exe -NoProfile -NonInteractive -Command '" + _PGREP_PS_FULL + "'; "
        "else "
        "__KIMIX_PAT=$__kimix_pat __KIMIX_LIST=$__kimix_list "
        "powershell.exe -NoProfile -NonInteractive -Command '" + _PGREP_PS_NAME + "'; fi"
    ),
    "pkill": (
        "local __kimix_full=0 __kimix_pat=''; "
        "while (( $# )); do case $1 in "
        "-f) __kimix_full=1; shift;; "
        "--) shift; break;; "
        "-*) printf '%s\\n' \"pkill: unsupported option for Stop-Process fallback: $1\" >&2; return 1;; "
        "*) __kimix_pat=$1; shift;; esac; done; "
        "if [[ -z $__kimix_pat && $# -gt 0 ]]; then __kimix_pat=$1; fi; "
        "if [[ -z $__kimix_pat ]]; then "
        "printf '%s\\n' 'pkill: missing pattern' >&2; return 1; fi; "
        "if (( __kimix_full )); then "
        "__KIMIX_PAT=$__kimix_pat "
        "powershell.exe -NoProfile -NonInteractive -Command '" + _PKILL_PS_FULL + "'; "
        "else "
        "__KIMIX_PAT=$__kimix_pat "
        "powershell.exe -NoProfile -NonInteractive -Command '" + _PKILL_PS_NAME + "'; fi"
    ),
    "traceroute": (
        "local -a __kimix_args=(); "
        "while (( $# )); do case $1 in "
        "-n) __kimix_args+=(-d); shift;; "
        "-m) [[ $# -ge 2 ]] || { printf '%s\\n' \"traceroute: option requires an argument -- 'm'\" >&2; return 1; }; __kimix_args+=(-h \"$2\"); shift 2;; "
        "-m?*) __kimix_args+=(-h \"${1#-m}\"); shift;; "
        "--max-hop=*) __kimix_args+=(-h \"${1#*=}\"); shift;; "
        "-w) [[ $# -ge 2 ]] || { printf '%s\\n' \"traceroute: option requires an argument -- 'w'\" >&2; return 1; }; __kimix_args+=(-w \"$(( $2 * 1000 ))\"); shift 2;; "
        "-w?*) __kimix_args+=(-w \"$(( ${1#-w} * 1000 ))\"); shift;; "
        "-*) printf '%s\\n' \"traceroute: unsupported option for tracert fallback: $1\" >&2; return 1;; "
        "*) __kimix_args+=(\"$1\"); shift;; esac; done; "
        "tracert \"${__kimix_args[@]}\""
    ),
    "tree": (
        "local __kimix_depth=0 __kimix_all=0 __kimix_dirsonly=0 __kimix_noreport=0; "
        "local -a __kimix_dirs=(); "
        "while (( $# )); do case $1 in "
        "-L) [[ $# -ge 2 ]] || { printf '%s\\n' \"tree: option requires an argument -- 'L'\" >&2; return 1; }; __kimix_depth=$2; shift 2;; "
        "-L?*) __kimix_depth=${1#-L}; shift;; "
        "-a) __kimix_all=1; shift;; "
        "-d) __kimix_dirsonly=1; shift;; "
        "--noreport) __kimix_noreport=1; shift;; "
        "--) shift; __kimix_dirs+=(\"$@\"); break;; "
        "-*) printf '%s\\n' \"tree: unsupported option for perl fallback: $1\" >&2; return 1;; "
        "*) __kimix_dirs+=(\"$1\"); shift;; esac; done; "
        "[[ ${#__kimix_dirs[@]} -gt 0 ]] || __kimix_dirs=(.); "
        "local __kimix_d; "
        "for __kimix_d in \"${__kimix_dirs[@]}\"; do "
        + _TREE_PERL
        + " -- \"$__kimix_depth\" \"$__kimix_all\" \"$__kimix_dirsonly\" \"$__kimix_noreport\" \"$__kimix_d\"; done"
    ),
    "say": (
        "while (( $# )); do case $1 in "
        "-*) printf '%s\\n' \"say: unsupported option for SAPI fallback: $1\" >&2; return 1;; "
        "*) shift;; esac; done; "
        "__KIMIX_SAY_TEXT=$* "
        "powershell.exe -NoProfile -NonInteractive -Command "
        "'Add-Type -AssemblyName System.Speech; "
        "(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak($env:__KIMIX_SAY_TEXT)'"
    ),
    "python3": 'python "$@"',
    "pip3": 'pip "$@"',
    # Windows cmd-style commands -> POSIX/Git Bash equivalents.
    "copy": (
        "if [[ $# -lt 2 ]]; then "
        "printf '%s\\n' 'copy: missing source or destination' >&2; return 1; fi; "
        "cp -R -- \"$@\""
    ),
    "move": (
        "if [[ $# -lt 2 ]]; then "
        "printf '%s\\n' 'move: missing source or destination' >&2; return 1; fi; "
        "mv -- \"$@\""
    ),
    "del": "rm -- \"$@\"",
    "erase": "rm -- \"$@\"",
    "ren": (
        "if [[ $# -ne 2 ]]; then "
        "printf '%s\\n' 'ren: exactly two arguments required' >&2; return 1; fi; "
        "mv -- \"$1\" \"$2\""
    ),
    "rename": (
        "if [[ $# -ne 2 ]]; then "
        "printf '%s\\n' 'rename: exactly two arguments required' >&2; return 1; fi; "
        "mv -- \"$1\" \"$2\""
    ),
    "rd": "rmdir -- \"$@\"",
    "md": "mkdir -p -- \"$@\"",
    "chdir": "cd -- \"$@\"",
    "cls": "clear",
    "xcopy": "cp -r -- \"$@\"",
    "mklink": (
        "local __kimix_hard=0 __kimix_link='' __kimix_target=''; "
        "while (( $# )); do case $1 in "
        "/D|/d|/J|/j) shift;; "
        "/H|/h) __kimix_hard=1; shift;; "
        "*) if [[ -z $__kimix_link ]]; then __kimix_link=$1; "
        "elif [[ -z $__kimix_target ]]; then __kimix_target=$1; "
        "else printf '%s\\n' 'mklink: too many arguments' >&2; return 1; fi; "
        "shift;; esac; done; "
        "if [[ -z $__kimix_link || -z $__kimix_target ]]; then "
        "printf '%s\\n' 'mklink: missing link name or target' >&2; return 1; fi; "
        "if (( __kimix_hard )); then ln -f -- \"$__kimix_target\" \"$__kimix_link\"; "
        "else ln -s -- \"$__kimix_target\" \"$__kimix_link\"; fi"
    ),
    "findstr": "grep \"$@\"",
    "fc": "diff \"$@\"",
    "where": "which \"$@\"",
    "tasklist": (
        "powershell.exe -NoProfile -NonInteractive -Command '" + _TASKLIST_PS + "'"
    ),
    "taskkill": (
        "local __kimix_force=0 __kimix_pid='' __kimix_im=''; "
        "while (( $# )); do case $1 in "
        "/F|/f) __kimix_force=1; shift;; "
        "/IM|/im) [[ $# -ge 2 ]] || { printf '%s\\n' \"taskkill: option requires an argument -- '${1//[![:alnum:]]/}'\" >&2; return 1; }; __kimix_im=$2; shift 2;; "
        "/PID|/pid) [[ $# -ge 2 ]] || { printf '%s\\n' \"taskkill: option requires an argument -- '${1//[![:alnum:]]/}'\" >&2; return 1; }; __kimix_pid=$2; shift 2;; "
        "/*) printf '%s\\n' \"taskkill: unsupported option: $1\" >&2; return 1;; "
        "*) printf '%s\\n' \"taskkill: unsupported argument: $1\" >&2; return 1;; esac; done; "
        "if [[ -n $__kimix_pid ]]; then "
        "__KIMIX_FORCE=$__kimix_force __KIMIX_PID=$__kimix_pid "
        "powershell.exe -NoProfile -NonInteractive -Command '" + _TASKKILL_PS + "'; "
        "elif [[ -n $__kimix_im ]]; then "
        "__KIMIX_FORCE=$__kimix_force __KIMIX_IM=$__kimix_im "
        "powershell.exe -NoProfile -NonInteractive -Command '" + _TASKKILL_PS + "'; "
        "else printf '%s\\n' 'taskkill: missing /PID or /IM' >&2; return 1; fi"
    ),
    "systeminfo": (
        "powershell.exe -NoProfile -NonInteractive -Command '" + _SYSTEMINFO_PS + "'"
    ),
    # POSIX utilities often absent from a bare Git Bash userland.
    "watch": (
        "local __kimix_interval=2; "
        "while (( $# )); do case $1 in "
        "-n|--interval) [[ $# -ge 2 ]] || { printf '%s\\n' \"watch: option requires an argument -- '${1//[![:alnum:]]/}'\" >&2; return 1; }; __kimix_interval=$2; shift 2;; "
        "--interval=*) __kimix_interval=${1#*=}; shift;; "
        "-n?*) __kimix_interval=${1#-n}; shift;; "
        "-t|-d|--no-title|--color) shift;; "
        "--) shift; break;; "
        "-*) printf '%s\\n' \"watch: unsupported option: $1\" >&2; return 1;; "
        "*) break;; esac; done; "
        "if [[ $# -eq 0 ]]; then printf '%s\\n' 'watch: missing command' >&2; return 1; fi; "
        "while true; do clear; eval \"$*\"; sleep \"$__kimix_interval\"; done"
    ),
    "killall": (
        "if [[ $# -eq 0 ]]; then printf '%s\\n' 'killall: missing process name' >&2; return 1; fi; "
        "__KIMIX_NAME=$1 powershell.exe -NoProfile -NonInteractive -Command '" + _KILLALL_PS + "'"
    ),
    "pidof": (
        "if [[ $# -eq 0 ]]; then printf '%s\\n' 'pidof: missing process name' >&2; return 1; fi; "
        "__KIMIX_NAME=$1 powershell.exe -NoProfile -NonInteractive -Command '" + _PIDOF_PS + "'"
    ),
    "column": (
        "local __kimix_sep='DEFAULT'; "
        "while (( $# )); do case $1 in "
        "-t) shift;; "
        "-s) [[ $# -ge 2 ]] || { printf '%s\\n' \"column: option requires an argument -- 's'\" >&2; return 1; }; __kimix_sep=$2; shift 2;; "
        "-s?*) __kimix_sep=${1#-s}; shift;; "
        "-*) printf '%s\n' \"column: unsupported option for perl fallback: $1\" >&2; return 1;; "
        "*) break;; esac; done; "
        + _COLUMN_PERL
        + " \"$__kimix_sep\" \"$@\""
    ),
    # POSIX utilities from the common command cheat-sheet that are absent
    # from a bare Git Bash userland and map onto native Windows tools.
    "free": (
        "local __kimix_unit=K; "
        "while (( $# )); do case $1 in "
        "-b|--bytes) __kimix_unit=B; shift;; "
        "-k|--kibi|--kilo) __kimix_unit=K; shift;; "
        "-m|--mebi|--mega) __kimix_unit=M; shift;; "
        "-g|--gibi|--giga) __kimix_unit=G; shift;; "
        "-h|--human) __kimix_unit=H; shift;; "
        "--help) printf '%s\n' 'free: report memory usage (Windows: Win32_OperatingSystem; no swap row)'; return 0;; "
        "-*) printf '%s\n' \"free: unsupported option: $1\" >&2; return 1;; "
        "*) shift;; esac; done; "
        "local -a __kimix_m=(); "
        "mapfile -t __kimix_m < <(powershell.exe -NoProfile -NonInteractive -Command "
        "'$o=Get-CimInstance Win32_OperatingSystem; "
        "Write-Output $o.TotalVisibleMemorySize; Write-Output $o.FreePhysicalMemory' "
        "2>/dev/null | tr -d '\\r'); "
        "if [[ ${#__kimix_m[@]} -lt 2 ]]; then "
        "printf '%s\n' 'free: failed to query memory information' >&2; return 1; fi; "
        "local __kimix_total=${__kimix_m[0]} __kimix_free=${__kimix_m[1]}; "
        "local __kimix_used=$(( __kimix_total - __kimix_free )); "
        "printf '%s\n' '               total        used        free'; "
        "case $__kimix_unit in "
        "B) printf 'Mem: %12d %11d %11d\n' $(( __kimix_total * 1024 )) $(( __kimix_used * 1024 )) $(( __kimix_free * 1024 ));; "
        "M) printf 'Mem: %12d %11d %11d\n' $(( __kimix_total / 1024 )) $(( __kimix_used / 1024 )) $(( __kimix_free / 1024 ));; "
        "G) printf 'Mem: %12d %11d %11d\n' $(( __kimix_total / 1024 / 1024 )) $(( __kimix_used / 1024 / 1024 )) $(( __kimix_free / 1024 / 1024 ));; "
        "H) awk -v t=\"$__kimix_total\" -v f=\"$__kimix_free\" '"
        "function h(x,  i){i=1; while(x>=10240&&i<4){x=x/1024;i++} "
        "return sprintf(\"%.1f%s\", x, substr(\"KMGT\", i, 1))} "
        "BEGIN{printf \"Mem: %11s %10s %10s\\n\", h(t), h(t-f), h(f)}';; "
        "*) printf 'Mem: %12d %11d %11d\n' \"$__kimix_total\" \"$__kimix_used\" \"$__kimix_free\";; esac"
    ),
    "uptime": (
        "local __kimix_since=0; "
        "while (( $# )); do case $1 in "
        "-s|--since) __kimix_since=1; shift;; "
        "--help) printf '%s\n' 'uptime: tell how long the system has been running (Windows approximation; no user count)'; return 0;; "
        "-*) printf '%s\n' \"uptime: unsupported option: $1\" >&2; return 1;; "
        "*) printf '%s\n' \"uptime: unsupported argument: $1\" >&2; return 1;; esac; done; "
        "local -a __kimix_u=(); "
        "mapfile -t __kimix_u < <(powershell.exe -NoProfile -NonInteractive -Command "
        "'$b=(Get-CimInstance Win32_OperatingSystem).LastBootUpTime; "
        "Write-Output $b.ToString(\"yyyy-MM-dd HH:mm:ss\"); "
        "$up=New-TimeSpan -Start $b -End (Get-Date); "
        "Write-Output $up.Days; Write-Output $up.Hours; Write-Output $up.Minutes' "
        "2>/dev/null | tr -d '\\r'); "
        "if [[ ${#__kimix_u[@]} -lt 4 ]]; then "
        "printf '%s\n' 'uptime: failed to query boot time' >&2; return 1; fi; "
        "if (( __kimix_since )); then printf '%s\n' \"${__kimix_u[0]}\"; return 0; fi; "
        "local __kimix_days=${__kimix_u[1]} __kimix_hours=${__kimix_u[2]} __kimix_mins=${__kimix_u[3]}; "
        "printf -v __kimix_mins '%02d' \"$__kimix_mins\"; "
        "local __kimix_up; "
        "if (( __kimix_days > 0 )); then "
        "printf -v __kimix_up '%d day(s), %d:%s' \"$__kimix_days\" \"$__kimix_hours\" \"$__kimix_mins\"; "
        "else printf -v __kimix_up '%d:%s' \"$__kimix_hours\" \"$__kimix_mins\"; fi; "
        "printf '%s up %s, load average: n/a (not reported on Windows)\n' \"$(date '+%H:%M:%S')\" \"$__kimix_up\""
    ),
    "top": (
        "local __kimix_delay=3 __kimix_iters=0 __kimix_batch=0 __kimix_count=0; "
        "while (( $# )); do case $1 in "
        "-b) __kimix_batch=1; shift;; "
        "-d) [[ $# -ge 2 ]] || { printf '%s\\n' \"top: option requires an argument -- 'd'\" >&2; return 1; }; __kimix_delay=$2; shift 2;; "
        "-d?*) __kimix_delay=${1#-d}; shift;; "
        "-n) [[ $# -ge 2 ]] || { printf '%s\\n' \"top: option requires an argument -- 'n'\" >&2; return 1; }; __kimix_iters=$2; shift 2;; "
        "-n?*) __kimix_iters=${1#-n}; shift;; "
        "-h|--help) printf '%s\n' 'top: display processes (Windows approximation via Get-Process; Ctrl+C quits)'; return 0;; "
        "-*) printf '%s\n' \"${FUNCNAME[0]}: unsupported option: $1\" >&2; return 1;; "
        "*) shift;; esac; done; "
        "__kimix_snapshot() { powershell.exe -NoProfile -NonInteractive -Command "
        "'Get-Process | Sort-Object -Property CPU -Descending | "
        "Select-Object -First 25 Id,ProcessName,CPU,WorkingSet | Format-Table -AutoSize'; }; "
        "if (( __kimix_batch )); then "
        "local __kimix_n=$__kimix_iters; (( __kimix_n > 0 )) || __kimix_n=1; "
        "while (( __kimix_count < __kimix_n )); do "
        "__kimix_snapshot; __kimix_count=$(( __kimix_count + 1 )); done; return 0; fi; "
        "while (( __kimix_iters == 0 || __kimix_count < __kimix_iters )); do "
        "clear; __kimix_snapshot; __kimix_count=$(( __kimix_count + 1 )); "
        "(( __kimix_iters > 0 && __kimix_count >= __kimix_iters )) && break; "
        "sleep \"$__kimix_delay\"; done"
    ),
    "ss": (
        "local __kimix_stats=0; "
        "local -a __kimix_split=(); "
        "local __kimix_combo='' __kimix_i=0; "
        "while (( $# )); do "
        "if [[ $1 == -[!-]* && ${#1} -gt 2 ]]; then "
        "__kimix_combo=${1#-}; __kimix_split=(); shift; "
        "for (( __kimix_i=0; __kimix_i<${#__kimix_combo}; __kimix_i++ )); do "
        "__kimix_split+=(-${__kimix_combo:__kimix_i:1}); done; "
        "set -- \"${__kimix_split[@]}\" \"$@\"; continue; fi; "
        "case $1 in "
        "-s|--summary) __kimix_stats=1; shift;; "
        "-t|-u|-l|-n|-a|-p|-e|-m|-r|-i|-x|-4|-6|-T|--tcp|--udp|--listening|--numeric|--all|--process|--extended|--memory|--resolve|--inet|--inet4|--inet6) shift;; "
        "--*) shift;; "
        "-*) printf '%s\n' \"ss: unsupported option: $1\" >&2; return 1;; "
        "*) shift;; esac; done; "
        "if (( __kimix_stats )); then netstat -s; else netstat -ano; fi"
    ),
    "ip": (
        "local __kimix_sub=''; "
        "while (( $# )); do case $1 in "
        "-4|-6|-br|--brief|-details|-s|-human|-iec|-o|-oneline|-c|--color|-color) shift;; "
        "-h|--help) __kimix_sub=help; shift;; "
        "-*) printf '%s\n' \"ip: unsupported option: $1\" >&2; return 1;; "
        "*) if [[ -z $__kimix_sub ]]; then __kimix_sub=$1; fi; shift;; esac; done; "
        "case $__kimix_sub in "
        "help) printf '%s\n' 'Usage: ip [addr|link|route|neigh] (Windows equivalents: ipconfig / Get-NetAdapter / route print / arp -a)'; return 0;; "
        "'') printf '%s\n' 'Usage: ip [addr|link|route|neigh] (Windows equivalents: ipconfig / Get-NetAdapter / route print / arp -a)' >&2; return 1;; "
        "addr|address|a) ipconfig;; "
        "link|l) powershell.exe -NoProfile -NonInteractive -Command "
        "'Get-NetAdapter | ForEach-Object { \"$($_.Name) $($_.Status) $($_.LinkSpeed) $($_.MacAddress)\" }';; "
        "route|r) route print;; "
        "neigh|n) arp -a;; "
        "*) printf '%s\n' \"ip: unsupported object: $__kimix_sub (supported: addr link route neigh)\" >&2; return 1;; esac"
    ),
    "man": (
        "local __kimix_rc=0 __kimix_seen=0; "
        "while (( $# )); do case $1 in "
        "--help) printf '%s\n' 'man: show command help (fallback prints <command> --help)'; return 0;; "
        "--) shift; break;; "
        "-*) printf '%s\n' \"man: unsupported option for --help fallback: $1\" >&2; return 1;; "
        "*) break;; esac; done; "
        "while (( $# )); do "
        "if [[ $1 =~ ^[0-9]+$ ]]; then shift; continue; fi; "
        "__kimix_seen=1; \"$1\" --help || __kimix_rc=1; shift; done; "
        "if (( ! __kimix_seen )); then printf '%s\n' 'man: missing command name' >&2; return 1; fi; "
        "return $__kimix_rc"
    ),
    "systemctl": (
        "local __kimix_cmd=''; "
        "local -a __kimix_names=(); "
        "while (( $# )); do case $1 in "
        "-q|--quiet|--no-pager|--plain|--full|-l|--no-legend|--no-ask-password|--user|--system|--global) shift;; "
        "--type=*) shift;; "
        "--) shift; break;; "
        "-*) printf '%s\n' \"systemctl: unsupported option: $1\" >&2; return 1;; "
        "*) if [[ -z $__kimix_cmd ]]; then __kimix_cmd=$1; else __kimix_names+=(\"$1\"); fi; shift;; esac; done; "
        "if [[ -z $__kimix_cmd ]]; then "
        "printf '%s\n' 'systemctl: missing subcommand (Windows service equivalents; supported: status start stop restart reload enable disable is-active is-enabled list-units list-unit-files)' >&2; return 1; fi; "
        "case $__kimix_cmd in "
        "status) (( ${#__kimix_names[@]} )) || { printf '%s\n' 'systemctl: missing unit name' >&2; return 1; }; "
        "local __kimix_csv=$(IFS=,; echo \"${__kimix_names[*]}\"); "
        "powershell.exe -NoProfile -NonInteractive -Command "
        "\"Get-Service -Name '$__kimix_csv' | Format-List Name,DisplayName,Status,StartType\";; "
        "start|stop|restart|reload) (( ${#__kimix_names[@]} )) || { printf '%s\n' 'systemctl: missing unit name' >&2; return 1; }; "
        "local __kimix_ps='' __kimix_n; "
        "for __kimix_n in \"${__kimix_names[@]}\"; do "
        "if [[ $__kimix_cmd == stop || $__kimix_cmd == restart || $__kimix_cmd == reload ]]; then "
        "__kimix_ps+=\"Stop-Service -Name '$__kimix_n' -ErrorAction Stop; \"; fi; "
        "if [[ $__kimix_cmd == start || $__kimix_cmd == restart || $__kimix_cmd == reload ]]; then "
        "__kimix_ps+=\"Start-Service -Name '$__kimix_n' -ErrorAction Stop; \"; fi; done; "
        "powershell.exe -NoProfile -NonInteractive -Command \"$__kimix_ps\";; "
        "enable|disable) (( ${#__kimix_names[@]} )) || { printf '%s\n' 'systemctl: missing unit name' >&2; return 1; }; "
        "local __kimix_start=demand; [[ $__kimix_cmd == enable ]] && __kimix_start=auto; "
        "local __kimix_rc=0 __kimix_n; "
        "for __kimix_n in \"${__kimix_names[@]}\"; do "
        "sc.exe config \"$__kimix_n\" start= \"$__kimix_start\" >/dev/null || __kimix_rc=1; done; "
        "return $__kimix_rc;; "
        "is-active) (( ${#__kimix_names[@]} )) || { printf '%s\n' 'systemctl: missing unit name' >&2; return 1; }; "
        "local __kimix_rc=0 __kimix_n; "
        "for __kimix_n in \"${__kimix_names[@]}\"; do "
        "if sc.exe query \"$__kimix_n\" 2>/dev/null | grep -q 'RUNNING'; then "
        "printf '%s\n' active; else printf '%s\n' inactive; __kimix_rc=3; fi; done; "
        "return $__kimix_rc;; "
        "is-enabled) (( ${#__kimix_names[@]} )) || { printf '%s\n' 'systemctl: missing unit name' >&2; return 1; }; "
        "local __kimix_rc=0 __kimix_n; "
        "for __kimix_n in \"${__kimix_names[@]}\"; do "
        "if sc.exe qc \"$__kimix_n\" 2>/dev/null | grep -q 'DISABLED'; then "
        "printf '%s\n' disabled; __kimix_rc=1; else printf '%s\n' enabled; fi; done; "
        "return $__kimix_rc;; "
        "list-units|list-unit-files) "
        "powershell.exe -NoProfile -NonInteractive -Command "
        "'Get-Service | ForEach-Object { \"$($_.Status) $($_.Name) $($_.DisplayName)\" }';; "
        "*) printf '%s\n' \"systemctl: unsupported subcommand: $__kimix_cmd (supported: status start stop restart reload enable disable is-active is-enabled list-units list-unit-files)\" >&2; return 1;; esac"
    ),
    # Windows 11 ships sudo.exe in system32; older Windows releases have no
    # sudo at all, so the guard below only defines this on those hosts.  The
    # body elevates through a UAC prompt: output is NOT captured (it shows in
    # the elevated window), which is an honest, documented deviation.
    "sudo": (
        "if (( $# == 0 )); then printf '%s\n' 'sudo: missing command' >&2; return 1; fi; "
        "local __kimix_bash; __kimix_bash=$(type -P bash) || { printf '%s\n' 'sudo: bash not found' >&2; return 1; }; "
        "local __kimix_wbash; __kimix_wbash=$(cygpath -w -- \"$__kimix_bash\") || return 1; "
        "printf '%s\n' 'sudo: elevating via UAC in a separate window; output is not captured here' >&2; "
        "__KIMIX_SUDO_CMD=\"$*\" powershell.exe -NoProfile -NonInteractive -Command "
        "\"Start-Process -Verb RunAs -Wait -FilePath '$__kimix_wbash' -ArgumentList '-c', \\$env:__KIMIX_SUDO_CMD\"; "
        "local __kimix_rc=$?; return $__kimix_rc"
    ),
}


# GNU ``g``-prefixed command names (the Homebrew coreutils spelling used on
# macOS) map to the very same GNU tools that Git Bash already ships, so the
# mapping is faithful by construction.  ``gtimeout`` is spelled out above.
for _gnu_command in (
    "awk", "cat", "comm", "cp", "cut", "date", "df", "du", "egrep",
    "fgrep", "find", "grep", "head", "join", "ln", "ls", "make", "mkdir",
    "mv", "paste", "readlink", "realpath", "rm", "rmdir", "sed", "seq",
    "shuf", "sort", "split", "stat", "tail", "tar", "tr", "uniq", "wc",
    "xargs",
):
    _FALLBACK_BODIES.setdefault("g" + _gnu_command, f'{_gnu_command} "$@"')


def _fallback_definition(name: str) -> str:
    body = _FALLBACK_BODIES[name]
    if name in _STUB_AWARE_FALLBACKS:
        # The Microsoft Store App Execution Alias satisfies ``command -v``
        # but is not a working interpreter: define the fallback anyway, and
        # never delegate to the stub path.
        guard = (
            f"if ! command -v {name} >/dev/null 2>&1 "
            f"|| [[ $(type -P {name}) == *WindowsApps* ]]; then "
        )
        delegate = (
            f"local __kimix_native=''; __kimix_native=$(type -P {name}) || :; "
            f"if [[ -n $__kimix_native && $__kimix_native != *WindowsApps* ]]; then "
            f"\"$__kimix_native\" \"$@\"; return; fi; "
        )
    else:
        guard = f"if ! command -v {name} >/dev/null 2>&1; then "
        delegate = _NATIVE_DELEGATE.format(name=name)
    return f"{guard}{name}() {{ {delegate}{body}; }}; fi"


def _single_quote(command: str) -> str:
    """Quote *command* as one literal Bash word."""
    return "'" + command.replace("'", "'\"'\"'") + "'"


def _wrapper_runner(name: str) -> str:
    """Return an executable command for wrappers that cannot invoke functions."""
    script = _fallback_definition(name) + f"; {name} \"$@\""
    return "/usr/bin/bash -c " + _single_quote(script) + " --"


# ``netcat`` is a common synonym for ``nc`` on systems where the binary is
# spelled with the longer name; both are absent from Git Bash, so share the
# same ``/dev/tcp`` zero-I/O fallback.
_FALLBACK_BODIES.setdefault("netcat", _FALLBACK_BODIES["nc"])

# ``htop`` is the fancy TUI spelling of the same live-process view; the
# Windows approximation is identical to ``top``'s.
_FALLBACK_BODIES.setdefault("htop", _FALLBACK_BODIES["top"])

_FALLBACKS = {name: _fallback_definition(name) for name in _FALLBACK_BODIES}

# Commands with no faithful Windows Git Bash equivalent at all.  The scanner
# records these in ``BashFix.unsupported`` (command text is left untouched)
# so the app layer can refuse to run the command and return the reason in its
# error message instead of letting Bash fail with a bare "command not found".
_UNSUPPORTED_BODIES = {
    "journalctl": (
        "systemd's journal does not exist on Windows and Git Bash has no "
        "journal daemon, so there is no faithful equivalent. Read the "
        "application's own log file(s), or query the Windows Event Log "
        "instead: powershell.exe Get-WinEvent -LogName Application -MaxEvents 50"
    ),
}

_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\+)?=")
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_COMMAND_START_KEYWORDS = frozenset(
    {"!", "{", "if", "then", "elif", "else", "while", "until", "do"}
)
_COMMAND_END_KEYWORDS = frozenset({"fi", "done", "esac"})
_LIST_KEYWORDS = frozenset({"for", "select", "case"})
_SHELL_KEYWORDS = frozenset(
    {
        "!", "{", "}", "if", "then", "elif", "else", "while", "until",
        "do", "fi", "done", "esac", "for", "select", "case", "time",
        "coproc", "in", "function",
    }
)

_COMMAND_WRAPPERS = frozenset(
    {"builtin", "command", "coproc", "env", "exec", "nohup", "sudo", "time",
     "timeout", "stdbuf", "nice", "xargs"}
)

# Fallback names that behave like command wrappers too: the word itself needs
# its fallback definition recorded AND its first non-option operand is a
# command the wrapper executes.  ``gtimeout`` runs ``timeout "$@"`` (an
# executable that execs argv, so its command operand needs the standalone
# runner), while ``watch`` runs its command inside the same shell (its body
# uses ``eval "$*"``, so a same-shell function call suffices).  ``sudo`` maps
# to its own wrapper semantics (option table and executable operand); the
# fallback definition only engages on hosts without ``sudo.exe`` (pre-Windows
# 11), where it elevates through a UAC prompt.
_FALLBACK_COMMAND_WRAPPERS = {
    "gtimeout": "timeout",
    "watch": "watch",
    "sudo": "sudo",
}

# Wrappers that require a fixed number of plain operands (options excluded)
# before the command word.  GNU ``timeout`` takes exactly one DURATION operand
# (``timeout 5 rev``, ``timeout -s KILL 5s rev``) before COMMAND, so the
# duration is consumed without being mistaken for the wrapped command.
_WRAPPER_OPERAND_COUNTS = {"timeout": 1}

# Wrapper kinds whose command operand runs inside the current shell rather
# than being exec'd as a new process: the fallback function defined by the
# prefix is directly callable, so the source word is kept (no standalone
# runner rewrite is needed or possible).
_SAME_SHELL_WRAPPERS = frozenset({"builtin", "coproc", "time", "watch"})
# Builtins whose *operand words* are re-parsed as shell code at runtime, so a
# quoted operand is an inline script: ``eval`` concatenates and re-parses all
# of its operands, ``trap`` stores its first operand and re-parses it when the
# signal fires (the rest are signal specs — data).  Value -1 means "every
# operand"; a positive value counts the leading operands that are scripts.
# Scanning the operand's literal value as its own command context means the
# null-device rewrite (``> nul``) and the other compatibility fixes reach the
# code that actually runs.
_REPARSE_BUILTINS = {"eval": -1, "trap": 1}
# Command words that pass part of their argv to a *child process* for
# execution (``find -exec``, ``xargs``), or that re-parse their operands
# (``eval``).  Only for these is an argument-position shell word
# (``... sh -c '<script>'``) a real inline script whose redirections run; for
# any other command (``echo sh -c 'x > nul'``) the words are data and must
# stay untouched.
_EXEC_OPERAND_COMMANDS = frozenset({"find", "xargs", "eval", "ssh", "watch"})
# Commands whose leading operand is PATTERN/PROGRAM DATA, not a path: an
# unquoted drive-prefixed pattern (``grep -F C:\Users file``) has the bash
# value ``C:Users`` and genuinely matches; rewriting it "as a path" to
# ``C:/Users`` silently breaks the match.  The first bare operand (and the
# value of pattern-taking options like ``grep -e``) therefore suppresses the
# Windows-path rewrite; file-taking options (``grep -f``) and later operands
# keep it.
_PATTERN_PROGRAM_COMMANDS = frozenset(
    {"grep", "fgrep", "egrep", "rg", "sed", "awk", "gawk", "mawk"}
)
_GREP_PATTERN_FAMILY = frozenset({"grep", "fgrep", "egrep", "rg"})
# grep-family options whose value word is an arbitrary string (context
# lengths, device actions, ...): neither pattern nor path, but the value
# word must not be counted as the pattern operand.
_GREP_VALUE_OPTIONS = frozenset(
    {
        "-A",
        "-B",
        "-C",
        "-m",
        "-d",
        "-D",
        "--after-context",
        "--before-context",
        "--context",
        "--max-count",
        "--directories",
        "--devices",
    }
)

# Shell executables that agents frequently put in front of a command the Bash
# tool is already going to run (``bash cd /c/dev/x && ...``).  Git Bash can
# stand in for these POSIX-family shells, so a redundant leading invocation is
# unwrapped instead of letting bash try to open ``cd`` as a script file.  Only
# shells whose syntax is a subset of bash are listed; ``zsh``/``ksh``/``csh``
# scripts may use incompatible syntax and are deliberately left untouched.
_SHELL_WRAPPERS = frozenset({"bash", "sh", "dash", "ash"})

# Short-option clusters of the shell wrappers that take the next argument as
# an inline command string.  ``bash -c '...'`` is the canonical form;
# ``-lc``/``-cl`` add the (harmless here) login flag.  Clusters such as
# ``-ec``/``-ce``/``-xc`` that change errexit/xtrace semantics are left for
# bash to handle so unwrapping cannot alter the script's meaning.
_SHELL_C_OPTIONS = re.compile(r"^-c$|^-lc$|^-cl$")
# Option clusters whose *last* flag is ``c`` (``-c``, ``-lc``, ``-ec``,
# ``-ilc``): the next word is the shell's inline script.
_SHELL_C_SCRIPT_OPTION_RE = re.compile(r"^-[A-Za-z]*c$")
# Shell options that consume the NEXT word as their value (``bash -o pipefail
# -c '...'``, ``sh --rcfile /dev/null -c '...'``): the value is a plain
# operand, not a script, so the scan must skip it instead of bailing out.
# ``+o``/``+O`` do not start with ``-`` and are listed explicitly.
_SHELL_VALUE_OPTIONS = frozenset(
    {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}
)
_WRAPPER_OPTIONS_WITH_VALUE = {
    "env": frozenset(
        {
            "-u",
            "--unset",
            "-C",
            "--chdir",
            "-S",
            "--split-string",
        }
    ),
    "exec": frozenset({"-a"}),
    "sudo": frozenset(
        {
            "-C",
            "--close-from",
            "-D",
            "--chdir",
            "-g",
            "--group",
            "-h",
            "--host",
            "-p",
            "--prompt",
            "-R",
            "--chroot",
            "-r",
            "--role",
            "-t",
            "--type",
            "-T",
            "--command-timeout",
            "-u",
            "--user",
        }
    ),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
    "timeout": frozenset(
        {
            "-k",
            "--kill-after",
            "-s",
            "--signal",
        }
    ),
    "stdbuf": frozenset(
        {
            "-o",
            "-e",
            "-i",
            "--output",
            "--error",
            "--input",
        }
    ),
    "nice": frozenset({"-n", "--adjustment"}),
    "xargs": frozenset(
        {
            "-I",
            "-n",
            "-L",
            "-P",
            "-s",
            "-S",
            "-a",
            "-d",
            "-E",
            "--arg-file",
            "--max-args",
            "--max-chars",
            "--max-procs",
            "--max-lines",
            "--replace",
            "--eof",
            "--delimiter",
        }
    ),
    "watch": frozenset({"-n", "--interval"}),
}

# Wrapper options whose value is a filesystem path rather than a name or
# number.  Windows backslash spellings of these values are rewritten for Git
# Bash just like ordinary argument paths; every other option value stays
# opaque.
_WRAPPER_PATH_OPTIONS = {
    "env": frozenset({"-C", "--chdir"}),
    "sudo": frozenset({"-D", "--chdir"}),
    "time": frozenset({"-o", "--output"}),
    "xargs": frozenset({"-a", "--arg-file"}),
}
_WRAPPER_PATH_OPTION_LONG = frozenset({"--chdir", "--output", "--arg-file"})

_OPERATOR_CHARS = frozenset(";&|()<>\n")
_REDIRECTION_START = frozenset("<>")

# Characters that terminate an unquoted word: operators plus horizontal
# whitespace.  A single frozenset membership test is cheaper than two.
_WORD_END_CHARS = _OPERATOR_CHARS | frozenset(" \t\r")

# Character classes for the C-accelerated skip loops.  ``str.translate`` with
# ``None`` mappings deletes the listed characters, turning a scan for "any
# special char present?" into a single C-level pass; the compiled regexes
# bulk-skip runs of ordinary characters inside quote-aware loops.
_LITERAL_SPECIAL_RE = re.compile(r'[\\\'"$`*?[{~]')
"""Chars whose presence forces the full quote-removal walk."""
_DQ_SPECIAL_RE = re.compile(r'[\\"`$]')
"""Chars that are special inside bash double quotes."""
_WORD_SPECIAL_RE = re.compile(r'[;&|()<>\n \t\r#\\\'"`$]')
"""Chars that can end a word or open a quoted/expanded region in ``_read_word``."""
_EXPANSION_SPECIAL_RE = re.compile(r'[\\\'"`$]')
"""Chars that matter while scanning a data region for substitutions."""
_HEREDOC_EXPANSION_SPECIAL_RE = re.compile(r'[\\`$]')
"""Chars that matter inside an expanding heredoc body (quotes are literal)."""
_CONDITIONAL_SPECIAL_RE = re.compile(r'[\\\]\'"`$]')
"""Chars that matter inside a ``[[ ... ]]`` conditional."""
_PARAMETER_SPECIAL_RE = re.compile(r'[\\$\'"{}]')
"""Chars that matter inside a ``${...}`` parameter expansion."""
_PARAMETER_LITERAL_SPECIAL_RE = re.compile(r'[\\\'"{}]')
"""Chars that matter inside a literal ``${...}`` (no substitution scan)."""
_ARITHMETIC_SPECIAL_RE = re.compile(r'[\\()\'"$]')
"""Chars that matter inside a ``$((...))`` arithmetic expansion."""
_BACKTICK_SPECIAL_RE = re.compile(r'[\\`]')
"""Chars that matter while finding a backtick region's end."""
_HSPACE_RE = re.compile(r'[ \t\r]+')
"""Horizontal whitespace run (skipped in one C-level match)."""
_CONTROL_OP_STARTS = frozenset(";&|()")
"""Characters a bash control operator can start with."""
_NAME_START_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
)
"""Characters a POSIX shell name can start with."""
_REDIRECTION_RE = re.compile(r"&>>|&>|<<<|<<-|<<|>>|<>|>\||<&|>&|<|>")
"""One C-level match against the longest-first redirection operator table."""

_UNDECODED = object()
"""Sentinel: "the caller did not pre-decode this word's literal value"."""

# Nesting deeper than this is never scanned: ``_find_matching`` and
# ``_scan_range`` recurse once per ``$( ... )`` level, so pathological input
# (e.g. a pasted blob with hundreds of nested substitutions) would otherwise
# cost O(depth * length).  Real commands stay far below this bound; content
# beyond it is simply left byte-for-byte for Bash to handle.
_MAX_NESTING_DEPTH = 1024

# ── Windows path recognition ────────────────────────────────────────────────
# Rewrites apply only to unquoted words that unambiguously look like Windows
# paths; everything else (quotes, expansions, short ambiguous words) is left
# byte-for-byte for Bash to handle.

_PATH_DRIVE_RE = re.compile(r"[A-Za-z]:[\\/].*")
"""Drive-absolute path such as ``D:\\foo`` or the drive root ``C:\\``."""

_PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9_.~\\-]+")
"""Decoded value of a plausible multi-segment relative path (no spaces)."""

_PATH_SAFE_CHARS = frozenset(
 "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./:~@%+=-#,[]*?{}"
)
"""Characters that may appear unquoted in a normalized path word.

Glob metacharacters are included on purpose so ``D:/x/*.txt`` still performs
pathname expansion instead of being quoted into a literal name; brace
metacharacters too so ``/tmp/{a,b}`` still performs brace expansion.
"""

_PATH_GLOB_CHARS = frozenset("*?[]{}")
"""Expansion metacharacters that must stay OUTSIDE any quoting the path
rewrite adds.

A glob or brace metacharacter wrapped inside double quotes becomes a
literal filename character, silently destroying the pathname/brace
expansion the original unquoted word performed (``D:/x/*.txt`` must keep
expanding).  Quoting therefore breaks its span at these characters and
emits them verbatim.
"""

_ESCAPED_LITERAL_CHARS = frozenset(" \t&;|()<>'\"$`{}!")
"""Chars whose backslash form is a pure Bash escape, not a path separator.

``\\ `` (escaped space) is how a space inside an unquoted word is written;
normalizing it to ``/ `` would invent a directory level that does not exist.
The backslash is dropped and the character kept inside its segment.
"""


def _windows_temp_dir() -> str:
    """Return the real Windows temp directory as a forward-slash path.

    Git Bash's ``/tmp`` maps to this directory (``cygpath -w /tmp`` reports
    the same value), but Git Bash's ``TMP``/``TEMP`` environment variables are
    MSYS-style ``/tmp`` inside the shell, so they cannot be used to translate
    ``/tmp`` paths for native Windows executables.  The parent (kimi-agent)
    process environment holds the Windows-style spelling, which is what
    ``tempfile.gettempdir()`` returns.  On POSIX hosts this returns ``/tmp``
    (the identity for the rewrite), which keeps the scanner's platform gate
    the single source of truth for when the rewrite applies.
    """
    return tempfile.gettempdir().replace("\\", "/")


_VAR_NAME_START_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
)
"""Characters that may start a ``$name`` variable expansion."""

_VAR_NAME_CHARS = _VAR_NAME_START_CHARS | frozenset("0123456789")
"""Characters that may continue a ``$name`` variable expansion."""

_SPECIAL_PARAM_CHARS = frozenset("0123456789?-$!#*@")
"""Single-character special parameters (``$$``, ``$?``, ``$@``, ...)."""


def _match_ansi_c_quote_span(text: str, i: int) -> int | None:
    """Return the index after the ``'`` closing a ``$'`` region opened before *i*.

    Returns None when the region is unterminated.
    """
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
        elif ch == "'":
            return i + 1
        else:
            i += 1
    return None


def _match_dquote_span(text: str, i: int) -> int | None:
    """Return the index after the ``"`` closing a double-quoted region opened before *i*.

    Returns None when the region is unterminated.
    """
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
        elif ch == '"':
            return i + 1
        else:
            i += 1
    return None


def _match_parameter_braces(text: str, i: int) -> int | None:
    """Return the index after the ``}`` closing ``${`` (opened at ``i - 2``).

    Returns None when the expansion is unterminated or embeds a command
    substitution (``$(...)`` or backquote): those inner command contexts are
    scanned separately, and rewriting a word that contains them would clash
    with the edits recorded there.
    """
    depth = 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
        elif ch == "'":
            j = text.find("'", i + 1)
            if j < 0:
                return None
            i = j + 1
        elif ch == '"':
            j = _match_dquote_span(text, i + 1)
            if j is None:
                return None
            i = j
        elif ch == "`" or (ch == "$" and i + 1 < n and text[i + 1] == "("):
            return None
        elif ch == "{":
            depth += 1
            i += 1
        elif ch == "}":
            depth -= 1
            i += 1
            if depth == 0:
                return i
        else:
            i += 1
    return None


def _match_arithmetic_span(text: str, i: int) -> int | None:
    """Return the index after the ``))`` closing ``$((`` opened at ``i - 3``.

    Returns None when the expansion is unterminated or embeds a command
    substitution (``$(...)``), whose inner command context is scanned
    separately.
    """
    depth = 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "$" and i + 1 < n and text[i + 1] == "(":
            return None
        if ch == "(" and text.startswith("((", i):
            depth += 1
            i += 2
        elif ch == ")" and text.startswith("))", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
        elif ch == "\\":
            i += 2
        elif ch == "'":
            j = text.find("'", i + 1)
            if j < 0:
                return None
            i = j + 1
        elif ch == '"':
            j = _match_dquote_span(text, i + 1)
            if j is None:
                return None
            i = j
        else:
            i += 1
    return None


def _decode_virtual_path_span(span: str) -> str | None:
    """Decode Bash unquoted-word backslash escapes in a literal path span.

    In the original unquoted word ``\\x`` decodes to the literal character
    ``x`` and ``\\`` + newline is a line continuation that joins the value.
    The virtual-path rewrite re-quotes the word, so the span must be decoded
    first — otherwise the re-emitted ``\\`` leaks into the value (``a\\ b``
    was rewritten to a value of ``a\\ b`` instead of ``a b``).

    Returns None when the span must stay untouched: an escaped glob
    metacharacter (``\\*``, ``\\?``, ``\\[`` — re-emitting it unquoted
    would re-enable pathname expansion the escape suppressed) or a trailing
    lone backslash (an unterminated line continuation Bash itself will
    diagnose).
    """
    out: list[str] = []
    i = 0
    n = len(span)
    while i < n:
        ch = span[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            return None
        nxt = span[i + 1]
        if nxt == "\n":
            i += 2  # line continuation: both characters drop from the value
            continue
        if nxt in "*?[{}":
            return None
        out.append(nxt)
        i += 2
    return "".join(out)


def _split_virtual_path_expansions(text: str) -> list[tuple[bool, str]] | None:
    """Split a rewritten virtual path into literal and expansion spans.

    Returns a list of ``(is_expansion, span)`` runs in source order.
    Expansion spans (``$var``, ``${...}``, ``$((...))``, ``$'...'``, special
    parameters) are copied verbatim by the caller so Bash's runtime expansion
    keeps working; literal spans between them are re-quoted only when they
    need it.  A ``$`` that starts no expansion is literal text and stays in a
    literal span (the re-quoting escapes it, preserving the value).

    Returns None when the word embeds a command substitution (``$(...)``,
    backquote, ``$"..."``), an unterminated expansion, or an unescaped
    quote character (a quoted region inside the word): the substitution's
    inner command context is scanned separately, and quoted segments cannot
    be re-emitted faithfully, so the whole word must be left untouched to
    keep the original word value.
    """
    spans: list[tuple[bool, str]] = []
    literal_start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            i += 2  # an escaped char stays in the literal span
            continue
        if ch in "'\"`":
            # A quote char inside the raw word opens a quoted region whose
            # quote removal is part of the word's value; the rewriter
            # cannot re-emit that faithfully, so the word stays untouched.
            return None
        if ch != "$":
            i += 1
            continue
        nxt = text[i + 1] if i + 1 < n else ""
        if nxt == "(":
            if text.startswith("$((", i):
                end = _match_arithmetic_span(text, i + 3)
                if end is None:
                    return None
            else:
                return None
        elif nxt == '"':
            return None
        elif nxt == "{":
            end = _match_parameter_braces(text, i + 2)
            if end is None:
                return None
        elif nxt == "'":
            end = _match_ansi_c_quote_span(text, i + 2)
            if end is None:
                return None
        elif nxt in _VAR_NAME_START_CHARS:
            end = i + 2
            while end < n and text[end] in _VAR_NAME_CHARS:
                end += 1
        elif nxt in _SPECIAL_PARAM_CHARS:
            end = i + 2
        else:
            i += 1  # a ``$`` starting no expansion is a literal dollar sign
            continue
        if literal_start < i:
            spans.append((False, text[literal_start:i]))
        spans.append((True, text[i:end]))
        i = end
        literal_start = i
    if literal_start < n:
        spans.append((False, text[literal_start:]))
    return spans


def _opaque_inline_expansions(text: str) -> tuple[str, list[str]]:
    """Copy inline-script text with every runtime expansion made opaque.

    Returns ``(sanitized, substitutions)``: *sanitized* parses as the same
    command structure (the same command words in the same positions) with
    each ``$...``/backquote expansion replaced by a placeholder word, so it
    can be scanned as a command context without executing or misreading the
    expansion values; *substitutions* holds the raw body of every ``$(...)``
    or backquote region (which run in the current shell at command time) for
    the caller to scan separately as its own command context.

    Single-quoted regions are copied verbatim (``$`` inside them is literal
    at runtime); escaped characters are kept verbatim; a ``$`` starting no
    expansion is a literal dollar sign.  Unterminated expansions are copied
    verbatim so the sanitized text stays a faithful prefix of the script.
    """
    placeholder = "__kimix_operand__"
    n = len(text)
    out: list[str] = []
    substitutions: list[str] = []
    i = 0
    in_squote = False
    in_dquote = False
    while i < n:
        ch = text[i]
        if in_squote:
            out.append(ch)
            if ch == "'":
                in_squote = False
            i += 1
            continue
        if ch == "'":
            in_squote = True
            out.append(ch)
            i += 1
            continue
        if ch == "\\":
            # An escaped char is literal script text; keep it verbatim.
            out.append(ch)
            if i + 1 < n:
                out.append(text[i + 1])
                i += 2
            else:
                i += 1
            continue
        if ch == '"':
            in_dquote = not in_dquote
            out.append(ch)
            i += 1
            continue
        if ch == "`":
            end = _find_backtick_end(text, i + 1)
            if end < 0:
                out.append(text[i:])
                break
            substitutions.append(text[i + 1 : end - 1])
            out.append(placeholder)
            i = end
            continue
        if ch != "$" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1]
        if nxt == "(":
            if text.startswith("$((", i):
                end = _match_arithmetic_span(text, i + 3)
                if end is None:
                    out.append(text[i:])
                    break
            else:
                close = _find_matching_paren(text, i + 1)
                if close < 0:
                    out.append(text[i:])
                    break
                substitutions.append(text[i + 2 : close])
                end = close + 1
            out.append(placeholder)
            i = end
            continue
        if nxt == "{":
            end = _match_parameter_braces(text, i + 2)
            if end is None:
                out.append(text[i:])
                break
            out.append(placeholder)
            i = end
            continue
        if nxt == "'":
            end = _find_ansi_c_end(text, i + 2)
            if end < 0:
                out.append(text[i:])
                break
            out.append(placeholder)
            i = end
            continue
        if nxt == '"':
            end = _find_dq_end(text, i + 2)
            if end < 0:
                out.append(text[i:])
                break
            out.append(placeholder)
            i = end
            continue
        if nxt in _VAR_NAME_START_CHARS:
            j = i + 2
            while j < n and text[j] in _VAR_NAME_CHARS:
                j += 1
            out.append(placeholder)
            i = j
            continue
        if nxt in _SPECIAL_PARAM_CHARS:
            out.append(placeholder)
            i += 2
            continue
        # A ``$`` starting no expansion is a literal dollar sign.
        out.append(ch)
        i += 1
    return "".join(out), substitutions


@dataclass(frozen=True)
class BashFix:
    """Result of :func:`fix_bash_command`.

    ``replacements`` records each original command name in source order and
    ``path_changes`` each original argument or command word whose
    Windows-style backslashes, Git Bash virtual absolute path (``/tmp/x``,
    ``/c/x``), or cmd.exe ``/d`` flag were rewritten for Git Bash.
    ``shell_wrappers`` records each redundant ``bash``/``sh``
    invocation that was unwrapped (``bash <cmd> ...`` or ``bash -c <script>``)
    so the command runs directly in the Bash tool.  ``nul_fixes`` records each
    redirection target whose literal value is the bare word ``nul``/``NUL``
    (plain, backslash-escaped ``\\nul``/``n\\ul``, ANSI-C ``$'nul'``, or
    empty-quote-concatenated ``nul''``) that was rewritten to ``/dev/null`` —
    including inside runtime-reparsed strings (``eval``/``trap`` operands,
    ``watch`` and nested ``sh -c`` scripts) — because Git Bash treats ``nul``
    as an ordinary filename, silently creating an empty ``nul`` file instead
    of discarding output.  A whole-token quoted ``'nul'``/``"nul"`` target
    stays untouched (documented intentional-filename policy).
    ``unsupported`` records each command name that has no faithful Windows
    Git Bash equivalent (see ``_UNSUPPORTED_BODIES`` for the reasons); the
    command text is left byte-for-byte unchanged for those.  Empty tuples
    mean the command was returned byte-for-byte unchanged.
    """

    command: str
    replacements: tuple[str, ...] = ()
    path_changes: tuple[str, ...] = ()
    shell_wrappers: tuple[str, ...] = ()
    nul_fixes: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        """Return whether any compatibility replacement was made."""
        return (
            bool(self.replacements)
            or bool(self.path_changes)
            or bool(self.shell_wrappers)
            or bool(self.nul_fixes)
        )

    @property
    def warning(self) -> str:
        """Return a concise description of compatibility changes."""
        parts: list[str] = []
        if self.unsupported:
            details = "; ".join(
                f"`{name}` — {_UNSUPPORTED_BODIES[name]}" for name in self.unsupported
            )
            parts.append(
                "Command(s) with no Windows Git Bash equivalent: "
                f"{details}."
            )
        if self.replacements:
            names = ", ".join(f"`{name}`" for name in self.replacements)
            parts.append(
                f"Added Windows Git Bash fallback(s) for native command(s): {names}."
            )
        if self.path_changes:
            words = ", ".join(f"`{word}`" for word in self.path_changes)
            parts.append(
                "Rewrote Windows path(s) for Git Bash (backslashes to forward "
                f"slashes; Git Bash virtual paths to native spellings): {words}."
            )
        if self.shell_wrappers:
            names = ", ".join(f"`{name}`" for name in self.shell_wrappers)
            parts.append(
                f"Removed redundant shell wrapper(s): {names}."
            )
        if self.nul_fixes:
            words = ", ".join(f"`{word}`" for word in self.nul_fixes)
            parts.append(
                "Rewrote null-device redirection target(s) for Git Bash: "
                f"{words} -> `/dev/null` (an unquoted `nul` would otherwise "
                "create an empty file named `nul`)."
            )
        return " ".join(parts)


@dataclass
class _BashWrapper:
    kind: str
    skip_next: bool = False
    opaque: bool = False
    path_value: bool = False
    operands: int = 0


@dataclass
class _BashHereDoc:
    delimiter: str | None
    strip_tabs: bool
    expands: bool


class _BashFixScanner:
    """Conservative scanner for Bash executable command positions."""

    __slots__ = ("s", "n", "edits", "names", "path_notes", "shell_notes", "heredoc_events", "nest_depth", "nul_fixes", "unsupported")

    def __init__(self, command: str) -> None:
        self.s = command
        self.n = len(command)
        self.edits: list[tuple[int, int, str]] = []
        self.names: list[str] = []
        self.path_notes: list[str] = []
        self.shell_notes: list[str] = []
        self.heredoc_events: list[tuple[int, int]] = []
        self.nest_depth = 0
        self.nul_fixes: list[str] = []
        self.unsupported: list[str] = []

    def fix(self) -> BashFix:
        try:
            self._scan_range(0, self.n)
        except RecursionError:
            # Malformed or adversarial nesting must never make the Bash tool
            # fail before Bash itself can report the syntax error.
            return BashFix(self.s)
        if (
            not self.names
            and not self.edits
            and not self.shell_notes
            and not self.nul_fixes
            and not self.unsupported
        ):
            return BashFix(self.s)
        unique_names = list(dict.fromkeys(self.names))
        definitions = "\n".join(_FALLBACKS[name] for name in unique_names)
        # Exported fallbacks are inherited by every nested bash (a ``bash -c``
        # operand of a command wrapper, the standalone runner scripts), where
        # the definitions above are not otherwise visible: ``env bash -c
        # 'rev <<< abc'`` keeps ``bash -c`` but the child shell still needs
        # ``rev`` to resolve to the function.  The export is conditional on
        # the function actually being defined: a fallback whose ``command -v``
        # guard found a real executable on PATH (coreutils ``uptime``, Windows
        # 11 ``sudo.exe``) installs nothing, and an unconditional ``export -f``
        # would then pollute stderr with "not a function" noise (the
        # interactive prelude guards its exports the same way).
        exports = "\n".join(
            f"if declare -F {name} >/dev/null; then export -f {name}; fi"
            for name in unique_names
        )
        source = self._build_source()
        source = self._build_fixed_source(source)
        prefix = definitions + "\n" + exports + "\n" if definitions else ""
        return BashFix(
            prefix + source,
            tuple(self.names),
            tuple(self.path_notes),
            tuple(self.shell_notes),
            tuple(self.nul_fixes),
            tuple(self.unsupported),
        )

    def _build_fixed_source(self, source: str | None = None) -> str:
        """Return the built source with heredoc trailing operators repaired.

        When no edits were recorded the build returns ``self.s`` itself, so the
        heredoc event positions this scan collected are still valid and are
        reused instead of scanning the same string a second time (the common
        case: e.g. fallback-command names alone produce no source edits).
        """
        if source is None:
            source = self._build_source()
        if source is self.s:
            return _fix_heredoc_trailing_operators(source, self.heredoc_events)
        return _fix_heredoc_trailing_operators(source)

    def _build_source(self) -> str:
        """Return the source with all recorded edits applied.

        Positions recorded by an inner scanner (a nested ``bash -c``/``eval``
        script) belong to the *inner* string and must not be interpreted as
        offsets into ``self.s``; callers splice the inner result themselves.
        """
        if not self.edits:
            return self.s
        return self._apply_edits(self.s, self.edits)

    @staticmethod
    def _apply_edits(s: str, edits: list[tuple[int, int, str]]) -> str:
        """Apply ``(start, end, replacement)`` edits to *s*."""
        if not edits:
            return s
        pieces: list[str] = []
        previous = 0
        for start, end, replacement in sorted(edits):
            pieces.extend((s[previous:start], replacement))
            previous = end
        pieces.append(s[previous:])
        return "".join(pieces)

    @staticmethod
    def _literal_word_value(raw: str) -> str | None:
        """Return the word value produced solely by Bash quote removal.

        Bash permits literal words such as ``'rev'``, ``\rev`` and ``r""ev``.
        Only words whose value can be determined without any expansion are
        accepted; parameter/command/arithmetic expansions, globbing, and
        malformed quotes return ``None`` so the caller leaves them untouched.
        """
        # Fast path: a word with no quoting/expansion characters evaluates to
        # itself (quote removal is the identity), so the walk below is skipped.
        search = _LITERAL_SPECIAL_RE.search
        m = search(raw)
        if m is None:
            return raw
        value: list[str] = []
        i = 0
        while i < len(raw):
            # Bulk-append the plain run before the next special character.
            m = search(raw, i)
            if m is None:
                value.append(raw[i:])
                break
            if m.start() > i:
                value.append(raw[i : m.start()])
                i = m.start()
            ch = raw[i]
            if ch == "\\":
                if i + 1 >= len(raw):
                    return None
                if raw[i + 1] == "\n":
                    i += 2
                    continue
                value.append(raw[i + 1])
                i += 2
                continue
            if ch == "'":
                close = raw.find("'", i + 1)
                if close < 0:
                    return None
                value.append(raw[i + 1 : close])
                i = close + 1
                continue
            if ch == '"':
                i += 1
                while i < len(raw) and raw[i] != '"':
                    inner = raw[i]
                    if inner in "$`":
                        return None  # runtime expansion: not a literal word
                    if inner == "\\" and i + 1 < len(raw):
                        escaped = raw[i + 1]
                        if escaped in '$`"\\\n':
                            if escaped != "\n":
                                value.append(escaped)
                            i += 2
                            continue
                    value.append(inner)
                    i += 1
                if i >= len(raw):
                    return None
                i += 1
                continue
            if ch == "$" and raw.startswith("$'", i):
                literal = _BashFixScanner._ansi_c_literal_span(raw, i)
                if literal is None:
                    return None
                text, i = literal
                value.append(text)
                continue
            if ch == "$" and raw.startswith('$"', i):
                # ``$"..."`` is locale translation: on Git Bash (no message
                # catalog) the value is the plain double-quoted body.
                close = i + 2
                while close < len(raw):
                    if raw[close] == "\\" and close + 1 < len(raw):
                        close += 2
                        continue
                    if raw[close] == '"':
                        break
                    close += 1
                if close >= len(raw):
                    return None
                inner_literal = _BashFixScanner._literal_word_value(
                    raw[i + 1 : close + 1]
                )
                if inner_literal is None:
                    return None
                value.append(inner_literal)
                i = close + 1
                continue
            if ch in "$`*?[{~":
                return None
            value.append(ch)
            i += 1
        return "".join(value)

    @staticmethod
    def _ansi_c_literal_span(raw: str, i: int) -> tuple[str, int] | None:
        """Decode the ``$'...'`` ANSI-C quote starting at *i* in *raw*.

        Returns ``(value, index_after_closing_quote)``, or ``None`` when the
        quote is unterminated or holds an unsupported escape (unicode
        ``\\u``/``\\U`` and multibyte octal sequences are rejected rather than
        approximated).  Supported escapes mirror bash: ``\\a \\b \\e \\E \\f
        \\n \\r \\t \\v \\\\ \\' \\" \\? \\c<char>`` plus octal ``\\[0-7]{1,3}``
        and hex ``\\x{1,2}`` for single-byte values.
        """
        if not raw.startswith("$'", i):
            return None
        body_start = i + 2
        j = body_start
        out: list[str] = []
        # Bash strings are C strings: the VALUE truncates at the first NUL
        # (``$'tree\0x'`` is the 4-char word ``tree``).  The scan still runs
        # to the closing quote so the returned index is correct, but nothing
        # after the NUL is part of the value.
        nul_seen = False
        while j < len(raw):
            ch = raw[j]
            if ch == "'":
                return "".join(out), j + 1
            if ch != "\\":
                if not nul_seen:
                    out.append(ch)
                j += 1
                continue
            j += 1
            if j >= len(raw):
                return None
            esc = raw[j]
            simple = {
                "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f",
                "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\",
                "'": "'", '"': '"', "?": "?",
            }
            if esc in simple:
                if not nul_seen:
                    out.append(simple[esc])
                j += 1
                continue
            if esc == "c":
                if j + 1 >= len(raw):
                    return None
                ctrl = raw[j + 1]
                code = ord(ctrl.upper()) & 0x1F
                if code == 0:
                    nul_seen = True
                elif not nul_seen:
                    out.append(chr(code))
                j += 2
                continue
            if esc in "01234567":
                digits = ""
                k = j
                while k < len(raw) and len(digits) < 3 and raw[k] in "01234567":
                    digits += raw[k]
                    k += 1
                code = int(digits, 8)
                if code > 0x7F:
                    return None  # multibyte octal: unsupported, be safe
                if code == 0:
                    nul_seen = True
                elif not nul_seen:
                    out.append(chr(code))
                j = k
                continue
            if esc == "x":
                digits = ""
                k = j + 1
                while (
                    k < len(raw)
                    and len(digits) < 2
                    and raw[k] in "0123456789abcdefABCDEF"
                ):
                    digits += raw[k]
                    k += 1
                if not digits:
                    return None
                code = int(digits, 16)
                if code > 0x7F:
                    return None
                if code == 0:
                    nul_seen = True
                elif not nul_seen:
                    out.append(chr(code))
                j = k
                continue
            if esc in "uU":
                return None  # unicode escapes: not worth approximating
            return None  # unknown escape: bash copies it, but stay conservative
        return None  # unterminated

    @classmethod
    def _is_effective_nul_target(cls, raw: str) -> bool:
        """Return True when redirection target word *raw* resolves to ``nul``.

        True for every spelling whose literal value (after bash quote removal
        / ANSI-C expansion) is exactly the word ``nul`` in any case: ``nul``,
        ``\\nul``, ``n\\ul``, ``$'nul'``, ``$'\\156ul'``, ``'nul'``,
        ``"nul"``.  All of these create an ordinary file named ``nul`` under
        Git Bash (verified), which is essentially always an accidental
        Windows-ism for ``/dev/null``.  Words containing runtime expansions
        (``$(...)``, backticks, ``${...}``, ``$var``) only resolve when the
        command runs and are rejected, as is any word whose value is a
        different string (``nul.txt``, ``null``, ``annul``).
        """
        if not raw:
            return False
        # Runtime-resolved words: leave untouched (a ``$var``/``$(...)``
        # target is data the scanner cannot evaluate).  ``$'...'`` and
        # ``$\"...\"`` are literal (no runtime expansion) and are decodable.
        if "$" in raw and not raw.startswith(("$'", '$"')):
            return False
        if "`" in raw:
            return False
        # Documented policy: a *whole-token* quoted ``'nul'``/``"nul"`` is an
        # intentional filename and is preserved byte-for-byte.  Accidental
        # spellings that still resolve to the word ``nul`` — ``\\nul``,
        # ``n\\ul``, ``$'nul'``, ``''nul``, ``nul''``, ``n""ul`` — are not a
        # deliberate choice and are rewritten.
        if raw[:1] in ("'", '"') and raw[-1:] == raw[:1] and len(raw) >= 2:
            inner = raw[1:-1]
            if inner.casefold() == "nul" and "\\" not in inner:
                return False
        value = cls._literal_word_value(raw)
        return value is not None and value.casefold() == "nul"

    @staticmethod
    def _literal_command_name(raw: str) -> str | None:
        """Return the fallback command name produced by Bash quote removal."""
        name = _BashFixScanner._literal_word_value(raw)
        return name if name is not None and name in _FALLBACKS else None

    @staticmethod
    def _literal_unsupported_name(raw: str) -> str | None:
        """Return the unsupported command name produced by Bash quote removal."""
        name = _BashFixScanner._literal_word_value(raw)
        return name if name is not None and name in _UNSUPPORTED_BODIES else None

    @staticmethod
    def _reparse_builtin_name(raw: str) -> int | None:
        """Return the operand count when *raw* is a literal reparse builtin.

        ``eval`` (``\\eval``, ``'eval'`` and ``ev""al`` all resolve to it)
        re-parses every operand; ``trap`` re-parses only its first (the rest
        are signal specs).  Returns ``-1`` for "all operands" or a positive
        count, ``None`` for any other word.
        """
        name = _BashFixScanner._literal_word_value(raw)
        if name is None:
            return None
        return _REPARSE_BUILTINS.get(name)

    @staticmethod
    def _shell_wrapper_name(raw: str) -> str | None:
        """Return the shell name when *raw* is a literal ``bash``/``sh`` word."""
        name = _BashFixScanner._literal_word_value(raw)
        return name if name is not None and name in _SHELL_WRAPPERS else None

    # The helpers above decode the raw word first; the ``*_value`` variants
    # below take an already-decoded literal value so a caller that decoded the
    # word once (the main scan loop) does not pay for the decode repeatedly.

    @staticmethod
    def _literal_command_name_value(name: str | None) -> str | None:
        return name if name is not None and name in _FALLBACKS else None

    @staticmethod
    def _literal_unsupported_name_value(name: str | None) -> str | None:
        return name if name is not None and name in _UNSUPPORTED_BODIES else None

    @staticmethod
    def _reparse_builtin_name_value(name: str | None) -> int | None:
        if name is None:
            return None
        return _REPARSE_BUILTINS.get(name)

    @staticmethod
    def _shell_wrapper_name_value(name: str | None) -> str | None:
        return name if name is not None and name in _SHELL_WRAPPERS else None

    @staticmethod
    def _plausible_script_file(raw: str) -> bool:
        """Return True when *raw* looks like a script path, not a command word.

        ``bash cd /c/dev/x`` and ``bash grep ...`` are redundant-wrapper
        mistakes (bash would try to open ``cd``/``grep`` as a script file);
        ``bash scripts/deploy.sh``, ``bash ./tool`` and ``bash app.sh`` are
        legitimate script invocations that must keep the wrapper.  A word with
        a directory separator, a leading ``.`` path segment, or a shell-script
        extension is treated as a script path; a bare command word is not.
        """
        if raw.startswith("./") or raw.startswith("../"):
            return True
        if raw.startswith(".\\") or raw.startswith("..\\"):
            return True
        if "/" in raw or "\\" in raw:
            return True
        lower = raw.lower()
        return any(
            lower.endswith(ext)
            for ext in (".sh", ".bash", ".zsh", ".ksh", ".dash", ".ash", ".bats")
        )

    def _handle_shell_wrapper(
        self,
        shell_name: str,
        word_start: int,
        i: int,
        end: int,
        *,
        assignment_prefix: bool = False,
        wrapped: bool = False,
    ) -> tuple[int, bool] | None:
        """Repair a redundant ``bash``/``sh`` invocation at command position.

        Returns ``(scan index, keep_wrapper)`` when the wrapper was rewritten,
        or ``None`` when the wrapper is left untouched (legitimate
        ``bash script.sh``, semantic options such as ``-e``/``-x``, stdin
        forms, scripts with trailing argv that ``$0``/``$1`` depend on, or an
        assignment prefix such as ``VAR=x bash ...`` whose scoping the rewrite
        could not preserve).  ``keep_wrapper`` asks the caller to leave any
        active command wrapper in place so the wrapped command keeps its
        executable context.

        Two shapes are repaired:
        * ``bash <command> ...`` — the shell word is redundant because the Bash
          tool already runs the whole string via bash, so it is removed
          (``bash cd /c/dev/x && grep ...`` becomes
          ``cd /c/dev/x && grep ...``); a bare ``bash`` would try to open
          ``cd`` as a script file and fail.
        * ``bash [-l] -c <script>`` — the wrapper is NOT inlined: the child
          shell's isolation (``exit``, assignments, ``cd``, traps) must be
          preserved, and the nested bash is harmless (it inherits the
          exported fallback functions).  Only the inline script is scanned
          and re-emitted in place (``bash -c 'cd C:\\x && rev'`` becomes
          ``bash -c 'cd C:/x && rev'``).  Only the ``-c``/``-lc``/``-cl``
          forms with the script as the final word of the segment are
          repaired this way; trailing argv, ``-ec``-style clusters and
          expansions are preserved for bash to handle.

        Under an active command wrapper (``env``/``nohup``/``timeout``/``sudo``
        ...) the shell word is an *operand* of that wrapper, so the repair
        must not move text out of its argv (``timeout 5 bash -c 'a && b'``
        rewritten to ``timeout 5 a && b`` would let ``&&`` split the list):
        both forms keep the wrapper's executable (standalone-runner) context
        — the ``-c`` form naturally, and the prefix form by leaving the
        wrapper active while only dropping the shell word.
        """
        if assignment_prefix:
            # ``VAR=x bash -c 'echo $VAR'``: the assignment is scoped to the
            # shell *process*, so unwrapping would make the outer shell expand
            # ``$VAR`` before the assignment takes effect.  Keep the wrapper.
            return None
        s = self.s
        j = i
        while j < end and s[j] in " \t\r":
            j += 1
        if j >= end or s[j] in _OPERATOR_CHARS or s[j] == "#":
            # Bare ``bash`` (or ``bash && ...`` / ``bash`` at EOF): bash would
            # start a nested shell; that is not a compatibility problem.
            return None
        next_end = self._read_word(j, end, scan_substitutions=False)
        if next_end <= j:
            return None
        next_raw = s[j:next_end]
        # The option/script-file decisions below must key on the word VALUE
        # (bash quote removal), not the raw source text: a quoted option
        # (``bash '-c' 'echo hi'``) or quoted script file (``bash "deploy.sh"``)
        # has a raw spelling that hides its role, and deleting the shell word
        # in front of it leaves the option/script word as the command name
        # (real bash: rc 0 -> 127).  A word whose value is not statically
        # decodable (``bash "$S"``) may open a script file relative to cwd,
        # so the unwrap is refused as well.
        next_val = self._literal_word_value(next_raw)
        if next_val is None:
            return None

        if next_val.startswith("-"):
            # Optional leading login flag, then the ``-c`` family.  Any other
            # option (``-e``, ``-x``, ``-i``, ``--norc``, ...) changes shell
            # behaviour and is left for bash to handle.
            opt_end = next_end
            opt = next_val
            if opt in ("-l", "-L", "--login"):
                cursor = opt_end
                while cursor < end and s[cursor] in " \t\r":
                    cursor += 1
                if cursor >= end or s[cursor] in _OPERATOR_CHARS or s[cursor] == "#":
                    return None
                opt_end = self._read_word(cursor, end, scan_substitutions=False)
                if opt_end <= cursor:
                    return None
                opt_raw = s[cursor:opt_end]
                opt_val = self._literal_word_value(opt_raw)
                if opt_val is None:
                    return None
                opt = opt_val
            if not _SHELL_C_OPTIONS.match(opt):
                return None
            # The word after ``-c`` is the inline script.
            k = opt_end
            while k < end and s[k] in " \t\r":
                k += 1
            if k >= end or s[k] in _OPERATOR_CHARS or s[k] == "#":
                return None  # ``bash -c`` with no script
            script_end = self._read_word(k, end, scan_substitutions=False)
            if script_end <= k:
                return None
            m = script_end
            while m < end and s[m] in " \t\r":
                m += 1
            if m < end and s[m] not in _OPERATOR_CHARS and s[m] != "#":
                # A digit run followed by a redirection operator
                # (``bash -c 'tree' 2>/dev/null``, ``1>&2``) closes the
                # wrapper segment exactly like ``;``/``&&``: the fd redirect
                # applies to the wrapper itself, it is NOT script argv.
                if not (s[m].isdigit() and self._redirection_after_fd(m, end)):
                    return None  # trailing script argv: ``$0``/``$1`` semantics
            script_raw = s[k:script_end]
            script = self._literal_word_value(script_raw)
            if script is None:
                # Expansions inside the script: the operand text cannot be
                # re-emitted, but its statically visible command words (and
                # the commands inside its substitutions, which run in this
                # shell) still need their fallback definitions registered
                # for the nested shell that re-parses the expanded text.
                self._harvest_nonliteral_inline_script(script_raw)
                return None  # leave the wrapper shape for bash to handle
            try:
                inner = _BashFixScanner(script)
                inner._scan_range(0, len(script))
            except RecursionError:
                return None
            fixed = inner._build_fixed_source()
            self.names.extend(n for n in inner.names if n not in self.names)
            self.path_notes.extend(inner.path_notes)
            self.shell_notes.extend(
                n for n in inner.shell_notes if n not in self.shell_notes
            )
            self.nul_fixes.extend(inner.nul_fixes)
            self.unsupported.extend(
                n for n in inner.unsupported if n not in self.unsupported
            )
            if inner.edits:
                self.edits.append((k, script_end, _single_quote(fixed)))
            # The wrapper stays: ``bash -c 'script'`` runs the script in a
            # CHILD shell whose isolation (``exit``, variable assignments,
            # ``cd``, traps) inlining into the surrounding script would
            # silently destroy.  The nested bash inherits the exported
            # fallback functions, and the in-place fix above repairs the
            # script's paths and commands.  Under an active command wrapper
            # (``env``/``nohup``/``timeout``/...) the wrapper additionally
            # keeps its executable context.
            return script_end, wrapped

        if self._plausible_script_file(next_val):
            return None
        # ``bash <command> ...``: drop the redundant shell word itself.  The
        # edit covers the shell word plus the whitespace that separated it from
        # the real command (``bash cd ...`` -> ``cd ...``).
        self.edits.append((word_start, j, ""))
        self.shell_notes.append(shell_name)
        # Under a command wrapper the wrapped command must keep its executable
        # context (``env bash rev`` -> ``env <runner>``), so the caller leaves
        # the active wrapper in place.
        return j, wrapped

    def _fix_shell_c_script_in_place(self, i: int, end: int) -> int | None:
        """Fix the ``-c`` inline script of a shell word seen as an *operand*.

        Covers shells invoked by another program (``find . -exec sh -c '...'
        \\;``, ``xargs -I{} sh -c '...'``) and command-position forms the
        unwrapper refuses to restructure (``bash -e -c '...'``, a script with
        trailing argv): the nested shell really runs, so the wrapper stays and
        only the quoted script operand is scanned and re-emitted in place —
        the same in-place repair the wrapped ``bash -c`` path performs.
        Returns the scan index after the script word, or ``None`` when no
        ``-c``-style option/script pair was found (nothing was changed).
        """
        s = self.s
        k = i
        script_start = -1
        script_end = -1
        expect_script = False
        while k < end:
            while k < end and s[k] in " \t\r":
                k += 1
            if k >= end or s[k] in _OPERATOR_CHARS or s[k] == "#":
                return None
            word_end = self._read_word(k, end, scan_substitutions=False)
            if word_end <= k:
                return None
            if expect_script:
                # The word right after the ``-c`` option is the inline script.
                script_start, script_end = k, word_end
                break
            raw = s[k:word_end]
            # Key on the word VALUE so quoted option spellings (``sh '-c'``,
            # ``bash \-c``) reach the script scan the same way raw ones do.
            opt = self._literal_word_value(raw)
            if opt is None:
                return None  # runtime expansion: not an option word we know
            if _SHELL_C_SCRIPT_OPTION_RE.match(opt):
                expect_script = True
            elif opt in _SHELL_VALUE_OPTIONS:
                # Value-taking option: skip its value word and keep looking
                # for the ``-c`` pair (``bash -o pipefail -c '...'``).
                k = word_end
                while k < end and s[k] in " \t\r":
                    k += 1
                if k >= end or s[k] in _OPERATOR_CHARS or s[k] == "#":
                    return None
                value_end = self._read_word(k, end, scan_substitutions=False)
                if value_end <= k:
                    return None
                k = value_end
                continue
            elif opt == "--":
                # End of options: the next word is a script PATH, not an
                # inline ``-c`` script.
                return None
            elif opt.startswith(("-", "+")):
                pass  # another option (``-e``, ``-l``): keep looking
            else:
                # A plain operand before any ``-c`` (``sh script.sh``): not
                # an inline-script form.
                return None
            k = word_end
        if script_start < 0:
            return None
        script_raw = s[script_start:script_end]
        script = self._literal_word_value(script_raw)
        if script is None:
            # Runtime expansions inside: no re-emit, but still register the
            # statically visible fallbacks (see _harvest_nonliteral_inline_script).
            self._harvest_nonliteral_inline_script(script_raw)
            return script_end
        try:
            inner = _BashFixScanner(script)
            inner._scan_range(0, len(script))
        except RecursionError:
            return script_end
        if (
            not inner.names
            and not inner.unsupported
            and not inner.nul_fixes
            and not inner.path_notes
        ):
            return script_end
        # Merge the inner script's registrations exactly like the wrapped
        # ``bash -c`` branch above.  The fallback definitions emitted for this
        # shell are ``export -f``-ed, and a nested bash / msys ``sh`` inherits
        # exported functions from its environment (verified on Git Bash 5.3:
        # ``tree() { ...; }; export -f tree; bash -e -c 'tree'`` runs the
        # function).  Dropping ``inner.names`` here left every option-led
        # wrapper repaired in place but still failing at runtime — the nested
        # shell had no ``tree`` (``bash -e -c 'tree'``, ``find . -exec sh -c
        # 'tree' \;``, ``sh -c 'rev' a b``) — while the expansion-bearing
        # variant (``_harvest_nonliteral_inline_script``) already registered
        # them.
        self.names.extend(n for n in inner.names if n not in self.names)
        self.path_notes.extend(inner.path_notes)
        self.shell_notes.extend(
            n for n in inner.shell_notes if n not in self.shell_notes
        )
        self.nul_fixes.extend(inner.nul_fixes)
        self.unsupported.extend(
            n for n in inner.unsupported if n not in self.unsupported
        )
        if inner.edits:
            self.edits.append(
                (script_start, script_end, _single_quote(inner._build_fixed_source()))
            )
        return script_end

    def _inline_script_operand(
        self, word_start: int, word_end: int, raw: str
    ) -> bool:
        """Fix the inline script of a quoted runtime-reparsed operand.
            Shared by the constructs whose quoted operand is re-parsed as shell
            code at *runtime*: procps ``watch`` (executes its command line
            through ``sh -c``; the Git Bash fallback mirrors that with
            ``eval "$*"``), ``eval`` (concatenates its operands and re-parses
            them), and ``trap`` (stores its first operand and re-parses it when
            the signal fires).  A quoted operand (``watch -n1 'rev <<< abc'``,
            ``eval 'foo > nul'``, ``trap 'foo > nul' EXIT``) is therefore an
            inline script: its literal value is scanned as its own command
            context and re-emitted with the inner fixes applied, so fallback
            names, Windows paths, and ``> nul`` null-device redirections inside
            it work.  Unquoted operands need no special handling here — they
            flow through the normal word rules.

            Returns ``True`` when the word was consumed as an inline script (any
            quoted operand): the caller must not also treat it as a command word,
            or a script that *is* a single fallback name (``watch 'uptime'``)
            would be recorded twice.
            """
        if not raw.startswith(("'", '"', "$'", '$"')):
            return False
        script = self._literal_word_value(raw)
        if script is None:
            # Expansions inside the script: no re-emit, but still register
            # the statically visible fallbacks (see _harvest_nonliteral_inline_script).
            self._harvest_nonliteral_inline_script(raw)
            return True
        try:
            inner = _BashFixScanner(script)
            inner._scan_range(0, len(script))
        except RecursionError:
            return True
        fixed = inner._build_fixed_source()
        self.names.extend(n for n in inner.names if n not in self.names)
        self.path_notes.extend(inner.path_notes)
        self.shell_notes.extend(
            n for n in inner.shell_notes if n not in self.shell_notes
        )
        self.nul_fixes.extend(inner.nul_fixes)
        self.unsupported.extend(
            n for n in inner.unsupported if n not in self.unsupported
        )
        if inner.edits:
            self.edits.append((word_start, word_end, _single_quote(fixed)))
        return True

    def _harvest_nonliteral_inline_script(self, raw: str) -> None:
        """Register fallbacks from an inline-script operand that holds
        runtime expansions.

        ``bash -c "rev $HOME"`` / ``watch "rev <<< $x"`` operands cannot be
        re-emitted (any re-quoting would change the word's runtime value), so
        no edit is recorded — but the statically visible command words must
        still register their fallback definitions, and so must the commands
        inside the operand's ``$(...)`` / backtick substitutions: those run
        in *this* shell (they build the script string), and the child shell
        re-parses the expanded text with the exported fallback functions
        available.  Only registrations are merged; path/nul metadata is not,
        because the operand text stays byte-for-byte and such metadata would
        claim a fix that was never applied.
        """
        inner_text = raw
        if len(raw) >= 2 and raw[0] in "'\"" and raw[-1] == raw[0]:
            inner_text = raw[1:-1]
        sanitized, substitutions = _opaque_inline_expansions(inner_text)
        sources = [sanitized, *substitutions]
        for source in sources:
            if not source.strip():
                continue
            try:
                inner = _BashFixScanner(source)
                inner._scan_range(0, len(source))
            except RecursionError:
                continue
            self.names.extend(n for n in inner.names if n not in self.names)
            self.shell_notes.extend(
                n for n in inner.shell_notes if n not in self.shell_notes
            )
            self.unsupported.extend(
                n for n in inner.unsupported if n not in self.unsupported
            )


    def _scan_range(self, start: int, end: int) -> None:
        """Scan *start..end* as a command context, bounding recursion depth.

        The wrapper exists so pathological nesting (hundreds of nested
        ``$( ... )`` levels) costs O(_MAX_NESTING_DEPTH * length) instead of
        O(depth * length); the innermost levels are left for Bash.  An empty
        range holds no commands, so it is skipped without touching the depth
        counter.
        """
        if start >= end:
            return
        if self.nest_depth >= _MAX_NESTING_DEPTH:
            return
        self.nest_depth += 1
        try:
            self._scan_range_inner(start, end)
        finally:
            self.nest_depth -= 1

    def _scan_range_inner(self, start: int, end: int) -> None:
        s = self.s
        i = start
        command_expected = True
        redirect_expected = False
        redirect_resume = True
        redirect_op: str | None = None
        wrapper: _BashWrapper | None = None
        heredoc_operator: str | None = None
        herestring_flag = False
        pending_heredocs: list[_BashHereDoc] = []
        case_stack: list[str] = []
        # Set right after a command-position ``for`` word so the immediately
        # following ``(( ))`` unit is recognised as the arithmetic loop header
        # (``for ((expr))``).  Bash 5 accepts a brace-group loop body without
        # ``do`` right after the header
        # (``for ((i=0;i<2;i++)) { tree; rev; }``); ``for_brace_body`` carries
        # that expectation to the next ``{`` word.
        for_arith_header = False
        for_brace_body = False
        function_name_expected = False
        function_body_expected = False
        # Set when an assignment prefix (``VAR=x``) precedes the command word on
        # this line; a following ``bash`` wrapper must keep its shell-process
        # scoping, so it is not unwrapped (``VAR=x bash -c 'echo $VAR'``).
        assignment_prefix = False
        # Non-zero right after a command-position reparse builtin (``eval``/
        # ``trap``): the following argument words are inline scripts.  -1
        # means every operand (``eval``); a positive count covers only the
        # leading operands (``trap``'s single script operand).
        reparse_operands = 0
        # True only for the FIRST operand word after a reparse builtin: bash
        # concatenates the operands and re-parses them, so the leading word
        # holds the command name (see the reparse branch below).
        reparse_leading = False
        # Literal value of the command word of the current simple command
        # (used to decide whether argument words can be executed by the
        # command, e.g. ``find -exec sh -c``; see _EXEC_OPERAND_COMMANDS).
        command_word: str | None = None
        # Pattern-consumer state for the current simple command (see
        # _PATTERN_PROGRAM_COMMANDS): the command family, the verdict
        # deferred to the next word by a value-taking option, and the count
        # of bare operands seen.  Resets wherever command_word resets.
        pattern_family: str | None = None
        pattern_pending: str | None = None
        pattern_bare = 0
        # One-shot suppression of the ``#``-comment check for the character
        # immediately after a command/process substitution's closing ``)``.
        # That ``)`` is word-internal in Bash (a substitution expands inside
        # a word), so a glued ``#`` continues the word and does NOT start a
        # comment — unlike a ``#`` after a control-operator ``)`` (subshell),
        # which genuinely comments out the rest of the line.
        subst_just_closed = False

        while i < end:
            ch = s[i]
            # Snapshot and clear the one-shot substitution-close flag: it
            # applies ONLY to the character directly after the ``)`` (this
            # iteration), never across whitespace or control operators.
            suppress_hash_comment = subst_just_closed
            subst_just_closed = False

            if ch in " \t\r":
                # A whitespace *run* (indentation, aligned columns) is skipped
                # in one C-level match; a single space just steps over.
                if i + 1 < end and s[i + 1] in " \t\r":
                    m = _HSPACE_RE.match(s, i, end)
                    i = m.end() if m is not None else i + 1
                else:
                    i += 1
                continue
            if ch == "\\" and i + 1 < end and s[i + 1] == "\n":
                i += 2
                continue
            if ch == "\n":
                i += 1
                if pending_heredocs:
                    i = self._skip_heredoc_bodies(i, end, pending_heredocs)
                    pending_heredocs.clear()
                command_expected = True
                redirect_expected = False
                redirect_op = None
                heredoc_operator = None
                herestring_flag = False
                wrapper = None
                assignment_prefix = False
                reparse_operands = 0
                command_word = None
                pattern_family = None
                pattern_pending = None
                pattern_bare = 0
                for_arith_header = False
                for_brace_body = False
                continue
            if (
                ch == "#"
                and self._comment_starts(i, start)
                and not suppress_hash_comment
            ):
                newline = s.find("\n", i + 1, end)
                i = end if newline < 0 else newline
                continue

            process_substitution = ch in _REDIRECTION_START and s[i + 1 : i + 2] == "("
            if not process_substitution and (
                ch in _REDIRECTION_START
                or (ch == "&" and s[i + 1 : i + 2] == ">")
                or (ch.isdigit() and self._redirection_after_fd(i, end))
            ):
                op_start = i
                if ch.isdigit():
                    while i < end and s[i].isdigit():
                        i += 1
                op, i = self._read_redirection(i, end)
                if op:
                    redirect_resume = command_expected
                    redirect_expected = True
                    redirect_op = op
                    herestring_flag = op == "<<<"
                    if op in {"<<", "<<-"}:
                        # The delimiter is captured when the following word is
                        # read; its body starts only after this command line.
                        pass
                    else:
                        op_start = -1
                    if op_start >= 0:
                        heredoc_operator = op
                    continue
                i = op_start

            if redirect_expected:
                if ch in _REDIRECTION_START and s[i + 1 : i + 2] == "(":
                    close = self._find_matching(i + 2, end, ")")
                    self._scan_range(i + 2, close if close < end else end)
                    word_end = close + 1 if close < end else end
                    # A ``#`` glued after the process-substitution ``)`` is
                    # word-internal, not a comment (see the main-loop check).
                    subst_just_closed = True
                else:
                    scan_substitutions = heredoc_operator not in {"<<", "<<-"}
                    word_end = self._read_word(
                        i, end, scan_substitutions=scan_substitutions
                    )
                if word_end <= i:
                    i += 1
                    continue
                if heredoc_operator in {"<<", "<<-"}:
                    heredoc = self._heredoc_delimiter(s[i:word_end])
                    if heredoc is not None:
                        delimiter, expands = heredoc
                        pending_heredocs.append(
                            _BashHereDoc(delimiter, heredoc_operator == "<<-", expands)
                        )
                elif not herestring_flag:
                    raw_word = s[i:word_end]
                    # ``> nul`` / ``2> NUL`` / ``&> nul`` / ``>> nul`` would
                    # silently create a file literally named ``nul`` in Git
                    # Bash (Windows-style null-device redirection).  Escape
                    # spellings whose value is exactly the word ``nul`` create
                    # the file too (verified on Git Bash: ``> \nul``,
                    # ``> n\ul``, ``> $'nul'`` all produce it), so they are
                    # rewritten as well.  Quoted ``'nul'``/``"nul"`` stays an
                    # intentional filename (documented policy), words with
                    # expansions (``> $(echo nul)``) keep their value only at
                    # runtime and are left untouched, and input redirections
                    # (``< nul``) never create a file.
                    if (
                        redirect_op is not None
                        # Pure input redirections (``<``/``<<``/``<<<``/``<&``)
                        # never create a file; ``<>`` opens its target for
                        # reading *and writing*, so ``<> nul`` does.
                        and redirect_op not in {"<", "<<", "<<-", "<<<", "<&"}
                        and self._is_effective_nul_target(raw_word)
                    ):
                        self.edits.append((i, word_end, "/dev/null"))
                        self.nul_fixes.append(raw_word)
                    else:
                        replacement = self._path_replacement(raw_word)
                        if replacement is not None:
                            self.edits.append((i, word_end, replacement))
                            self.path_notes.append(raw_word)
                i = word_end
                command_expected = redirect_resume
                redirect_expected = False
                redirect_op = None
                heredoc_operator = None
                continue

            if ch == "[" and s[i + 1 : i + 2] == "[":
                function_body_expected = False
                i = self._skip_conditional(i + 2, end)
                command_expected = False
                for_brace_body = False
                continue
            if ch == "(" and s[i + 1 : i + 2] == "(":
                function_body_expected = False
                i = self._skip_arithmetic(i + 2, end)
                # ``for ((expr))`` arithmetic header: bash 5 accepts a do-less
                # brace-group body right after the unit
                # (``for ((i=0;i<2;i++)) { tree; rev; }``); the ``{`` word then
                # opens the group in command position.  A bare simple command
                # there is a syntax error, so words stay arguments.
                command_expected = False
                for_brace_body = for_arith_header
                for_arith_header = False
                continue
            if ch == "$" and s[i + 1 : i + 2] == "(" and s[i + 2 : i + 3] != "(":
                close = self._find_matching(i + 2, end, ")")
                inner_end = close if close < end else end
                self._scan_range(i + 2, inner_end)
                i = close + 1 if close < end else end
                # A ``#`` glued after the substitution's ``)`` continues the
                # word in Bash — it must not start a comment here.
                subst_just_closed = True
                for_brace_body = False
                if case_stack and case_stack[-1] == "word":
                    # A substitution can be the case subject word itself
                    # (``case $(...) in ...``); it ends the header just like
                    # a plain subject word.
                    case_stack[-1] = "await-in"
                command_expected = False
                continue
            if ch == "`":
                close = self._find_backtick_end(i + 1, end)
                self._scan_range(i + 1, close)
                i = close + 1 if close < end else end
                for_brace_body = False
                if case_stack and case_stack[-1] == "word":
                    case_stack[-1] = "await-in"
                command_expected = False
                continue
            if ch in _REDIRECTION_START and s[i + 1 : i + 2] == "(":
                close = self._find_matching(i + 2, end, ")")
                self._scan_range(i + 2, close if close < end else end)
                i = close + 1 if close < end else end
                # A ``#`` glued after the process-substitution's ``)`` is
                # word-internal, not a comment (see the main-loop check).
                subst_just_closed = True
                for_brace_body = False
                if case_stack and case_stack[-1] == "word":
                    case_stack[-1] = "await-in"
                command_expected = False
                continue

            if ch in _CONTROL_OP_STARTS:
                op, op_end = self._read_control_operator(i, end)
            else:
                op, op_end = "", i
            if op:
                i = op_end
                for_brace_body = False
                if op == "(" and function_body_expected:
                    function_body_expected = False
                    command_expected = True
                elif op == "(":
                    command_expected = True
                elif op == ")":
                    if case_stack and case_stack[-1] == "patterns":
                        case_stack[-1] = "body"
                        command_expected = True
                    else:
                        command_expected = False
                elif op in {";;", ";&", ";;&"}:
                    if case_stack:
                        case_stack[-1] = "patterns"
                        command_expected = False
                    else:
                        command_expected = True
                else:
                    command_expected = True
                redirect_expected = False
                heredoc_operator = None
                wrapper = None
                assignment_prefix = False
                reparse_operands = 0
                command_word = None
                pattern_family = None
                pattern_pending = None
                pattern_bare = 0
                continue

            word_start = i
            scan_substitutions = heredoc_operator not in {"<<", "<<-"}
            word_end = self._read_word(i, end, scan_substitutions=scan_substitutions)
            if word_end <= i:
                i += 1
                continue
            raw = s[word_start:word_end]
            i = word_end
            # Any word token is a full token boundary: a pending ``for`` word
            # only pairs with an ``(( ))`` unit that follows it immediately.
            brace_body_open = for_brace_body and raw == "{"
            for_arith_header = False
            for_brace_body = False
            if function_name_expected:
                function_name_expected = False
                function_body_expected = True
                command_expected = False
                declaration_end = self._empty_parentheses_end(i, end)
                if declaration_end is not None:
                    i = declaration_end
                continue

            if function_body_expected:
                function_body_expected = False
                if raw == "{":
                    command_expected = True
                    continue
                # Brace-less body: a compound keyword (`f() if …`, `f() case
                # …`, `f() while …`) or a simple command (`f() tree;`).  Fall
                # through to the ordinary command-position handling below —
                # it re-arms command position for test keywords and sets up
                # the case/for state machine — instead of consuming the word
                # as data, which dropped the body's fallback registrations.
                command_expected = True

            if case_stack and case_stack[-1] == "word":
                case_stack[-1] = "await-in"
                command_expected = False
                continue
            if case_stack and case_stack[-1] == "await-in" and raw == "in":
                case_stack[-1] = "patterns"
                command_expected = False
                continue
            if case_stack and case_stack[-1] == "patterns":
                if raw == "esac":
                    case_stack.pop()
                command_expected = False
                continue

            if not command_expected:
                if (
                    command_word in _EXEC_OPERAND_COMMANDS
                    and self._shell_wrapper_name(raw) is not None
                ):
                    # A shell invoked as an *operand* of a program that
                    # executes its argv (``find . -exec sh -c '...' \;``,
                    # ``xargs sh -c``): the nested shell really runs, so fix
                    # its ``-c`` inline script in place.  Gated on the current
                    # command word so plain data uses (``echo sh -c '...'``)
                    # stay untouched.
                    script_end = self._fix_shell_c_script_in_place(i, end)
                    if script_end is not None:
                        i = script_end
                        continue
                if reparse_operands and raw not in _COMMAND_END_KEYWORDS:
                    # Argument word of a reparse builtin (``eval``/``trap``):
                    # its literal value is re-parsed as shell code at runtime,
                    # so scan it as its own command context (this is what
                    # fixes ``eval 'foo > nul'``).  Unquoted words are
                    # ordinary words in the current context (``eval foo >
                    # nul`` — the redirection is scanned here anyway); only
                    # quoted operands carry a whole script.
                    if reparse_operands > 0:
                        reparse_operands -= 1
                    if self._inline_script_operand(word_start, word_end, raw):
                        reparse_leading = False
                        continue
                    if reparse_leading:
                        # Unquoted leading operand of ``eval``/``trap``: bash
                        # concatenates and re-parses it as code in THIS shell
                        # (the prelude's fallback functions are in scope), so
                        # a literal command name it carries needs its fallback
                        # registered just like a quoted script's command word
                        # (``eval tree`` runs ``tree`` -> rc 127 without it).
                        # Only the LEADING word is command position; later
                        # operands are arguments (``eval echo tree``).
                        reparse_leading = False
                        operand = self._literal_word_value(raw)
                        if operand:
                            fallback_name = self._literal_command_name_value(
                                operand
                            )
                            if fallback_name is not None:
                                self.names.append(fallback_name)
                            else:
                                self._record_unsupported(raw, operand)
                if raw in {"then", "do", "else", "elif"}:
                    command_expected = True
                elif raw == "esac" and case_stack:
                    case_stack.pop()
                elif brace_body_open:
                    # do-less ``for ((expr)) { … }" body opens here
                    command_expected = True
                elif pattern_family is not None:
                    # Argument of a pattern-consuming command (grep/sed/awk):
                    # the leading operand is pattern/program DATA, so a
                    # drive-prefixed word keeps its runtime value instead of
                    # being "repaired" into a different string (see
                    # _PATTERN_PROGRAM_COMMANDS).
                    value = self._literal_word_value(raw)
                    word = value if value is not None else raw
                    if pattern_pending is not None:
                        pending, pattern_pending = pattern_pending, None
                        path_ok = pending == "path"
                    elif word.startswith("-") and word != "-":
                        option = self._classify_pattern_option(
                            pattern_family, word
                        )
                        # Option words themselves are never paths (they do
                        # not start with a drive letter, so the rewrite
                        # could not fire anyway); defer the verdict for
                        # value-taking options.
                        path_ok = False
                        if option == "data-pending":
                            pattern_pending = "data"
                        elif option == "path-pending":
                            pattern_pending = "path"
                        elif option == "value-pending":
                            pattern_pending = "value"
                    elif pattern_bare == 0:
                        # First bare operand: the pattern/program.
                        pattern_bare += 1
                        path_ok = False
                    else:
                        # Later bare operands: files.
                        pattern_bare += 1
                        path_ok = True
                    if path_ok:
                        replacement = self._path_replacement(raw)
                        if replacement is not None:
                            self.edits.append(
                                (word_start, word_end, replacement)
                            )
                            self.path_notes.append(raw)
                else:
                    replacement = self._path_replacement(raw)
                    if replacement is not None:
                        self.edits.append((word_start, word_end, replacement))
                        self.path_notes.append(raw)
                if (
                    "=" in raw
                    and _ASSIGNMENT_RE.match(raw)
                    and i < end
                    and s[i] == "("
                ):
                        # Array literal as a declaration-builtin argument
                        # (``declare -a arr=( ...)``): its elements are data
                        # words, scanned like the command-position form.
                        close = self._find_matching(i + 1, end, ")")
                        self._scan_array_words(
                            i + 1, close if close < end else end
                        )
                        i = close + 1 if close < end else end
                continue

            if raw == "function":
                function_name_expected = True
                command_expected = True
                continue
            declaration_end = self._function_declaration_end(raw, i, end)
            if declaration_end is not None:
                i = declaration_end
                function_body_expected = True
                command_expected = False
                continue
            if raw in _COMMAND_START_KEYWORDS:
                command_expected = True
                continue
            if raw in _COMMAND_END_KEYWORDS:
                if raw == "esac" and case_stack:
                    case_stack.pop()
                command_expected = False
                continue
            if raw in _LIST_KEYWORDS:
                if raw == "case":
                    case_stack.append("word")
                elif raw == "for":
                    for_arith_header = True
                command_expected = False
                continue
            if "=" in raw and _ASSIGNMENT_RE.match(raw):
                if i < end and s[i] == "(":
                    close = self._find_matching(i + 1, end, ")")
                    self._scan_array_words(i + 1, close if close < end else end)
                    i = close + 1 if close < end else end
                command_expected = True
                assignment_prefix = True
                continue

            if raw == "cd":
                self._drop_cmd_cd_flag(i, end)

            executable_wrapper = (
                wrapper is not None and wrapper.kind not in _SAME_SHELL_WRAPPERS
            )
            if wrapper is not None and wrapper.kind == "coproc":
                if self._coproc_name_before_compound(raw, i, end):
                    wrapper = None
                    command_expected = True
                    continue
            inline_consumed = False
            if wrapper is not None and wrapper.kind in _WRAPPER_PATH_OPTIONS:
                for option in _WRAPPER_PATH_OPTION_LONG:
                    if raw.startswith(option + "="):
                        value = raw[len(option) + 1 :]
                        replacement = self._path_replacement(value)
                        if replacement is not None:
                            self.edits.append(
                                (word_start, word_end, option + "=" + replacement)
                            )
                            self.path_notes.append(raw)
                        # An inline option also fills a pending value slot
                        # (``env -C --chdir=D:\\x``) and leaves the wrapper
                        # itself active for the command that follows it.
                        wrapper.skip_next = False
                        wrapper.path_value = False
                        command_expected = True
                        inline_consumed = True
                        break
            if inline_consumed:
                continue
            if wrapper is not None:
                path_option_value = wrapper.path_value and wrapper.skip_next
                action = self._consume_wrapper_word(wrapper, raw)
                if action == "skip":
                    if path_option_value:
                        replacement = self._path_replacement(raw)
                        if replacement is not None:
                            self.edits.append((word_start, word_end, replacement))
                            self.path_notes.append(raw)
                    command_expected = True
                    continue
                if action == "inspect":
                    command_expected = False
                    wrapper = None
                    continue
                if wrapper is not None and wrapper.kind == "watch":
                    # ``watch`` re-executes its command in the current shell
                    # (``eval "$*"``, matching procps ``watch``'s ``sh -c``),
                    # so a quoted operand is an inline script: scan its
                    # literal value as its own command context so fallback
                    # names and Windows paths inside it are fixed too.  An
                    # unquoted operand falls through to the plain command-word
                    # rules below (``watch`` is a same-shell wrapper, so the
                    # source word resolves against the prefix functions).
                    consumed = self._inline_script_operand(
                        word_start, word_end, raw
                    )
                    wrapper = None
                    if consumed:
                        command_expected = False
                        continue

            # A leading-backslash word (``\sudo tree``) only suppresses
            # alias lookup of the wrapper name; it invokes the same wrapper,
            # so the tables are keyed on the backslash-stripped word.  Shell
            # keywords (``time``, ``coproc``) are the exception: escaping
            # suppresses the KEYWORD, so the external command runs — and an
            # external command cannot invoke the registered shell functions
            # (the raw keyword can), making wrapper registration useless.
            unescaped = (
                raw[1:]
                if raw.startswith("\\") and raw[1:] not in _SHELL_KEYWORDS
                else raw
            )
            # Quoted spellings of the wrapper word itself (``$'command'``,
            # ``"env"``) decode to the same static name the tables key on;
            # without the fallback the wrapped command's operand is never
            # scanned (``$'command' $'tree'`` -> rc 127).  Shell keywords are
            # excluded for the same reason as the backslash form above: a
            # quoted/escaped keyword suppresses keyword recognition, so the
            # external command runs and cannot invoke the shell functions.
            literal_unescaped: str | None = None
            if raw != unescaped or raw[:1] in {"'", '"', "$"}:
                decoded = self._literal_word_value(raw)
                if decoded is not None and decoded not in _SHELL_KEYWORDS:
                    literal_unescaped = decoded
            wrapper_key = unescaped
            if wrapper_key not in _FALLBACK_COMMAND_WRAPPERS and (
                wrapper_key not in _COMMAND_WRAPPERS
            ):
                wrapper_key = literal_unescaped if literal_unescaped else wrapper_key
            # Fallback wrappers are checked first: ``sudo`` is both a plain
            # command wrapper (operand semantics, option tables) and a
            # fallback name whose definition must be recorded for hosts
            # without ``sudo.exe``.
            fallback_wrapper = _FALLBACK_COMMAND_WRAPPERS.get(wrapper_key)
            if fallback_wrapper is not None:
                # ``gtimeout 5 rev``/``watch -n1 rev``/``sudo rev``: the
                # wrapper word is itself a fallback (its definition is
                # recorded) and the command that follows its
                # options/operands is scanned like a wrapped command word.
                self.names.append(wrapper_key)
                if executable_wrapper:
                    # The wrapping executable (``xargs gtimeout ...``) cannot
                    # invoke shell functions: swap the word for the standalone
                    # runner.  The operand scan continues through the wrapper
                    # state so the names the wrapped command uses are still
                    # recorded; the prefix exports them, which the runner's
                    # nested bash inherits.
                    self.edits.append(
                        (word_start, word_end, _wrapper_runner(wrapper_key))
                    )
                wrapper = _BashWrapper(
                    fallback_wrapper,
                    operands=_WRAPPER_OPERAND_COUNTS.get(fallback_wrapper, 0),
                )
                command_expected = True
                continue
            if wrapper_key in _COMMAND_WRAPPERS:
                wrapper = _BashWrapper(
                    wrapper_key,
                    operands=_WRAPPER_OPERAND_COUNTS.get(wrapper_key, 0),
                )
                command_expected = True
                command_word = self._literal_word_value(raw) or raw
                continue

            # Decode the word's literal value ONCE: the shell-wrapper, reparse
            # builtin, fallback-name, unsupported-name and command_word checks
            # below all need it, and each used to re-run the quote-removal
            # walk on the same raw text.
            literal = self._literal_word_value(raw)

            # Redundant shell invocation (``bash cd ...``, ``bash -c '...'``):
            # only at a plain command position — after ``command``/``env``/
            # ``sudo`` the shell word is the wrapped command itself and is
            # consumed by the wrapper logic above, so it is left untouched.
            shell_name = self._shell_wrapper_name_value(literal)
            if shell_name is not None:
                handled = self._handle_shell_wrapper(
                    shell_name,
                    word_start,
                    i,
                    end,
                    assignment_prefix=assignment_prefix,
                    wrapped=wrapper is not None,
                )
                if handled is not None:
                    i, keep_wrapper = handled
                    command_expected = True
                    redirect_expected = False
                    heredoc_operator = None
                    if not keep_wrapper:
                        wrapper = None
                    assignment_prefix = False
                    continue

                # The unwrap was refused (semantic options such as ``-e``,
                # trailing argv, ...): the nested shell still runs, so fix a
                # ``-c`` inline script operand in place.
                script_end = self._fix_shell_c_script_in_place(i, end)
                if script_end is not None:
                    i = script_end
                    command_expected = False
                    wrapper = None
                    reparse_operands = 0
                    continue

            # Record the command word of this simple command so argument-
            # position decisions (e.g. ``find -exec sh -c '...'``) know what
            # program will consume the words that follow.
            command_word = literal if literal is not None else raw
            pattern_family = (
                command_word
                if command_word in _PATTERN_PROGRAM_COMMANDS
                else None
            )
            pattern_pending = None
            pattern_bare = 0

            reparse_count = self._reparse_builtin_name_value(literal)
            if reparse_count is not None:
                # ``eval``/``trap``: the operand words that follow are
                # re-parsed as shell code at runtime; scan the quoted ones as
                # inline scripts so ``> nul`` inside them is rewritten too.
                reparse_operands = reparse_count
                reparse_leading = True
                command_expected = False
                wrapper = None
                assignment_prefix = False
                continue

            fallback_name = self._literal_command_name_value(literal)
            if fallback_name is not None:
                self.names.append(fallback_name)
                if executable_wrapper:
                    self.edits.append(
                        (word_start, word_end, _wrapper_runner(fallback_name))
                    )
            elif self._record_unsupported(raw, literal):
                pass
            else:
                # A command word can itself be a Windows executable path
                # (``C:\tools\rg.exe``) or a Git Bash virtual absolute path
                # (``/c/tools/rg.exe``); Bash quote removal would eat the
                # backslashes and lose the command, so rewrite it like an
                # argument path.
                replacement = self._path_replacement(raw)
                if replacement is not None:
                    self.edits.append((word_start, word_end, replacement))
                    self.path_notes.append(raw)
            command_expected = False
            wrapper = None

    def _record_unsupported(self, raw: str, name: object = _UNDECODED) -> bool:
        """Record a command word with no Windows Git Bash equivalent.

        Returns True when *raw* literal-resolved to an unsupported command
        name; the command text is left untouched so the app layer can surface
        ``_UNSUPPORTED_BODIES``' reason instead of executing a guaranteed
        "command not found".  *name* may carry the already-decoded literal
        value (``None`` included); the ``_UNDECODED`` sentinel decodes *raw*.
        """
        if name is _UNDECODED:
            name = self._literal_unsupported_name(raw)
        if name is None or name not in _UNSUPPORTED_BODIES:
            return False
        if name not in self.unsupported:
            self.unsupported.append(name)
        return True

    def _read_word(
        self, start: int, end: int, *, scan_substitutions: bool = True
    ) -> int:
        s = self.s
        i = start
        search = _WORD_SPECIAL_RE.search
        while i < end:
            # Bulk-skip plain word characters (C-level regex search): only the
            # special set can end a word or open a quoted/expanded region.
            m = search(s, i, end)
            if m is None:
                return end
            i = m.start()
            ch = s[i]
            if ch in _WORD_END_CHARS:
                break
            if ch == "#":
                if i == start:
                    break
                i += 1
                continue
            if ch == "\\":
                i += 2 if i + 1 < end else 1
                continue
            if ch == "'":
                i = self._skip_single_quote(i + 1, end)
                continue
            if ch == '"':
                if scan_substitutions:
                    i = self._skip_double_quote(i + 1, end)
                else:
                    i = self._skip_double_quote_for_matching(i + 1, end)
                continue
            if ch == "`":
                close = self._find_backtick_end(i + 1, end)
                if scan_substitutions:
                    self._scan_range(i + 1, close)
                i = close + 1 if close < end else end
                continue
            # ch == "$"
            nxt = s[i + 1] if i + 1 < end else ""
            if nxt == "(":
                if s.startswith("$((", i):
                    i = self._skip_arithmetic(i + 3, end)
                    continue
                close = self._find_matching(i + 2, end, ")")
                if scan_substitutions:
                    self._scan_range(i + 2, close if close < end else end)
                i = close + 1 if close < end else end
                continue
            if nxt == "{":
                if scan_substitutions:
                    i = self._skip_parameter(i + 2, end)
                else:
                    i = self._skip_parameter_literal(i + 2, end)
                continue
            if nxt == "'":
                i = self._skip_ansi_quote(i + 2, end)
                continue
            i += 1
        return i

    def _skip_single_quote(self, i: int, end: int) -> int:
        close = self.s.find("'", i, end)
        return end if close < 0 else close + 1

    def _skip_ansi_quote(self, i: int, end: int) -> int:
        s = self.s
        while i < end:
            if s[i] == "\\":
                i += 2 if i + 1 < end else 1
            elif s[i] == "'":
                return i + 1
            else:
                i += 1
        return end

    def _skip_double_quote(self, i: int, end: int) -> int:
        s = self.s
        while i < end:
            # Bulk-skip plain content: only '\\', '"', '`' and '$' are special
            # inside double quotes, so a C-level regex search jumps over runs of
            # ordinary characters instead of testing them one at a time.
            m = _DQ_SPECIAL_RE.search(s, i, end)
            if m is None:
                return end
            i = m.start()
            ch = s[i]
            if ch == "\\":
                if i + 1 < end and s[i + 1] in '$`"\\\n':
                    i += 2
                else:
                    i += 1
            elif ch == '"':
                return i + 1
            elif ch == "`":
                close = self._find_backtick_end(i + 1, end)
                self._scan_range(i + 1, close)
                i = close + 1 if close < end else end
            elif s.startswith("$(", i) and not s.startswith("$((", i):
                close = self._find_matching(i + 2, end, ")")
                self._scan_range(i + 2, close if close < end else end)
                i = close + 1 if close < end else end
            elif s.startswith("${", i):
                i = self._skip_parameter(i + 2, end)
            else:
                i += 1
        return end

    def _scan_array_words(self, i: int, end: int) -> None:
        """Scan array literal elements as data words.

        Elements are data, not commands: substitutions inside them are
        scanned as their own command contexts (``_read_word`` handles
        ``$( ... )`` and backquotes), and unquoted words get the same
        Windows path rewrite as ordinary arguments — Bash quote removal
        would otherwise eat their backslashes (``arr=(D:\\x\\y)`` would
        store ``D:xy``).
        """
        s = self.s
        while i < end:
            ch = s[i]
            if ch in " \t\r\n":
                i += 1
                continue
            if ch == "\\" and i + 1 < end and s[i + 1] == "\n":
                i += 2
                continue
            if ch == "#" and self._comment_starts(i, 0):
                newline = s.find("\n", i + 1, end)
                i = end if newline < 0 else newline
                continue
            word_end = self._read_word(i, end)
            if word_end <= i:
                i += 1
                continue
            raw = s[i:word_end]
            replacement = self._path_replacement(raw)
            if replacement is not None:
                self.edits.append((i, word_end, replacement))
                self.path_notes.append(raw)
            i = word_end

    def _scan_expansions(self, i: int, end: int) -> None:
        """Scan executable substitutions in a region whose plain words are data."""
        s = self.s
        search = _EXPANSION_SPECIAL_RE.search
        while i < end:
            m = search(s, i, end)
            if m is None:
                return
            i = m.start()
            ch = s[i]
            if ch == "\\":
                i += 2 if i + 1 < end else 1
            elif ch == "$" and s.startswith("$'", i):
                i = self._skip_ansi_quote(i + 2, end)
            elif ch == "'":
                i = self._skip_single_quote(i + 1, end)
            elif ch == '"':
                i = self._skip_double_quote(i + 1, end)
            elif ch == "`":
                close = self._find_backtick_end(i + 1, end)
                self._scan_range(i + 1, close)
                i = close + 1 if close < end else end
            elif ch == "$" and s.startswith("$(", i) and not s.startswith("$((", i):
                close = self._find_matching(i + 2, end, ")")
                self._scan_range(i + 2, close if close < end else end)
                i = close + 1 if close < end else end
            elif ch == "$" and s.startswith("$((", i):
                i = self._skip_arithmetic(i + 3, end)
            elif ch == "$" and s.startswith("${", i):
                i = self._skip_parameter(i + 2, end)
            else:
                i += 1

    def _scan_heredoc_expansions(self, i: int, end: int) -> None:
        """Scan substitutions in an expanding heredoc body.

        Quote characters are literal in heredoc bodies; only a backslash can
        suppress the expansion introducers that Bash recognizes there.
        """
        s = self.s
        search = _HEREDOC_EXPANSION_SPECIAL_RE.search
        while i < end:
            m = search(s, i, end)
            if m is None:
                return
            i = m.start()
            ch = s[i]
            if ch == "\\":
                i += 2 if i + 1 < end else 1
            elif ch == "`":
                close = self._find_backtick_end(i + 1, end)
                self._scan_range(i + 1, close)
                i = close + 1 if close < end else end
            elif ch == "$" and s.startswith("$(", i) and not s.startswith("$((", i):
                close = self._find_matching(i + 2, end, ")")
                self._scan_range(i + 2, close if close < end else end)
                i = close + 1 if close < end else end
            elif ch == "$" and s.startswith("$((", i):
                i = self._skip_arithmetic(i + 3, end)
            elif ch == "$" and s.startswith("${", i):
                i = self._skip_parameter(i + 2, end)
            else:
                i += 1

    def _skip_conditional(self, i: int, end: int) -> int:
        """Skip a ``[[ ... ]]`` expression while scanning its substitutions."""
        s = self.s
        search = _CONDITIONAL_SPECIAL_RE.search
        while i < end:
            m = search(s, i, end)
            if m is None:
                return end
            i = m.start()
            ch = s[i]
            if ch == "]" and s.startswith("]]", i):
                return i + 2
            if ch == "\\":
                i += 2 if i + 1 < end else 1
            elif ch == "$" and s.startswith("$'", i):
                i = self._skip_ansi_quote(i + 2, end)
            elif ch == "'":
                i = self._skip_single_quote(i + 1, end)
            elif ch == '"':
                i = self._skip_double_quote(i + 1, end)
            elif ch == "`":
                close = self._find_backtick_end(i + 1, end)
                self._scan_range(i + 1, close)
                i = close + 1 if close < end else end
            elif ch == "$" and s.startswith("$(", i) and not s.startswith("$((", i):
                close = self._find_matching(i + 2, end, ")")
                self._scan_range(i + 2, close if close < end else end)
                i = close + 1 if close < end else end
            elif ch == "$" and s.startswith("$((", i):
                i = self._skip_arithmetic(i + 3, end)
            else:
                i += 1
        return end

    def _skip_parameter_literal(self, i: int, end: int) -> int:
        s = self.s
        depth = 1
        search = _PARAMETER_LITERAL_SPECIAL_RE.search
        while i < end:
            m = search(s, i, end)
            if m is None:
                return end
            i = m.start()
            if s[i] == "\\":
                i += 2 if i + 1 < end else 1
            elif s[i] == "'":
                i = self._skip_single_quote(i + 1, end)
            elif s[i] == '"':
                i = self._skip_double_quote_for_matching(i + 1, end)
            elif s[i] == "{":
                depth += 1
                i += 1
            elif s[i] == "}":
                depth -= 1
                i += 1
                if depth == 0:
                    return i
            else:
                i += 1
        return end

    def _skip_parameter(self, i: int, end: int) -> int:
        s = self.s
        depth = 1
        search = _PARAMETER_SPECIAL_RE.search
        while i < end:
            m = search(s, i, end)
            if m is None:
                return end
            i = m.start()
            ch = s[i]
            if ch == "\\":
                i += 2 if i + 1 < end else 1
            elif ch == "$" and s.startswith("$(", i) and not s.startswith("$((", i):
                close = self._find_matching(i + 2, end, ")")
                self._scan_range(i + 2, close if close < end else end)
                i = close + 1 if close < end else end
            elif ch == "'":
                i = self._skip_single_quote(i + 1, end)
            elif ch == '"':
                i = self._skip_double_quote(i + 1, end)
            elif ch == "{":
                depth += 1
                i += 1
            elif ch == "}":
                depth -= 1
                i += 1
                if depth == 0:
                    return i
            else:
                i += 1
        return end

    def _skip_arithmetic(self, i: int, end: int) -> int:
        s = self.s
        depth = 1
        search = _ARITHMETIC_SPECIAL_RE.search
        while i < end:
            m = search(s, i, end)
            if m is None:
                return end
            i = m.start()
            ch = s[i]
            if ch == "$" and s.startswith("$(", i) and not s.startswith("$((", i):
                close = self._find_matching(i + 2, end, ")")
                self._scan_range(i + 2, close if close < end else end)
                i = close + 1 if close < end else end
            elif ch == "(" and s.startswith("((", i):
                depth += 1
                i += 2
            elif ch == ")" and s.startswith("))", i):
                depth -= 1
                i += 2
                if depth == 0:
                    return i
            elif ch == "\\":
                i += 2 if i + 1 < end else 1
            elif ch == "'":
                i = self._skip_single_quote(i + 1, end)
            elif ch == '"':
                i = self._skip_double_quote(i + 1, end)
            else:
                i += 1
        return end

    def _find_backtick_end(self, i: int, end: int) -> int:
        s = self.s
        search = _BACKTICK_SPECIAL_RE.search
        while i < end:
            m = search(s, i, end)
            if m is None:
                return end
            i = m.start()
            if s[i] == "\\":
                i += 2 if i + 1 < end else 1
            else:
                return i
        return end

    def _find_matching(self, i: int, end: int, closing: str) -> int:
        """Find the position of the bracket matching the one at ``i - 2``.

        Recursion is bounded by :data:`_MAX_NESTING_DEPTH`; beyond it the
        region is treated as unmatched so the caller skips it for Bash.
        """
        if i >= end:
            return end
        if self.nest_depth >= _MAX_NESTING_DEPTH:
            return end
        self.nest_depth += 1
        try:
            return self._find_matching_inner(i, end, closing)
        finally:
            self.nest_depth -= 1

    def _find_matching_inner(self, i: int, end: int, closing: str) -> int:
        s = self.s
        depth = 0
        pending_heredocs: list[_BashHereDoc] = []
        case_stack: list[str] = []
        while i < end:
            ch = s[i]
            if ch == "\\":
                i += 2 if i + 1 < end else 1
            elif ch == "\n":
                i += 1
                if pending_heredocs:
                    i = self._skip_heredoc_bodies(
                        i, end, pending_heredocs, scan_expansions=False
                    )
                    pending_heredocs.clear()
            elif ch == "$" and s.startswith("$((", i):
                i = self._skip_arithmetic(i + 3, end)
                if case_stack and case_stack[-1] == "word":
                    # Arithmetic expansion can be the case subject word
                    # (``case $((...)) in ...``); it ends the header just
                    # like a plain subject word.
                    case_stack[-1] = "await-in"
            elif ch == "<" and s.startswith("<<", i) and not s.startswith("<<<", i):
                strip_tabs = s.startswith("<<-", i)
                delimiter_start = i + (3 if strip_tabs else 2)
                while delimiter_start < end and s[delimiter_start] in " \t\r":
                    delimiter_start += 1
                delimiter_end = self._read_word(
                    delimiter_start, end, scan_substitutions=False
                )
                heredoc = self._heredoc_delimiter(s[delimiter_start:delimiter_end])
                if heredoc is not None:
                    delimiter, expands = heredoc
                    pending_heredocs.append(_BashHereDoc(delimiter, strip_tabs, expands))
                i = delimiter_end if delimiter_end > delimiter_start else delimiter_start
            elif ch == "'":
                i = self._skip_single_quote(i + 1, end)
            elif ch == '"':
                i = self._skip_double_quote_for_matching(i + 1, end)
            elif ch == "`":
                close = self._find_backtick_end(i + 1, end)
                i = close + 1 if close < end else end
                if case_stack and case_stack[-1] == "word":
                    # A backquote substitution can be the case subject word
                    # (``case `...` in ...``); it ends the header just like
                    # a plain subject word.
                    case_stack[-1] = "await-in"
            elif ch == "#" and self._comment_starts(i, 0):
                newline = s.find("\n", i + 1, end)
                i = end if newline < 0 else newline
            elif ch == ";" and s.startswith(";;&", i):
                if case_stack:
                    case_stack[-1] = "patterns"
                i += 3
            elif ch == ";" and (s.startswith(";;", i) or s.startswith(";&", i)):
                if case_stack:
                    case_stack[-1] = "patterns"
                i += 2
            elif ch not in _WORD_END_CHARS:
                word_end = self._read_word(i, end, scan_substitutions=False)
                if word_end <= i:
                    i += 1
                    continue
                word = s[i:word_end]
                if word == "case":
                    case_stack.append("word")
                elif (
                    case_stack
                    and case_stack[-1] in {"patterns", "body"}
                    and word == "esac"
                ):
                    case_stack.pop()
                elif case_stack and case_stack[-1] == "word":
                    case_stack[-1] = "await-in"
                elif case_stack and case_stack[-1] == "await-in" and word == "in":
                    case_stack[-1] = "patterns"
                i = word_end
            elif ch == "(":
                depth += 1
                i += 1
            elif ch == closing:
                if case_stack and case_stack[-1] == "patterns":
                    case_stack[-1] = "body"
                    i += 1
                elif depth == 0:
                    return i
                else:
                    depth -= 1
                    i += 1
            else:
                i += 1
        return end

    def _skip_double_quote_for_matching(self, i: int, end: int) -> int:
        s = self.s
        search = _DQ_SPECIAL_RE.search
        while i < end:
            m = search(s, i, end)
            if m is None:
                return end
            i = m.start()
            ch = s[i]
            if ch == "\\":
                if i + 1 < end and s[i + 1] in '$`"\\\n':
                    i += 2
                else:
                    i += 1
            elif ch == '"':
                return i + 1
            elif ch == "`":
                close = self._find_backtick_end(i + 1, end)
                i = close + 1 if close < end else end
            elif s.startswith("$(", i) and not s.startswith("$((", i):
                close = self._find_matching(i + 2, end, ")")
                i = close + 1 if close < end else end
            else:
                i += 1
        return end

    def _read_control_operator(self, i: int, end: int) -> tuple[str, int]:
        s = self.s
        ch = s[i]
        nxt = s[i + 1 : i + 2]
        if ch == ";":
            if nxt == ";":
                if s[i + 2 : i + 3] == "&":
                    return ";;&", i + 3
                return ";;", i + 2
            if nxt == "&":
                return ";&", i + 2
            return ";", i + 1
        if ch == "&":
            if nxt == "&":
                return "&&", i + 2
            return "&", i + 1
        if ch == "|":
            if nxt == "|":
                return "||", i + 2
            if nxt == "&":
                return "|&", i + 2
            return "|", i + 1
        if ch == "(" or ch == ")":
            return ch, i + 1
        return "", i

    def _read_redirection(self, i: int, end: int) -> tuple[str, int]:
        m = _REDIRECTION_RE.match(self.s, i, end)
        if m is None:
            return "", i
        return m.group(), m.end()

    def _redirection_after_fd(self, i: int, end: int) -> bool:
        s = self.s
        while i < end and s[i].isdigit():
            i += 1
        return i < end and s[i] in _REDIRECTION_START

    def _comment_starts(self, i: int, range_start: int) -> bool:
        if i <= range_start:
            return True
        return self.s[i - 1] in " \t\r\n;&|()<>"

    def _empty_parentheses_end(self, i: int, end: int) -> int | None:
        s = self.s
        while i < end and s[i] in " \t\r":
            i += 1
        if i >= end or s[i] != "(":
            return None
        i += 1
        while i < end and s[i] in " \t\r":
            i += 1
        return i + 1 if i < end and s[i] == ")" else None

    def _function_declaration_end(self, raw: str, i: int, end: int) -> int | None:
        # ``_NAME_RE`` requires a leading letter/underscore; that one-char test
        # rejects most words without running the regex.
        if not raw or (raw[0] not in _NAME_START_CHARS):
            return None
        if not _NAME_RE.fullmatch(raw):
            return None
        return self._empty_parentheses_end(i, end)

    @staticmethod
    def _classify_pattern_option(family: str, word: str) -> str | None:
        """Classify an option word of a pattern-consuming command.

        Returns one of:
          ``"data-pending"`` -- the NEXT word is pattern/program data
            (``grep -e``, ``sed -e``, ``awk -F``): suppress the path rewrite;
          ``"path-pending"`` -- the next word is a file path (``grep -f``,
            ``sed -f``, ``awk -f``): the rewrite stays allowed;
          ``"value-pending"`` -- the next word is an arbitrary value
            (context lengths, ...): not a pattern operand, not a path;
          ``"data"`` -- the option word itself carries the data (glued
            ``--regexp=PAT``): suppress the rewrite;
          ``None`` -- an ordinary flag: no operand consumed.
        """
        if family in _GREP_PATTERN_FAMILY:
            if word in {"-e", "--regexp"}:
                return "data-pending"
            if word in {"-f", "--file"}:
                return "path-pending"
            if word.startswith("--regexp="):
                return "data"
            if word.startswith("--file="):
                return "path"
            if word.startswith(("--include", "--exclude")):
                # Glob option (glued ``--include=GLOB`` or separate value).
                return "data"
            if word in _GREP_VALUE_OPTIONS:
                return "value-pending"
            return None
        if family == "sed":
            if word in {"-e", "--expression"}:
                return "data-pending"
            if word in {"-f", "--file"}:
                return "path-pending"
            if word.startswith("--expression="):
                return "data"
            if word.startswith("--file="):
                return "path"
            return None
        # awk family.
        if word in {"-v", "-F", "--assign"}:
            return "data-pending"
        if word in {"-f", "--file"}:
            return "path-pending"
        if word.startswith("--assign="):
            return "data"
        if word.startswith("--file="):
            return "path"
        return None

    def _consume_wrapper_word(self, wrapper: _BashWrapper, raw: str) -> str:
        if wrapper.skip_next:
            wrapper.skip_next = False
            if wrapper.opaque:
                return "inspect"
            return "skip"
        if wrapper.opaque:
            return "inspect"
        # Option matching keys on the word VALUE: quoted spellings
        # (``env $'--'``, ``timeout $'-s'``) are statically the same option
        # the raw-text checks look for, and mistaking ``$'-s'`` for a plain
        # operand derails the wrapper state machine.
        word = self._literal_word_value(raw)
        if word is None:
            word = raw
        if wrapper.operands > 0 and not word.startswith("-"):
            # A plain operand before the command word (GNU ``timeout`` takes
            # exactly one DURATION operand) is wrapper data, not the wrapped
            # command; consume it without ending the wrapper.
            wrapper.operands -= 1
            return "skip"
        if wrapper.kind == "command" and word in {"-v", "-V"}:
            return "inspect"
        if wrapper.kind == "command" and (
            word == "-p"
            or (
                word.startswith("-")
                and not word.startswith("--")
                and "p" in word[1:]
            )
        ):
            wrapper.opaque = True
            return "skip"
        if wrapper.kind == "env" and word in {"-S", "--split-string"}:
            wrapper.opaque = True
            wrapper.skip_next = True
            return "skip"
        if wrapper.kind == "env" and (
            word.startswith("--split-string=")
            or (word.startswith("-S") and word != "-S")
        ):
            return "inspect"
        if word == "--":
            return "skip"
        if word in _WRAPPER_OPTIONS_WITH_VALUE.get(wrapper.kind, ()):
            wrapper.skip_next = True
            if word in _WRAPPER_PATH_OPTIONS.get(wrapper.kind, ()):
                wrapper.path_value = True
            return "skip"
        if word.startswith("-"):
            return "skip"
        if wrapper.kind == "env" and _ASSIGNMENT_RE.match(raw):
            return "skip"
        return "command"

    def _coproc_name_before_compound(self, raw: str, i: int, end: int) -> bool:
        if not _NAME_RE.fullmatch(raw):
            return False
        while i < end and self.s[i] in " \t\r":
            i += 1
        if i >= end:
            return False
        if self.s.startswith(("{", "(", "[[", "(("), i):
            return True
        for keyword in ("case", "for", "if", "select", "until", "while"):
            keyword_end = i + len(keyword)
            if self.s.startswith(keyword, i) and (
                keyword_end >= end
                or self.s[keyword_end] in " \t\r\n;&|()<>{}"
            ):
                return True
        return False

    def _heredoc_delimiter(self, raw: str) -> tuple[str | None, bool] | None:
        if not raw:
            return None
        result: list[str] = []
        quoted = False
        matchable = True
        i = 0
        while i < len(raw):
            ch = raw[i]
            if raw.startswith("$'", i):
                quoted = True
                value, i, valid = self._read_ansi_c_delimiter(raw, i + 2)
                result.append(value)
                matchable = matchable and valid
            elif ch == "'":
                quoted = True
                close = raw.find("'", i + 1)
                if close < 0:
                    result.append(raw[i + 1 :])
                    i = len(raw)
                else:
                    result.append(raw[i + 1 : close])
                    i = close + 1
            elif ch == '"' or raw.startswith('$"', i):
                # A plain ``"..."`` run and the locale form ``$"..."`` both
                # quote the delimiter text (locale translation never changes
                # it on Git Bash); the ``$`` of the locale form is consumed
                # with the opener so the effective delimiter is not prefixed
                # with a stray ``$``.
                quoted = True
                i += 2 if ch != '"' else 1
                while i < len(raw) and raw[i] != '"':
                    if raw[i] == "\\" and i + 1 < len(raw):
                        escaped = raw[i + 1]
                        if escaped in '$`"\\\n':
                            if escaped != "\n":
                                result.append(escaped)
                            i += 2
                            continue
                    result.append(raw[i])
                    i += 1
                if i < len(raw):
                    i += 1
            elif ch == "\\" and i + 1 < len(raw):
                quoted = True
                result.append(raw[i + 1])
                i += 2
            else:
                result.append(ch)
                i += 1
        delimiter = "".join(result) if matchable else None
        return delimiter, not quoted

    def _read_ansi_c_delimiter(
        self, raw: str, i: int
    ) -> tuple[str, int, bool]:
        result: list[str] = []
        valid = True
        # Bash strings are C strings: the delimiter value truncates at the
        # first NUL escape (same rule as _ansi_c_literal_span).  The scan
        # still runs to the closing quote for the correct end index.
        nul_seen = False
        simple = {
            "a": "\a",
            "b": "\b",
            "e": "\x1b",
            "E": "\x1b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "v": "\v",
            "\\": "\\",
            "'": "'",
            '"': '"',
            "?": "?",
        }
        while i < len(raw):
            if raw[i] == "'":
                return "".join(result), i + 1, valid
            if raw[i] != "\\" or i + 1 >= len(raw):
                if not nul_seen:
                    result.append(raw[i])
                i += 1
                continue
            escape = raw[i + 1]
            if escape in simple:
                if not nul_seen:
                    result.append(simple[escape])
                i += 2
                continue
            if escape in "01234567":
                j = i + 1
                while j < len(raw) and j < i + 4 and raw[j] in "01234567":
                    j += 1
                if int(raw[i + 1 : j], 8) == 0:
                    nul_seen = True
                elif not nul_seen:
                    result.append(chr(int(raw[i + 1 : j], 8)))
                i = j
                continue
            if escape in "xXuU":
                widths = {"x": 2, "X": 2, "u": 4, "U": 8}
                j = i + 2
                limit = min(len(raw), j + widths[escape])
                while j < limit and raw[j] in "0123456789abcdefABCDEF":
                    j += 1
                if j > i + 2:
                    value = int(raw[i + 2 : j], 16)
                    if value == 0:
                        nul_seen = True
                    elif value <= 0x10FFFF and not 0xD800 <= value <= 0xDFFF:
                        if not nul_seen:
                            result.append(chr(value))
                    else:
                        # Bash accepts byte sequences outside Python's Unicode
                        # scalar range.  Python text cannot represent the same
                        # delimiter, so mark it unmatchable and conservatively
                        # keep all remaining source inside the heredoc.
                        valid = False
                        result.append(raw[i:j])
                    i = j
                    continue
            if not nul_seen:
                result.append("\\" + escape)
            i += 2
        return "".join(result), i, valid

    def _skip_heredoc_bodies(
        self,
        i: int,
        end: int,
        documents: list[_BashHereDoc],
        *,
        scan_expansions: bool = True,
    ) -> int:
        s = self.s
        redir_line_end = -1
        for document in documents:
            body_start = i
            if redir_line_end < 0:
                redir_line_end = body_start - 1
            logical_line = ""
            logical_start = i
            while i < end:
                newline = s.find("\n", i, end)
                line_end = end if newline < 0 else newline
                # A CRLF source leaves a trailing ``\r`` on every line; strip it
                # so the terminator line matches the delimiter byte-for-byte
                # (otherwise the closing ``EOF\r`` never ends the heredoc and
                # the trailing-operator repair never runs).  The stored body is
                # not mutated — this only feeds the delimiter/continuation test.
                line = s[i:line_end].rstrip("\r")
                compare = line.lstrip("\t") if document.strip_tabs else line
                if not logical_line:
                    logical_start = i
                if document.expands and self._heredoc_line_continues(compare):
                    logical_line += compare[:-1]
                    i = end if newline < 0 else newline + 1
                    continue
                logical_line += compare
                if (
                    document.delimiter is not None
                    and logical_line == document.delimiter
                ):
                    if scan_expansions and document.expands:
                        self._scan_heredoc_expansions(body_start, logical_start)
                    i = end if newline < 0 else newline + 1
                    break
                logical_line = ""
                i = end if newline < 0 else newline + 1
            else:
                if scan_expansions and document.expands:
                    self._scan_heredoc_expansions(body_start, end)
        if redir_line_end >= 0:
            self.heredoc_events.append((redir_line_end, i))
        return i

    @staticmethod
    def _heredoc_line_continues(line: str) -> bool:
        trailing = len(line) - len(line.rstrip("\\"))
        return trailing % 2 == 1

    # ── Windows path normalization for Git Bash ─────────────────────────────

    def _drop_cmd_cd_flag(self, i: int, end: int) -> None:
        """Drop the cmd.exe-only ``cd /d <path>`` flag form.

        Bash ``cd`` accepts a single argument, so ``cd /d D:\\x`` fails with
        "too many arguments".  The flag is deleted only when a path argument
        actually follows it on the same line; bare ``cd /d`` stays untouched.
        """
        s = self.s
        j = i
        while j < end and s[j] in " \t\r":
            j += 1
        if j >= end:
            return
        flag_end = self._read_word(j, end, scan_substitutions=False)
        if flag_end <= j or s[j:flag_end] not in {"/d", "/D"}:
            return
        k = flag_end
        while k < end and s[k] in " \t\r":
            k += 1
        if k >= end or s[k] in _OPERATOR_CHARS or s[k] == "#":
            return
        if s[k].isdigit() and self._redirection_after_fd(k, end):
            # ``cd /d 2>/dev/null``: an fd-prefixed redirection is not a path
            # argument; the flag is dropped only when a path follows it.
            return
        self.edits.append((j, flag_end, ""))
        self.path_notes.append("cd /d")

    def _windows_path_replacement(self, raw: str) -> str | None:
        """Return the Git Bash spelling of a Windows backslash path word.

        Only unquoted words are considered: quoted text is literal data that
        may carry regexes or tool-level escape sequences.  The word must look
        unambiguously like a Windows path (drive letter, UNC share, root- or
        home-relative, dot-relative, or a multi-segment relative path); short
        ambiguous words such as ``a\nb`` and ``foo\bar`` are left for Bash
        to handle.  An anchored path with mixed separators (``D:/x\\y``,
        ``~/x\\y``, ``./x\\y``) is still unambiguous — the anchor commits it —
        and must be rewritten: outside quotes Bash removes a backslash before
        any character, so a lone backslash left in place would silently
        collapse two segments into one name.  Words spanning a backslash-newline line continuation are
        also left untouched: injecting the line break into a rewritten word
        would change the command's line structure.
        """
        if not raw or "\\" not in raw:
            return None
        backslashes = 0
        for ch in raw:
            if ch == "\\":
                backslashes += 1
            elif ch in "'\"`$\n\r":
                return None
        if _PATH_DRIVE_RE.fullmatch(raw):
            pass
        elif raw.startswith("\\\\") and len(raw) > 2:
            pass
        elif raw.startswith("\\") and not raw.startswith("\\\\") and backslashes >= 2:
            # Root-relative paths are not anchored like ``D:\...``: an unquoted
            # word such as ``\a\b`` or ``\033\015`` is far more likely to be a
            # Bash escape sequence than a path, so the segments must look like
            # real directory names before the rewrite happens.
            if not self._plausible_path_segments(raw):
                return None
        elif raw.startswith("~\\") or raw.startswith("~/"):
            # ``~/x\y`` (mixed separators) is just as anchored as ``~\x\y``;
            # leaving it untouched would let Bash eat the lone backslash and
            # collapse two path segments into one name.
            pass
        elif (
            raw.startswith(".\\")
            or raw.startswith("./")
            or raw.startswith("..\\")
            or raw.startswith("../")
        ):
            pass
        elif backslashes >= 2:
            decoded = self._decode_unquoted_word(raw)
            if (
                len(decoded) < 2
                or not any(ch.isalnum() for ch in decoded)
                or not _PATH_SEGMENT_RE.fullmatch(decoded)
                or not self._plausible_path_segments(raw)
            ):
                return None
        else:
            return None
        return self._quote_path_word(self._normalize_windows_path(raw))

    def _git_bash_abs_path_replacement(self, raw: str) -> str | None:
        """Return the native Windows spelling of a Git Bash virtual absolute path.

        Git Bash accepts POSIX-style absolute paths that native Windows
        executables cannot resolve.  ``/tmp/x`` maps to the user's Windows temp
        directory (``cygpath -w /tmp``), while a native tool would read it as
        ``<current-drive>:\\tmp\\x``; ``/c/x`` means ``C:/x``, but a native
        tool would read it as ``<current-drive>:\\c\\x``.  Rewriting these
        words to their real Windows spellings makes the same command behave
        identically under Git Bash and native POSIX bash: ``/tmp`` is the
        system temp directory in both, and ``/c/...``/``/d/...`` resolve to
        same files whether the consumer is an MSYS tool or a native
        executable.  Only unquoted words are considered; quoted text is
        literal data.  A bare single-letter mount (``/c``, ``/d``) is left
        untouched so cmd.exe's ``cd /d <path>`` flag is never mistaken for a
        drive path.

        Unquoted ``$``-expansions inside the word (``$var``, ``${...}``,
        ``$((...))``, ``$'...'``, special parameters) are preserved verbatim
        and outside any quotes by :meth:`_quote_virtual_path`: quoting or
        escaping the ``$`` would turn Bash's runtime parameter expansion into
        a literal ``$`` and the tool would receive a path literally named
        ``x_${b}``.  Words embedding a command substitution (``$(...)`` or a
        backquote) are left untouched, because the substitution's inner
        command context has already been scanned and a whole-word rewrite
        would clash with the edits recorded there.
        """
        if not raw:
            return None
        if raw.startswith("/tmp"):
            if raw != "/tmp" and not raw.startswith("/tmp/"):
                return None
            return self._quote_virtual_path(_windows_temp_dir() + raw[4:])
        if len(raw) >= 3 and raw[0] == "/" and raw[1].isalpha() and raw[2] == "/":
            return self._quote_virtual_path(raw[1].upper() + ":" + raw[2:])
        return None

    def _quote_virtual_path(self, normalized: str) -> str | None:
        """Quote a rewritten virtual path without disabling shell expansions.

        Literal spans are quoted only when unquoted emission would break
        them (same policy as :meth:`_quote_path_word`); expansion spans are
        spliced back in verbatim so the rewritten word expands at runtime
        exactly like the original unquoted word.  Returns None when the word
        embeds a command substitution or an unterminated expansion — the
        caller then leaves the whole word untouched.
        """
        spans = _split_virtual_path_expansions(normalized)
        if spans is None:
            return None
        out: list[str] = []
        for is_expansion, span in spans:
            if is_expansion:
                out.append(span)
                continue
            decoded = _decode_virtual_path_span(span)
            if decoded is None:
                # Escaped glob metacharacter or unterminated continuation:
                # leave the whole word untouched so Bash keeps the original
                # pathname-expansion behavior.
                return None
            out.append(self._quote_path_word(decoded))
        return "".join(out)

    def _path_replacement(self, raw: str) -> str | None:
        """Return the Git Bash spelling for a Windows path word.

        Covers both spellings agents use on Windows: native backslash paths
        (``D:\\repo\\src``) and Git Bash virtual POSIX absolute paths
        (``/tmp/x``, ``/c/x``).  Returns ``None`` when *raw* is not an
        unambiguous path word.
        """
        # Fast reject: a word without a backslash and not starting with "/"
        # can be neither a Windows path nor a Git Bash virtual path.
        if not raw or ("\\" not in raw and raw[0] != "/"):
            return None
        replacement = self._windows_path_replacement(raw)
        if replacement is not None:
            return replacement
        return self._git_bash_abs_path_replacement(raw)

    @staticmethod
    def _plausible_path_segments(raw: str) -> bool:
        """Require at least one segment that looks like a real directory name.

        Bash escape sequences are written with single-letter backslash escapes
        (``\\a``, ``\\n``, ``\\t``, ``\\x``) or pure-digit octal/hex bodies
        (``\\033``, ``\\015``), so words built only from one-character or
        digit-led segments (``\\a\\b``, ``\\033\\015``, ``x\\n\\t``) stay
        ambiguous and are preserved byte-for-byte.  A segment of at least two
        characters starting with a letter (``Users``, ``build``, ``Program``)
        marks the word as a genuine path.
        """
        return any(
            len(segment) >= 2 and segment[0].isalpha()
            for segment in raw.split("\\")
        )

    @staticmethod
    def _decode_unquoted_word(raw: str) -> str:
        """Return the word value after Bash quote removal (unquoted form)."""
        value: list[str] = []
        i = 0
        while i < len(raw):
            ch = raw[i]
            if ch == "\\" and i + 1 < len(raw):
                value.append(raw[i + 1])
                i += 2
            else:
                value.append(ch)
                i += 1
        return "".join(value)

    @staticmethod
    def _normalize_windows_path(raw: str) -> str:
        """Rewrite backslashes as the forward slashes Git Bash understands.

        A leading ``\\\\`` UNC prefix becomes ``//``; a backslash before a
        char from :data:`_ESCAPED_LITERAL_CHARS` is a pure Bash escape (the
        char belongs inside its segment, e.g. ``\\ `` is a space); every other
        backslash separates segments and becomes ``/``.
        """
        out: list[str] = []
        i = 0
        n = len(raw)
        if n >= 2 and raw.startswith("\\\\"):
            out.append("//")
            i = 2
        while i < n:
            ch = raw[i]
            if ch == "\\" and i + 1 < n:
                nxt = raw[i + 1]
                if nxt == "\\":
                    out.append("/")
                elif nxt in _ESCAPED_LITERAL_CHARS:
                    out.append(nxt)
                else:
                    out.append("/")
                    out.append(nxt)
                i += 2
            elif ch == "\\":
                out.append("/")
                i += 1
            else:
                out.append(ch)
                i += 1
        return "".join(out)

    def _quote_path_word(self, normalized: str) -> str:
        """Quote a normalized path only when unquoted emission would break it.

        Safe characters (including glob metacharacters, so ``D:/x/*.txt``
        keeps performing pathname expansion) pass through untouched.  A
        leading ``~`` stays outside the quotes so tilde expansion still
        applies to it — including the slash: ``~"x"`` is a literal tilde
        (a quote stops tilde expansion), so a home-relative path that needs
        quoting is emitted as ``~/"x"``.
        """
        if all(ch in _PATH_SAFE_CHARS for ch in normalized):
            return normalized
        if normalized.startswith("~"):
            # Keep the slash outside the quotes: ``~"..."`` does NOT expand
            # (a tilde followed by a quote is a literal tilde), but
            # ``~/"..."`` expands the home directory and concatenates.
            rest = normalized[1:]
            if rest.startswith("/"):
                return "~/" + self._quote_path_word(rest[1:])
            return "~" + self._quote_path_word(rest)
        # Quote only the spans that need it — and never swallow a glob or
        # brace metacharacter into the quotes: doing so would turn the live
        # expansion the original unquoted word performed into a fixed
        # literal name (``ls D:\a\ b\*.txt`` must keep expanding ``*.txt``).
        out: list[str] = []
        buf: list[str] = []

        def _flush() -> None:
            if not buf:
                return
            span = "".join(buf)
            buf.clear()
            if all(ch in _PATH_SAFE_CHARS for ch in span):
                out.append(span)
                return
            escaped = (
                span.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("$", "\\$")
                .replace("`", "\\`")
            )
            out.append('"' + escaped + '"')

        for ch in normalized:
            if ch in _PATH_GLOB_CHARS:
                _flush()
                out.append(ch)
            else:
                buf.append(ch)
        _flush()
        return "".join(out)


def bash_compatibility_prelude() -> str:
    """Return exported fallback definitions for a persistent Git Bash shell.

    Interactive input can be an incomplete Bash fragment (for example a
    heredoc body or the second half of a quote), so it must never be scanned
    and prefixed independently.  The interactive shell instead executes this
    prelude once and exports the fallback functions across ``exec bash -i``.
    """
    if sys.platform != "win32":
        return ""
    definitions = "\n".join(_FALLBACKS.values())
    exports = "\n".join(
        f"if declare -F {name} >/dev/null; then export -f {name}; fi"
        for name in _FALLBACKS
    )
    return definitions + "\n" + exports


_HEREDOC_TRAILING_OPERATORS = frozenset({"&&", "||", "|", "|&", ";", "&"})


def _read_shell_control_operator(s: str, i: int, n: int) -> tuple[str, int]:
    """Return the control operator at position *i* and the index after it."""
    if i >= n:
        return "", i
    ch = s[i]
    if ch == ";":
        if s.startswith(";;&", i):
            return ";;&", i + 3
        if s.startswith(";;", i):
            return ";;", i + 2
        if s.startswith(";&", i):
            return ";&", i + 2
        return ";", i + 1
    if ch == "&":
        if s.startswith("&&", i):
            return "&&", i + 2
        return "&", i + 1
    if ch == "|":
        if s.startswith("||", i):
            return "||", i + 2
        if s.startswith("|&", i):
            return "|&", i + 2
        return "|", i + 1
    return "", i


def _apply_heredoc_operator_move(
    source: str, redir_line_end: int, terminator_end: int
) -> str:
    """Move a control-operator line following a heredoc terminator to the redirection line.

    Bash requires a control operator that continues a heredoc-delimited command
    to appear on the same line as the ``<<`` redirection.  A common model
    mistake is to place the operator on the line after the closing delimiter,
    which produces ``syntax error near unexpected token `&&'``.  This helper
    repairs that pattern while leaving the heredoc body and delimiter intact.
    """
    n = len(source)
    if redir_line_end < 0 or redir_line_end >= n or source[redir_line_end] != "\n":
        return source
    if terminator_end < 0 or terminator_end > n:
        return source

    i = terminator_end
    while i < n:
        ch = source[i]
        if ch in " \t\r":
            i += 1
            continue
        if ch == "\n":
            i += 1
            continue
        if ch == "#":
            nl = source.find("\n", i, n)
            i = n if nl < 0 else nl + 1
            continue
        break
    if i >= n:
        return source

    op, op_end = _read_shell_control_operator(source, i, n)
    if op not in _HEREDOC_TRAILING_OPERATORS:
        return source
    # ``&>`` / ``&>>`` are redirections, not list terminators.
    if op == "&" and op_end < n and source[op_end] == ">":
        return source

    move_start = i
    line_end = source.find("\n", i, n)
    if line_end < 0:
        line_end = n
        move_end = n
    else:
        move_end = line_end + 1

    rest = source[op_end:line_end].lstrip(" \t\r")
    if not rest or rest.startswith("#"):
        k = move_end
        while k < n:
            if source[k] in " \t\r":
                k += 1
                continue
            if source[k] == "\n":
                k += 1
                continue
            if source[k] == "#":
                nl = source.find("\n", k, n)
                k = n if nl < 0 else nl + 1
                continue
            break
        if k >= n:
            return source
        next_line_end = source.find("\n", k, n)
        if next_line_end < 0:
            move_end = n
        else:
            move_end = next_line_end + 1

    # A trailing backslash continues the logical command on the following
    # physical lines; the continuation must move with its line, or the
    # orphaned tail is left behind and read as heredoc body.
    while move_end < n:
        prev_nl = source.rfind("\n", move_start, move_end - 1)
        last_line = source[prev_nl + 1 : move_end - 1]
        if not _BashFixScanner._heredoc_line_continues(last_line):
            break
        next_end = source.find("\n", move_end, n)
        move_end = n if next_end < 0 else next_end + 1

    # A list may continue over SEVERAL physical operator lines (the model can
    # write ``&& echo A`` on one line and ``&& echo B`` on the next, both after
    # the closing delimiter).  The backslash loop above only carries a single
    # logical line; absorb each following operator-led line as well, or it is
    # left orphaned after the terminator and bash reports
    # ``syntax error near unexpected token `&&'``.
    while move_end < n:
        j = move_end
        while j < n:
            cj = source[j]
            if cj in " \t\r":
                j += 1
                continue
            if cj == "\n":
                j += 1
                continue
            if cj == "#":
                nl2 = source.find("\n", j, n)
                j = n if nl2 < 0 else nl2 + 1
                continue
            break
        if j >= n:
            break
        op2, op2_end = _read_shell_control_operator(source, j, n)
        if op2 not in _HEREDOC_TRAILING_OPERATORS:
            break
        if op2 == "&" and op2_end < n and source[op2_end] == ">":
            break
        cand_nl = source.find("\n", j, n)
        move_end = n if cand_nl < 0 else cand_nl + 1
        # carry this operator line's own backslash continuations too
        while move_end < n:
            pl = source.rfind("\n", j, move_end - 1)
            ll = source[pl + 1 : move_end - 1]
            if not _BashFixScanner._heredoc_line_continues(ll):
                break
            ne = source.find("\n", move_end, n)
            move_end = n if ne < 0 else ne + 1

    lines = source[move_start:move_end].splitlines()

    # Closing delimiters that belong to a construct opened BEFORE the
    # redirection line (a command substitution ``$(`` or backtick, a subshell
    # ``(``, a brace group ``{``) must NOT ride up onto the redirection line:
    # doing so closes the construct before the heredoc body, ejects the body
    # lines into the outer command list, and bash reports an unterminated
    # here-document.  The excess closers — and everything after them — stay
    # in place after the terminator (e.g. ``x=$(cat <<E | wc -l`` + body +
    # ``E`` + ``); echo q``).  The stay suffix may sit mid-line (``| wc -l);
    # echo q``) or after balanced groups inside the moved text (``| (a); b)``;
    # only the closers exceeding the moved text's own openers belong outside).
    # Guards are naive character counts; the prefix guard (an unclosed opener
    # before the redirection line) keeps ordinary operator lines such as
    # ``| (wc -l)`` moving whole.
    stay = ""
    if lines:
        moved_text = "\n".join(lines)
        prefix = source[:redir_line_end]
        stay_start: int | None = None
        paren_excess = moved_text.count(")") - moved_text.count("(")
        if paren_excess > 0 and prefix.count("(") > prefix.count(")"):
            # The suffix from the last excess ``)`` onward stays: it is the
            # construct closer (any ``)`` beyond the moved text's own openers
            # must close a prefix opener).
            target = moved_text.count("(") + paren_excess
            idx = -1
            for _ in range(target):
                idx = moved_text.find(")", idx + 1)
            if idx >= 0 and (stay_start is None or idx < stay_start):
                stay_start = idx
        if moved_text.count("`") % 2 == 1 and prefix.count("`") % 2 == 1:
            idx = moved_text.rfind("`")
            if idx >= 0 and (stay_start is None or idx < stay_start):
                stay_start = idx
        brace_excess = moved_text.count("}") - moved_text.count("{")
        if brace_excess > 0 and prefix.count("{") > prefix.count("}"):
            target = moved_text.count("{") + brace_excess
            idx = -1
            for _ in range(target):
                idx = moved_text.find("}", idx + 1)
            if idx >= 0 and (stay_start is None or idx < stay_start):
                stay_start = idx
        if stay_start is not None:
            stay = moved_text[stay_start:]
            lines = moved_text[:stay_start].splitlines() or [""]
            # If the first stay line is only construct closers and the next
            # stay line starts with a control operator, joining them with a
            # space is required: an operator-led physical line after a
            # completed command is a bash syntax error (``cmd \n && next``).
            stay_lines = stay.split("\n")
            if (
                len(stay_lines) > 1
                and not stay_lines[0].strip(" \t\r").strip(")`}")
                and stay_lines[1].lstrip(" \t\r")
            ):
                nxt = stay_lines[1]
                lead = len(nxt) - len(nxt.lstrip(" \t\r"))
                op2, _op2_end = _read_shell_control_operator(
                    nxt, lead, len(nxt)
                )
                if op2 in _HEREDOC_TRAILING_OPERATORS and not (
                    op2 == "&" and nxt[lead + len(op2) :].startswith(">")
                ):
                    stay = " ".join(
                        [stay_lines[0].rstrip(" \t\r")]
                        + [
                            ln.strip(" \t\r")
                            for ln in stay_lines[1:]
                            if ln.strip(" \t\r")
                        ]
                    )

    parts: list[str] = []
    if lines:
        parts.append(lines[0][len(op):].strip())
        parts.extend(line.strip() for line in lines[1:])
    # Re-join stripped parts, but keep a backslash line-continuation as a real
    # newline: splicing the next line on with a space would turn ``\`` + NL
    # into ``\ `` (an escaped space), corrupting the continued command.
    segments: list[str] = []
    for part in parts:
        if not part or part.startswith("#"):
            continue
        if segments:
            prev = segments[-1]
            trailing = len(prev) - len(prev.rstrip("\\"))
            segments.append(("\n" if trailing % 2 == 1 else " ") + part)
        else:
            segments.append(part)
    joined = "".join(segments)
    moved = f"{op} {joined}\n" if joined else f"{op}\n"

    return (
        source[:redir_line_end]
        + " "
        + moved
        + source[redir_line_end + 1 : move_start]
        + (stay + "\n" if stay else "")
        + source[move_end:]
    )


def _fix_heredoc_trailing_operators(
    source: str, events: list[tuple[int, int]] | None = None
) -> str:
    """Repair heredoc commands whose trailing control operator is on the wrong line.

    *events* may carry the ``(redir_line_end, terminator_end)`` pairs already
    collected by a scan of *source*; when omitted the source is scanned here.
    """
    if events is None:
        try:
            scanner = _BashFixScanner(source)
            scanner._scan_range(0, scanner.n)
        except RecursionError:
            return source
        events = scanner.heredoc_events
    if not events:
        return source
    for redir_line_end, terminator_end in reversed(events):
        source = _apply_heredoc_operator_move(source, redir_line_end, terminator_end)
    return source


def fix_bash_command(command: str) -> BashFix:
    """Rewrite selected native POSIX commands for Windows Git Bash.

    The Windows-platform gate lives in the app layer
    (``kimi_cli.tools.file.bash.bash_fix.fix_bash_command``); this scanner is
    byte-identical to the native kernel, which also does not gate on platform.
    Empty input is returned byte-for-byte unchanged.
    """
    if not command:
        return BashFix(command)
    # Quoting and escaping can form a literal command name without the source
    # containing it contiguously (for example ``r""ev`` or ``\rev``), so a
    # substring fast path would miss legal executable words.  The scanner is
    # linear and exits without allocating generated shell code when unchanged.
    scanner = _BashFixScanner(command)
    result = scanner.fix()
    if result.command == command:
        # ``fix()`` scanned the source verbatim: reuse its heredoc events
        # instead of scanning the same string a second time.
        fixed = _fix_heredoc_trailing_operators(command, scanner.heredoc_events)
    else:
        fixed = _fix_heredoc_trailing_operators(result.command)
    return BashFix(
        fixed,
        result.replacements,
        result.path_changes,
        result.shell_wrappers,
        result.nul_fixes,
        result.unsupported,
    )


# ======================================================================
# bash_tool.py (kimi-agent) - _process_unquoted + helpers
# ======================================================================
_BASH_METACHARACTERS = frozenset("()|;&<>$\"`'\"*?[]{}~!#=% \t\n\r")

# In double quotes, \ only escapes these characters.  $ and ` are included
# because \$, \` inside "..." are literal (the $ / ` is escaped, not triggering
# variable expansion or command substitution).
_DQ_ESCAPED = frozenset(('"', '\\', '$', '`'))

# Precompiled regex for finding the next special character in unquoted mode.
# Matches backslash, single quote, double quote, dollar, or backtick.
_UNQUOTED_SPECIAL_RE = re.compile(r'[\\\'"$`]')
# A word that is an escaped/interleaved spelling of the Windows null device
# (``\nul``, ``n\ul``, ``nu\l``, any case): the backslash(s) are quote
# removal, so the word's value is exactly ``nul``.  ``_process_unquoted``
# preserves the escape pairs so ``fix_bash_command`` can see the effective
# word and rewrite the whole token to ``/dev/null``; converting the
# backslashes to ``/`` first would corrupt the word (``/nul`` is an absolute
# path, not the null device).
_ESCAPED_NUL_RE = re.compile(
    # The escaped word must be a *complete* word: it starts at a word
    # boundary and ends at one, so a real Windows path (``C:\\nul``) or a
    # longer escaped word (``a\\nul\\b``) keeps its normal backslash
    # conversion.
    r"(?:(?<=^)|(?<=[\s;|&<>(){}]))"
    r"\\?[nN]\\?[uU]\\?[lL]"
    r"(?![A-Za-z0-9_.\\])"
)


def _find_ansi_c_end(cmd: str, start: int) -> int:
    """Return the index AFTER the closing ' of a ``$'...'`` region.

    ``start`` is the position right after the opening ``$'`` (i.e. the first
    character inside the region).  Returns ``-1`` if the region is
    unterminated.  Inside ``$'...'`` every ``\\X`` pair is treated as an
    escape (any character after \\ is skipped over).
    """
    i = start
    length = len(cmd)
    while i < length:
        c = cmd[i]
        if c == "\\" and i + 1 < length:
            i += 2
        elif c == "'":
            return i + 1
        else:
            i += 1
    return -1


def _find_backtick_end(cmd: str, start: int) -> int:
    """Return the index AFTER the closing `` ` `` of a backtick region.

    ``start`` is the position right after the opening `` ` ``.
    Returns ``-1`` if the region is unterminated.  ``\\` `` inside the
    region is an escaped backtick (literal `` ` ``).
    """
    i = start
    length = len(cmd)
    while i < length:
        c = cmd[i]
        if c == "\\" and i + 1 < length:
            i += 2  # skip escaped char (including \`)
        elif c == "`":
            return i + 1
        else:
            i += 1
    return -1


def _find_matching_paren(cmd: str, open_pos: int) -> int:
    """Return the index of the ``)`` matching the ``(`` at ``cmd[open_pos]``.

    Returns ``-1`` if no matching ``)`` is found.  Tracks nested ``$(...)``,
    single-quoted regions, double-quoted regions (including their own
    nested ``$(...)`` and backticks), and backtick regions.
    """
    assert cmd[open_pos] == "("
    depth = 1
    i = open_pos + 1
    length = len(cmd)
    while i < length:
        c = cmd[i]
        if c == "'":
            end = cmd.find("'", i + 1)
            if end == -1:
                return -1
            i = end + 1
        elif c == '"':
            i = _find_dq_end(cmd, i + 1)
            if i == -1:
                return -1
        elif c == "`":
            i = _find_backtick_end(cmd, i + 1)
            if i == -1:
                return -1
        elif c == "$" and i + 1 < length and cmd[i + 1] == "(":
            depth += 1
            i += 2
        elif c == "$" and i + 1 < length and cmd[i + 1] == "'":
            # $'...' ANSI-C quoted region — skip to its closing '
            end = _find_ansi_c_end(cmd, i + 2)
            if end == -1:
                return -1
            i = end
        elif c == "(":
            # A plain parenthesis opens a subshell group: it must be balanced
            # by its own ``)`` before the substitution can close (``$( (ls)
            # ; cat x )``).  Without this the matcher closed the substitution
            # at the group's ``)`` and everything after the group was dropped
            # from the scan.
            depth += 1
            i += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
            i += 1
        else:
            i += 1
    return -1


def _find_dq_end(cmd: str, start: int) -> int:
    """Return the index AFTER the closing ``"`` of a double-quoted region.

    ``start`` is the position right after the opening ``"``.
    Returns ``-1`` if the region is unterminated.  Recognises ``\\X``
    escapes (``X`` in ``_DQ_ESCAPED``), nested ``$(...)``, ``$'...'``, and
    backtick command substitutions inside the region.
    """
    i = start
    length = len(cmd)
    while i < length:
        c = cmd[i]
        if c == "\\" and i + 1 < length and cmd[i + 1] in _DQ_ESCAPED:
            i += 2  # skip \X (X is escaped: ", \, $, `)
        elif c == '"':
            return i + 1
        elif c == "$" and i + 1 < length and cmd[i + 1] == "(":
            end = _find_matching_paren(cmd, i + 1)
            if end == -1:
                return -1
            i = end + 1
        elif c == "$" and i + 1 < length and cmd[i + 1] == "'":
            # Inside "..." bash does NOT start an ANSI-C quoting region at
            # $' (ANSI-C only applies outside double quotes): both characters
            # are ordinary literals here.  Treat the $ as ordinary so a stray
            # or unbalanced ' cannot derail the closing-quote search (which
            # would previously discard the whole region verbatim and lose
            # required rewrites of a real $(...) inside it).
            i += 1
        elif c == "`":
            end = _find_backtick_end(cmd, i + 1)
            if end == -1:
                return -1
            # _find_backtick_end returns the index AFTER the closing `
            i = end
        else:
            i += 1
    return -1


def _process_unquoted(cmd: str) -> str:
    """Convert unquoted backslashes to forward slashes in ``cmd``.

    Walks the string in *unquoted mode* (the same rules that apply at the
    top level of a bash command): a bare ``\\`` followed by a non-metachar
    is converted to ``/``, while ``\\`` followed by a bash metacharacter,
    or ``\\`` inside single / double / ANSI-C quotes, is preserved.

    The function also descends into ``$(...)`` and backtick command
    substitutions, processing their *content* in unquoted mode as well
    (because bash runs the content of ``$(...)`` and `` ` ` `` in a
    subshell where it is parsed unquoted — even when the substitution is
    itself nested inside ``"..."``).
    """
    result: list[str] = []
    i = 0
    length = len(cmd)
    # Word spans that are escaped spellings of ``nul`` (see _ESCAPED_NUL_RE);
    # backslashes inside them are preserved, not converted to ``/``.
    _escaped_nul_spans = (
        [m.span() for m in _ESCAPED_NUL_RE.finditer(cmd)]
        if "\\" in cmd
        else []
    )

    while i < length:
        # ---- find the next special character ----
        # Use a single regex search (C-accelerated) to bulk-skip non-special chars.
        m = _UNQUOTED_SPECIAL_RE.search(cmd, i)
        if m:
            nxt = m.start()
            if nxt > i:
                result.append(cmd[i:nxt])
                i = nxt
        else:
            # No more special characters — append the remaining suffix and finish.
            result.append(cmd[i:])
            break

        if i >= length:
            break

        char = cmd[i]

        if char == "'":
            # Single-quoted region — copy literally until closing '
            end = cmd.find("'", i + 1)
            if end == -1:
                result.append(cmd[i:])
                break
            result.append(cmd[i : end + 1])
            i = end + 1

        elif char == '"':
            # Double-quoted region.  First find the end of the region,
            # then walk through it and convert the *content* of any
            # $(...) and `...` sub-regions using unquoted-mode rules
            # (bash runs command substitutions in a subshell where the
            # content is parsed unquoted, so backslashes inside must be
            # converted to '/' just like at the top level).
            dq_end = _find_dq_end(cmd, i + 1)
            if dq_end == -1:
                # Unterminated — copy the rest verbatim
                result.append(cmd[i:])
                break
            j = i + 1
            chunk_start = i
            while j < dq_end:
                # Bulk-skip to the next interesting character inside DQ:
                # backslash, dollar, or backtick.
                m2 = _UNQUOTED_SPECIAL_RE.search(cmd, j, dq_end)
                if m2:
                    nxt2 = m2.start()
                    if nxt2 > j:
                        j = nxt2
                else:
                    # No more special chars inside DQ — rest is verbatim
                    j = dq_end
                    break

                c = cmd[j]
                if c == "\\" and j + 1 < dq_end and cmd[j + 1] in _DQ_ESCAPED:
                    # \X inside DQ: X is escaped.  Skip the pair; it will
                    # be included in the next emitted chunk.
                    j += 2
                elif c == "$" and j + 1 < dq_end and cmd[j + 1] == "(":
                    # $(...) command substitution — process content
                    paren_end = _find_matching_paren(cmd, j + 1)
                    if paren_end == -1 or paren_end >= dq_end:
                        # Unterminated or mismatched — treat rest as verbatim
                        j = dq_end
                        break
                    result.append(cmd[chunk_start:j])
                    result.append("$(")
                    result.append(_process_unquoted(cmd[j + 2 : paren_end]))
                    result.append(")")
                    j = paren_end + 1
                    chunk_start = j
                elif c == "$" and j + 1 < dq_end and cmd[j + 1] == "'":
                    # A literal ``$'`` inside double quotes: bash does NOT
                    # start an ANSI-C quoting region there (ANSI-C only applies
                    # outside ``"..."``), so the ``$`` and the following ``'``
                    # are ordinary literal characters that belong to the chunk.
                    # Advancing by one leaves both to be copied verbatim and,
                    # crucially, keeps scanning for a *real* ``$(...)`` /
                    # backtick substitution that may sit between the ``$'``
                    # and the next ``'`` (such a substitution is active inside
                    # double quotes and its content must still be walked).  A
                    # naive ANSI-C skip would swallow it and lose the required
                    # backslash-to-slash rewrite inside the substitution.
                    j += 1
                elif c == "`":
                    # Backtick command substitution — process content
                    bt_end = _find_backtick_end(cmd, j + 1)
                    if bt_end == -1 or bt_end > dq_end:
                        # Unterminated or extends beyond DQ — treat rest as verbatim
                        j = dq_end
                        break
                    result.append(cmd[chunk_start:j])
                    result.append("`")
                    result.append(_process_unquoted(cmd[j + 1 : bt_end - 1]))
                    result.append("`")
                    j = bt_end
                    chunk_start = j
                else:
                    # Should not reach here — char is not one we handle in DQ
                    j += 1
            # Emit the final chunk (up to and including the closing ")
            result.append(cmd[chunk_start:dq_end])
            i = dq_end

        elif char == "$" and i + 1 < length and cmd[i + 1] == "'":
            # $'...' ANSI-C quoted region at top level — copy literally
            ac_end = _find_ansi_c_end(cmd, i + 2)
            if ac_end == -1:
                result.append(cmd[i:])
                break
            result.append(cmd[i:ac_end])
            i = ac_end

        elif char == "`":
            # Backtick command substitution at top level — process content
            bt_end = _find_backtick_end(cmd, i + 1)
            if bt_end == -1:
                result.append(cmd[i:])
                break
            result.append("`")
            result.append(_process_unquoted(cmd[i + 1 : bt_end - 1]))
            result.append("`")
            i = bt_end

        elif char == "\\":
            if i + 1 < length and cmd[i + 1] in _BASH_METACHARACTERS:
                # Backslash is escaping a bash metacharacter — preserve both.
                # Append atomically so the metacharacter (e.g. ' " $) is not
                # re-processed as a quote-start or ANSI-C region on the next
                # iteration.
                result.append("\\")
                result.append(cmd[i + 1])
                i += 2
            elif _escaped_nul_spans and any(
                lo <= i < hi for lo, hi in _escaped_nul_spans
            ):
                # This backslash belongs to an escaped spelling of the null
                # device (``\nul``, ``n\ul``, ``nu\l`` — usually a
                # redirection target).  It must reach ``fix_bash_command``
                # intact: converting it to ``/`` would corrupt the word
                # (``/nul`` is an absolute path Git Bash cannot open),
                # and the null-device rewrite would never fire.  Preserve
                # the escape pair; the scanner rewrites the whole word to
                # ``/dev/null``.
                result.append("\\")
                result.append(cmd[i + 1])
                i += 2
            else:
                # Unquoted backslash in a path-like context — convert to /
                result.append("/")
                i += 1

        else:
            # Defensive: nxt should always point to a special char we handle.
            result.append(char)
            i += 1

    return "".join(result)




# pwsh_fix.py (kimi-agent) - PowerShell quoting validator/repair
# ======================================================================


_PWSH_NORMAL_RE = re.compile(r'''["'`#<@\-$]''')
"""Chars that can open a PowerShell region in normal (unquoted) mode.

Everything else in normal mode is plain code, so the tokenizer can bulk-skip
runs of it with one C-level regex search instead of a per-character dispatch.
``-`` is included for the ``--%`` stop-parsing marker, ``#``/``@`` for the
token-start comment / here-string checks, and ``$`` for ``$(...)``.
"""

_NORMAL = "normal"
_DQ = "double-quoted"
_SQ = "single-quoted"
_HDQ = "here-double"
_HSQ = "here-single"
_COMMENT = "line-comment"
_BLOCK = "block-comment"

_W_UNCLOSED_DQ = (
    "The command has an unclosed double-quoted string; "
    'appended a closing `"` at the end to make it a legal PowerShell command.'
)
_W_UNCLOSED_SQ = (
    "The command has an unclosed single-quoted string; "
    "appended a closing `'` at the end to make it a legal PowerShell command."
)
_W_UNCLOSED_HDQ = (
    "The command has an unclosed double-quoted here-string; "
    'appended a newline and `"@` at the end to close it.'
)
_W_UNCLOSED_HSQ = (
    "The command has an unclosed single-quoted here-string; "
    "appended a newline and `'@` at the end to close it."
)
_W_UNCLOSED_BLOCK = (
    "The command has an unclosed block comment `<#`; "
    "appended `#>` at the end to close it."
)
_W_TRAILING_COMMENT = (
    "The command ends with a line comment; "
    "appended a newline so the trailing comment does not swallow the "
    "try/catch wrapper used to execute the command."
)
_W_STOP_PARSING = (
    "The command ends with the `--%` stop-parsing marker; "
    "appended a newline so the wrapper is not passed literally to the "
    "native command."
)
_W_COMMENT_ONLY = (
    "The command contains only comments; appended a newline and a no-op "
    "`$null` statement so the try/catch wrapper has a statement to execute."
)
_W_TRAILING_CONTINUATION = (
    "The command ends with a backtick line-continuation; "
    "appended a newline so the continuation does not join with the "
    "try/catch wrapper used to execute the command."
)
_W_NUL_REDIRECT = (
    "Rewrote Windows-style null-device redirection target(s) `nul` to "
    "`$null` so PowerShell discards the output instead of creating a file "
    "named `nul`."
)

# Redirection operators that can create a file in PowerShell: ``>``, ``>>``,
# ``*>`` (all streams) and an fd/stream prefix such as ``2>`` / ``2>>``.  A
# target that is the unquoted word ``nul`` is the Windows null-device spelling
# (cmd.exe ``> NUL``); PowerShell treats it as an ordinary filename and would
# create an empty ``nul`` file, so it is rewritten to the ``$null`` sink.
_PWSH_NUL_REDIRECT_RE = re.compile(
    r"(?P<op>(?:[0-9]|\*)?>+)[ \t]*(?P<target>nul)(?![A-Za-z0-9_.])",
    re.IGNORECASE,
)


def _rewrite_pwsh_nul_redirections(cmd: str) -> tuple[str, str]:
    """Rewrite unquoted ``> nul``-style redirections to the ``$null`` sink.

    Returns ``(rewritten_command, warning)`` where *warning* is
    ``_W_NUL_REDIRECT`` when at least one target was rewritten, else ``""``.

    Quoting/comment/here-string awareness comes from :func:`_build_region_mask`
    (the same region scanner the PS7->PS5.1 transformer uses), so a literal
    ``nul`` inside a string/comment is never touched.  After ``--%`` the rest
    of the line is passed literally to the native executable (where ``> NUL``
    is valid cmd.exe syntax), so those regions are carved out of the mask too.
    Append redirections (``>> nul`` / ``2>> nul``) collapse to a single
    ``>``/``2>`` because appending to ``$null`` is meaningless.
    """
    if "nul" not in cmd.casefold() or ">" not in cmd:
        return cmd, ""
    mask = _build_region_mask(cmd)
    n = len(cmd)
    # ``--%`` stop-parsing: the rest of that line is literal for the native
    # command (cmd.exe ``> NUL`` is valid there and must not be rewritten).
    i = 0
    while i < n:
        if cmd[i] == "-" and cmd.startswith("--%", i) and mask[i]:
            if i == 0 or not (cmd[i - 1].isalnum() or cmd[i - 1] == "_"):
                line_end = cmd.find("\n", i + 3)
                end = n if line_end < 0 else line_end
                mask[i:end] = b"\x00" * (end - i)
                i = end
                continue
        i += 1

    out: list[str] = []
    previous = 0
    changed = False
    for m in _PWSH_NUL_REDIRECT_RE.finditer(cmd):
        op_start, op_end = m.span("op")
        target_start, target_end = m.span("target")
        # Only code-region matches are rewritten (strings/comments are 0).
        if not (mask[op_start] and mask[target_end - 1]):
            continue
        # Ensure every byte of the op+whitespace+target run is code.  A quoted
        # target (``> 'nul'``) has mask 0 at the quote, so the target_end-1
        # check above already preserves it.
        op_text = m.group("op")
        prefix = op_text.rstrip(">")  # preserve fd/stream prefix: 2>, *>
        between = cmd[op_end:target_start]
        space = " " if between else ""
        replacement = prefix + ">" + space + "$null"
        out.append(cmd[previous:op_start])
        out.append(replacement)
        previous = target_end
        changed = True
    if not changed:
        return cmd, ""
    out.append(cmd[previous:])
    return "".join(out), _W_NUL_REDIRECT


@dataclass(frozen=True)
class PwshFix:
    """Result of :func:`fix_pwsh_command`.

    ``command`` is the (possibly repaired) command to execute and ``warning``
    is a human-readable note describing any modification, or ``""`` when the
    command was already valid and left unchanged.
    """

    command: str
    warning: str = ""

    @property
    def changed(self) -> bool:
        """True when the command text differs from the input."""
        return bool(self.warning)


def fix_pwsh_command(cmd: str) -> PwshFix | None:
    """Validate *cmd* with PowerShell quoting rules and repair it if possible.

    Returns a :class:`PwshFix` (``command`` may equal *cmd* when the command
    is already legal), or ``None`` when the command cannot be repaired.

    Null-device redirections (``> nul`` etc.) are rewritten both before and
    after the quote-repair scanner: the first pass catches plain commands that
    would otherwise skip the scanner's fast path, and the second pass catches
    any target newly exposed when the scanner appends a missing closing quote
    or newline.
    """
    if not cmd or not cmd.strip():
        return None
    first, nul_warning = _rewrite_pwsh_nul_redirections(cmd)
    if not first.strip():
        return None
    result = _PwshScanner(first).fix()
    if result is None:
        return None
    second, nul_warning_after = _rewrite_pwsh_nul_redirections(result.command)
    warnings = [w for w in (nul_warning, result.warning, nul_warning_after) if w]
    return PwshFix(second, "\n".join(warnings))


class _PwshScanner:
    """Single-pass tokenizer implementing PowerShell's quoting rules."""

    __slots__ = ("s", "n")

    def __init__(self, s: str) -> None:
        self.s = s
        self.n = len(s)

    # -- helpers used while skipping $(...) sub-expressions -----------------

    def _skip_sq(self, start: int) -> int:
        """Skip a single-quoted string starting at *start*; index after it."""
        s, n = self.s, self.n
        i = start + 1
        while i < n:
            if s[i] == "'":
                if i + 1 < n and s[i + 1] == "'":
                    i += 2  # '' -> literal single quote
                else:
                    return i + 1  # closing quote
            else:
                i += 1
        return i

    def _skip_dq(self, start: int) -> int:
        """Skip a double-quoted string starting at *start*; index after it."""
        s, n = self.s, self.n
        i = start + 1
        while i < n:
            ch = s[i]
            if ch == "`":
                i += 2 if i + 1 < n else 1
            elif ch == '"':
                if i + 1 < n and s[i + 1] == '"':
                    i += 2  # "" -> literal double quote
                else:
                    return i + 1  # closing quote
            elif ch == "$" and i + 1 < n and s[i + 1] == "(":
                i = self._skip_subexpr(i)
            else:
                i += 1
        return i

    def _skip_block(self, start: int) -> int:
        """Skip a block comment starting at *start*; index after it.

        PowerShell closes block comments at the first ``#>`` — they do not
        nest (verified empirically with pwsh 7.6.2).
        """
        s, n = self.s, self.n
        i = start + 2
        while i < n:
            if s[i] == "#" and i + 1 < n and s[i + 1] == ">":
                return i + 2
            i += 1
        return i

    def _skip_subexpr(self, start: int) -> int:
        """Skip a ``$( ... )`` sub-expression starting at *start*.

        Iterative (no recursion) so deeply nested ``$(...)`` cannot hit the
        interpreter recursion limit.  Nested ``$(...)`` are handled by the
        paren-depth counter; strings and comments are skipped before the
        parens are counted, so a ``)`` inside a string is never mistaken for
        the closing paren.

        Returns the index *after* the matching ``)`` (or ``n`` when the
        sub-expression never closes — the caller then treats the enclosing
        string as unclosed).
        """
        s, n = self.s, self.n
        i = start + 2
        depth = 1
        while i < n and depth:
            ch = s[i]
            if ch == "(":
                depth += 1
                i += 1
            elif ch == ")":
                depth -= 1
                i += 1
            elif ch == "'":
                i = self._skip_sq(i)
            elif ch == '"':
                i = self._skip_dq(i)
            elif ch == "`":
                i += 2 if i + 1 < n else 1
            elif ch == "#":
                if self._at_token_start(i):
                    while i < n and s[i] != "\n":
                        i += 1
                else:
                    i += 1
            elif ch == "<" and i + 1 < n and s[i + 1] == "#":
                i = self._skip_block(i)
            else:
                i += 1
        return i

    # -- token-boundary predicate -------------------------------------------

    def _at_token_start(self, i: int) -> bool:
        """True when *i* starts a fresh token (not glued to a word/identifier)."""
        return i == 0 or not (self.s[i - 1].isalnum() or self.s[i - 1] == "_")

    # -- EOF repair ----------------------------------------------------------

    def _dq_closer(self) -> str:
        """Closing quote for an unclosed double-quoted string.

        A trailing backtick would escape a single appended ``"``, so an odd
        run of trailing backticks needs two quotes (one escaped, one closing).
        """
        k = 0
        for ch in reversed(self.s):
            if ch == "`":
                k += 1
            else:
                break
        return '""' if k % 2 == 1 else '"'

    # -- main scan -----------------------------------------------------------

    def fix(self) -> PwshFix | None:
        s, n = self.s, self.n
        mode = _NORMAL
        here_quote = ""   # opening quote of the current here-string
        line_start = 0    # start of the current line inside a here-string
        saw_code = False  # any real statement code seen (not comments/whitespace)
        last_cont_target = -1  # index after the last backtick-newline continuation
        i = 0
        search = _PWSH_NORMAL_RE.search
        while i < n:
            if mode == _NORMAL:
                # Bulk-skip a run of characters that cannot open a quoted,
                # comment, here-string, stop-parsing or sub-expression region.
                # They are all code (set ``saw_code``); only whitespace is
                # handled separately so it does not flip the flag.
                m = search(s, i)
                if m is None:
                    tail = s[i:]
                    if tail.strip():
                        saw_code = True
                    i = n
                    continue
                run = s[i:m.start()]
                if run:
                    if run.strip():
                        saw_code = True
                    i = m.start()
                ch = s[i]
                if ch == '"':
                    saw_code = True
                    mode = _DQ
                    i += 1
                elif ch == "'":
                    saw_code = True
                    mode = _SQ
                    i += 1
                elif ch == "`":
                    if i + 1 < n:
                        saw_code = True
                        if s[i + 1] == "\n":
                            last_cont_target = i + 2
                        i += 2  # escaped char (or line continuation)
                    else:
                        # Dangling line-continuation backtick: PowerShell
                        # rejects a backtick with nothing after it.
                        return None
                elif ch == "#" and self._at_token_start(i):
                    mode = _COMMENT
                    i += 1
                elif ch == "<" and i + 1 < n and s[i + 1] == "#":
                    mode = _BLOCK
                    i += 2
                elif (
                    ch == "@"
                    and i + 1 < n
                    and s[i + 1] in ("'", '"')
                    and self._at_token_start(i)
                ):
                    # Here-string opener: @' or @" followed by only
                    # whitespace until end-of-line (or end of input).
                    j = i + 2
                    while j < n and s[j] in " \t\r":
                        j += 1
                    if j == n or s[j] == "\n":
                        saw_code = True
                        here_quote = s[i + 1]
                        mode = _HDQ if here_quote == '"' else _HSQ
                        line_start = n if j == n else j + 1
                        i = line_start
                        continue
                    saw_code = True
                    i += 1  # @' / @" with content on the same line: not a here-string
                elif (
                    ch == "-"
                    and s.startswith("--%", i)
                    and self._at_token_start(i)
                ):
                    # --% stop-parsing: the rest of the line is literal.
                    if not saw_code:
                        # `--%` with no command before it is a PowerShell
                        # parse error ("Missing expression after unary
                        # operator '--'") — nothing to repair.
                        return None
                    nl = s.find("\n", i)
                    if nl == -1:
                        # The literal region reaches EOF and would swallow the
                        # try/catch wrapper — terminate the line explicitly.
                        return PwshFix(s + "\n", _W_STOP_PARSING)
                    i = nl + 1
                elif ch == "$" and i + 1 < n and s[i + 1] == "(":
                    saw_code = True
                    i = self._skip_subexpr(i)
                elif ch.isspace():
                    i += 1
                else:
                    saw_code = True
                    i += 1
            elif mode == _DQ:
                ch = s[i]
                if ch == "`":
                    i += 2 if i + 1 < n else 1
                elif ch == '"':
                    if i + 1 < n and s[i + 1] == '"':
                        i += 2  # "" -> literal double quote
                    else:
                        mode = _NORMAL
                        i += 1
                elif ch == "$" and i + 1 < n and s[i + 1] == "(":
                    i = self._skip_subexpr(i)
                else:
                    i += 1
            elif mode == _SQ:
                # Bulk-skip: only a single quote is special inside '...'.
                close = s.find("'", i, n)
                if close < 0:
                    i = n
                    continue
                if close + 1 < n and s[close + 1] == "'":
                    i = close + 2  # '' -> literal single quote
                else:
                    mode = _NORMAL
                    i = close + 1
            elif mode in (_HDQ, _HSQ):
                ch = s[i]
                if ch == "\n":
                    line_start = i + 1
                    i += 1
                elif (
                    ch == here_quote
                    and i + 1 < n
                    and s[i + 1] == "@"
                    and s[line_start:i].strip() == ""
                ):
                    mode = _NORMAL
                    i += 2
                else:
                    i += 1
            elif mode == _COMMENT:
                # Bulk-skip to the end of the line.
                nl = s.find("\n", i, n)
                if nl < 0:
                    i = n
                else:
                    mode = _NORMAL
                    i = nl + 1
            elif mode == _BLOCK:
                # Bulk-skip to the closing ``#>``.
                close = s.find("#>", i, n)
                if close < 0:
                    i = n
                else:
                    mode = _NORMAL
                    i = close + 2

        # -- end of input -----------------------------------------------------
        # A line-continuation backtick whose target line runs to the end of
        # the command would join with the try/catch wrapper added by the tool,
        # silently corrupting the command.  Append a newline so the
        # continuation ends on an empty line instead.
        needs_cont_nl = last_cont_target != -1 and s.rfind("\n") < last_cont_target
        if mode == _NORMAL:
            if saw_code:
                if needs_cont_nl:
                    return PwshFix(s + "\n", _W_TRAILING_CONTINUATION)
                return PwshFix(s, "")
            # Only comments/whitespace: give the wrapper a statement to run.
            return PwshFix(s + "\n$null", _W_COMMENT_ONLY)
        if mode == _DQ:
            fixed = s + self._dq_closer()
            warning = _W_UNCLOSED_DQ
            if needs_cont_nl:
                fixed += "\n"
                warning += "\n" + _W_TRAILING_CONTINUATION
            return PwshFix(fixed, warning)
        if mode == _SQ:
            fixed = s + "'"
            warning = _W_UNCLOSED_SQ
            if needs_cont_nl:
                fixed += "\n"
                warning += "\n" + _W_TRAILING_CONTINUATION
            return PwshFix(fixed, warning)
        if mode == _HDQ:
            fixed = s + '\n"@'
            warning = _W_UNCLOSED_HDQ
            if needs_cont_nl:
                fixed += "\n"
                warning += "\n" + _W_TRAILING_CONTINUATION
            return PwshFix(fixed, warning)
        if mode == _HSQ:
            fixed = s + "\n'@"
            warning = _W_UNCLOSED_HSQ
            if needs_cont_nl:
                fixed += "\n"
                warning += "\n" + _W_TRAILING_CONTINUATION
            return PwshFix(fixed, warning)
        if mode == _COMMENT:
            if saw_code:
                return PwshFix(s + "\n", _W_TRAILING_COMMENT)
            return PwshFix(s + "\n$null", _W_COMMENT_ONLY)
        if mode == _BLOCK:
            if saw_code:
                fixed = s + "#>"
                warning = _W_UNCLOSED_BLOCK
            else:
                fixed = s + "#>\n$null"
                warning = _W_COMMENT_ONLY
            if needs_cont_nl:
                fixed += "\n"
                warning += "\n" + _W_TRAILING_CONTINUATION
            return PwshFix(fixed, warning)
        return None  # pragma: no cover - unreachable


# ======================================================================
# process_pwsh.py (kimi-agent) - PS7 to PS5.1 source transform
# ======================================================================


try:
    import regex as re
except ImportError:  # pragma: no cover
    import re  # stdlib: the used patterns are all stdlib-compatible


# ===========================================================================
# Constants
# ===========================================================================

_PS_KEYWORDS = frozenset({
    "begin", "break", "catch", "class", "continue", "data", "define", "do",
    "dynamicparam", "else", "elseif", "end", "enum", "exit", "filter", "finally",
    "for", "foreach", "from", "function", "hidden", "if", "in", "param",
    "process", "return", "static", "switch", "throw", "trap", "try", "until",
    "using", "var", "while",
})



_EXPR_STOP = "=;|&,"

_DEPTH_OPEN = "([{"
_DEPTH_CLOSE = ")]}"


# ===========================================================================
# Low-level scanners — skip over strings, comments, subexpressions
# ===========================================================================

def _scan_single_quoted(code: str, i: int) -> int:
    """Skip a single-quoted string starting at *i*; return index after it."""
    i += 1
    n = len(code)
    while i < n:
        if code[i] == "'":
            if i + 1 < n and code[i + 1] == "'":
                i += 2          # escaped '' → literal single-quote
            else:
                return i + 1     # closing quote
        else:
            i += 1
    return i


def _scan_double_quoted(code: str, i: int) -> int:
    """Skip a double-quoted string starting at *i*; return index after it."""
    i += 1
    n = len(code)
    while i < n:
        ch = code[i]
        if ch == "`" and i + 1 < n:
            i += 2              # backtick-escaped char
        elif ch == '"':
            return i + 1         # closing quote
        elif ch == "$" and i + 1 < n and code[i + 1] == "(":
            i = _skip_subexpression(code, i)
        else:
            i += 1
    return i


def _scan_block_comment(code: str, i: int) -> int:
    """Skip a block comment ``<# ... #>`` starting at *i*; return index after it.

    PowerShell block comments do NOT nest: the comment ends at the first
    ``#>`` (matching ``_PwshScanner._skip_block`` and the real parser).  A
    nested-looking ``<#`` inside the comment is ordinary comment text.
    """
    i += 2
    n = len(code)
    while i < n:
        if code[i] == "#" and i + 1 < n and code[i + 1] == ">":
            return i + 2
        i += 1
    return i


def _skip_subexpression(code: str, start: int) -> int:
    """Skip past a ``$(...)`` sub-expression starting at *start*.

    Returns the index *after* the closing ``)``.
    """
    assert code[start] == "$"
    i = start + 2
    depth = 1
    n = len(code)
    while i < n and depth:
        c = code[i]
        if c == "(":
            depth += 1
            i += 1
        elif c == ")":
            depth -= 1
            i += 1
        elif c == "'":
            i = _scan_single_quoted(code, i)
        elif c == '"':
            i = _scan_double_quoted(code, i)
        elif c == "$" and i + 1 < n and code[i + 1] == "(":
            i = _skip_subexpression(code, i)
        else:
            i += 1
    return i


# ===========================================================================
# Region finders  (strings, comments, here-strings)
# ===========================================================================

def _scan_here_string(code: str, start: int) -> int:
    """Skip a here-string (``@'...'@`` or ``@"..."@``) starting at *start*.

    *start* points to the opening ``@``.
    Returns the index *after* the closing delimiter.
    """
    n = len(code)
    quote = code[start + 1]
    i = start + 2
    seen_newline = False
    line_begin = start + 2  # start of current line
    while i < n:
        if code[i] == "\n":
            seen_newline = True
            line_begin = i + 1
        elif (
            code[i] == quote
            and i + 1 < n
            and code[i + 1] == "@"
            and seen_newline
        ):
            # Closing delimiter must be at the beginning of its own line
            # (only whitespace may precede it on that line)
            if code[line_begin:i].strip() == "":
                return i + 2
        i += 1
    return i


_REGION_START_RE = re.compile(r"[#<@'\"]")


def _build_region_mask(code: str, *, here_strings: bool = True) -> bytearray:
    """Build a region mask for *code* in a single pass.

    Returns a bytearray where 1 means outside regions (code), 0 means inside
    (strings, comments, here-strings), and 2 means live code inside a ``$(...)``
    string interpolation (see :func:`_mask_double_quoted`).

    When *here_strings* is False, here-strings are not detected (used for
    line-level scanning where here-strings cannot be reliably identified).
    """
    n = len(code)
    mask = bytearray(b"\x01" * n)
    _mask_code_span(code, mask, 0, n, here_strings=here_strings)
    return mask


def _mask_code_span(code: str, mask: bytearray, lo: int, hi: int, *, here_strings: bool) -> None:
    """Mask region (0) characters within the code span ``[lo, hi)``.

    Byte 1 marks ordinary code, byte 2 marks interpolated code (inside a
    ``$(...)`` within a double-quoted string), byte 0 marks strings,
    comments and here-strings.  Scanners are clamped to *hi* so a region
    that (pathologically) extends past the span cannot corrupt neighbours.
    """
    find = _REGION_START_RE.search
    i = lo
    while i < hi:
        # Bulk-jump to the next region-opening character ('#', '<', '@',
        # quote): a C-level regex search replaces the per-character walk.
        m = find(code, i)
        if m is None or m.start() >= hi:
            break
        i = m.start()
        c = code[i]
        if c == "<" and i + 1 < hi and code[i + 1] == "#":
            start = i
            i = min(_scan_block_comment(code, i), hi)
            mask[start:i] = b"\x00" * (i - start)
        elif c == "#":
            # ``#`` opens a comment only at a token boundary: PowerShell treats
            # ``foo#c`` (a hash glued to an identifier character) as a single
            # bareword token, not as ``foo`` followed by a comment.  Without
            # this check the mask swallows everything after a glued ``#`` and
            # any ``??``/``?.``/``?:`` on the rest of the line survives into the
            # emitted PS 5.1 text (where it is a parse error).  This mirrors
            # ``_PwshScanner._at_token_start``.
            if i > lo and (code[i - 1].isalnum() or code[i - 1] == "_"):
                i += 1
                continue
            start = i
            newline = code.find("\n", i, hi)
            i = hi if newline < 0 else newline
            mask[start:i] = b"\x00" * (i - start)
        elif c == "@" and i + 1 < hi and code[i + 1] in ("'", '"'):
            if not here_strings:
                i += 1
                continue
            # PowerShell here-strings require the closing delimiter at the
            # beginning of its own line (only whitespace may precede it).
            j = i + 2
            while j < hi and code[j] in " \t\r":
                j += 1
            if j >= hi or code[j] != "\n":
                i += 1  # Not a here-string: @' or @" not followed by newline
                continue
            start = i
            i = min(_scan_here_string(code, i), hi)
            mask[start:i] = b"\x00" * (i - start)
        elif c == "'":
            start = i
            i = min(_scan_single_quoted(code, i), hi)
            mask[start:i] = b"\x00" * (i - start)
        elif c == '"':
            i = _mask_double_quoted(code, mask, i, hi, here_strings=here_strings)
        else:
            i += 1


def _mask_double_quoted(
    code: str, mask: bytearray, start: int, hi: int, *, here_strings: bool
) -> int:
    """Mask a double-quoted string starting at *start*; return index after it.

    String text is masked 0.  ``$(...)`` interpolations are *live code* in
    PowerShell, so their contents are masked 2 ("interpolated code": truthy
    like ordinary code for the expression transforms, but distinguishable
    from plain code) with nested strings/comments masked 0.  Without this,
    operators inside an interpolation such as ``"$($a ?? 'd')"`` were
    invisible to every transform and survived into the emitted 5.1 text as
    parse errors.
    """
    n = min(len(code), hi)
    i = start + 1
    mask[start] = 0
    while i < n:
        ch = code[i]
        if ch == "`" and i + 1 < n:
            mask[i] = 0
            mask[i + 1] = 0
            i += 2
        elif ch == '"':
            mask[i] = 0
            return i + 1
        elif ch == "$" and i + 1 < n and code[i + 1] == "(":
            sub_end = min(_skip_subexpression(code, i), n)
            mask[i] = 2  # $(
            mask[i + 1] = 2
            if sub_end - 1 > i + 1:
                mask[sub_end - 1] = 2  # closing )
                _mask_code_span(
                    code, mask, i + 2, sub_end - 1, here_strings=here_strings
                )
                # Promote plain code (1) to interpolated code (2); nested
                # strings/comments stay 0, deeper interpolations stay 2.
                for k in range(i + 2, sub_end - 1):
                    if mask[k] == 1:
                        mask[k] = 2
            i = sub_end
        else:
            mask[i] = 0
            i += 1
    return i


def _line_mask(line: str) -> bytearray:
    """Return a region mask for *line* (here-strings disabled)."""
    return _build_region_mask(line, here_strings=False)


# ===========================================================================
# Depth tracking  (for matching ternary colon)
# ===========================================================================

def _compute_depths(line: str, mask: bytearray) -> list[int]:
    """Return nesting depth of ``()``, ``{}``, ``[]`` before each character."""
    depths: list[int] = []
    depth = 0
    for i, ch in enumerate(line):
        depths.append(depth)
        if mask[i]:
            if ch in _DEPTH_OPEN:
                depth += 1
            elif ch in _DEPTH_CLOSE:
                depth -= 1
    depths.append(depth)
    return depths


# ===========================================================================
# Pre-processing: backtick line continuation
# ===========================================================================

def _join_continuation_lines(code: str) -> str:
    """Collapse line-continuations into single logical lines.

    Two flavours continue a statement past a newline in PowerShell 7: an
    explicit backtick, and a trailing binary operator (``&&`` / ``||`` /
    ``??`` / ``??=``) at end of line.  Both are collapsed here so the
    per-line transforms see the whole statement.  Blank lines and comment
    lines between the operator and the operand are skipped (PowerShell
    ignores them while continuing).
    """
    has_op_cont = False
    if "&&" in code or "||" in code or "??" in code:
        has_op_cont = True
    if "`" not in code and not has_op_cont:
        return code
    if "`" in code:
        code = _join_backtick_continuations(code)
    if has_op_cont:
        code = _join_operator_continuations(code)
    return code


def _join_backtick_continuations(code: str) -> str:
    """Collapse explicit backtick line-continuations into logical lines."""
    if "`" not in code:
        return code
    mask = _build_region_mask(code)
    n = len(code)
    result: list[str] = []
    i = 0
    previous = 0
    find = code.find
    while True:
        # Bulk-jump to the next backtick (C-level find) instead of testing
        # every character; only a backtick in a code region can continue.
        i = find("`", i)
        while i != -1 and not mask[i]:
            i = find("`", i + 1)
        if i == -1:
            break
        j = i + 1
        while j < n and code[j] in " \t\r":
            j += 1
        if j < n and code[j] == "\n":
            j += 1
            while j < n and code[j] in " \t\r":
                j += 1
            result.append(code[previous:i])
            result.append(" ")
            previous = j
            i = j
            continue
        if j == i + 1 and j < n and code[j] == "`":
            # Escaped backtick (`` `` ``): a literal backtick character, NOT a
            # line continuation.  Skip the pair so the second backtick is not
            # re-examined and mistaken for a continuation when a newline
            # follows it (real PowerShell keeps the line break).
            i = j + 1
            continue
        i += 1
    result.append(code[previous:])
    return "".join(result)


def _join_operator_continuations(code: str) -> str:
    """Join a statement whose physical line ends with a binary operator.

    PowerShell 7 continues the logical line when a binary operator
    (``&&`` / ``||`` / ``??`` / ``??=``) is the last token of the line; the
    per-line transforms would otherwise see an operator with no operand and
    emit broken or wrongly-gated 5.1 code.  Only code-region tails qualify
    (a ``&&`` inside a string or comment is data).  Blank lines and full-
    comment lines between the operator and the operand are skipped, matching
    PowerShell's continuation.
    """
    mask = _build_region_mask(code)
    n = len(code)
    result: list[str] = []
    pos = 0
    while pos < n:
        nl = code.find("\n", pos)
        if nl == -1:
            result.append(code[pos:])
            break
        line = code[pos:nl]
        stripped = line.rstrip(" \t\r")
        join = False
        if stripped:
            tail_idx = pos + len(line) - len(stripped)
            if stripped.endswith("??=") and mask[tail_idx + len(stripped) - 3]:
                join = True
            elif stripped.endswith(("&&", "||", "??")) and mask[tail_idx + len(stripped) - 2]:
                join = True
        if not join:
            result.append(code[pos:nl + 1])
            pos = nl + 1
            continue
        # Skip blank / comment lines to find the continuation operand.
        j = nl + 1
        while j < n:
            nxt_nl = code.find("\n", j)
            if nxt_nl == -1:
                nxt_nl = n
            nxt = code[j:nxt_nl].strip(" \t\r")
            if not nxt or nxt.startswith("#"):
                j = nxt_nl + 1 if nxt_nl < n else n
                continue
            break
        if j >= n:
            # Nothing to continue onto: leave the newline in place.
            result.append(code[pos:nl + 1])
            pos = nl + 1
            continue
        result.append(code[pos:nl].rstrip(" \t\r") + " ")
        pos = j
    return "".join(result)
    return "".join(result)


# ===========================================================================
# Assignment detection
# ===========================================================================

_ASSIGN_RE = re.compile(r"(.*?)(\$\w+(?::\w+)?(?:\.\w+)*)\s*=\s*$")
_CHAIN_ASSIGN_RE = re.compile(
    r"^(\s*(?:\$\{[^}]+\}|\$\w+(?::\w+)?(?:\.\w+|\[[^\]]*\])*)\s*"
    r"(\+=|-=|\*=|/=|%=|=(?![=~])))\s*(.*)$"
)
"""Match a variable assignment at the START of a chain's left segment.

``$x = A && B`` assigns the COMBINED output of the executed segments in
PowerShell 7, so the sequential rewrite must place the assignment outside
the lifted value: ``$x = $(A; if ($?) { B })`` — assigning inside would
give ``$x`` only the left segment and leak the right one to the output
stream.  Comparison operators (``-eq`` …) never match; ``==``/``=~`` are
excluded by the negative lookahead.  Group 1 is the assignment incl. the
operator, group 3 the remainder of the left segment.
"""
_COMMAND_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*\s+")


def _match_assignment(before: str) -> tuple[str, str] | None:
    """Match an assignment prefix like ``$var = `` at the end of *before*."""
    m = _ASSIGN_RE.match(before.rstrip())
    if m:
        return m.group(1), m.group(2)
    return None


def _build_replacement(prefix: str, inner: str) -> str:
    """Build replacement string, preserving an assignment if one is detected."""
    assign = _match_assignment(prefix)
    if assign:
        p, var = assign
        return f"{p}{var} = {inner}"
    return f"{prefix}{inner}"


def _emit_replacement(prefix: str, inner: str) -> str:
    """Concatenate *prefix* with the generated *inner*, guarding keyword glue.

    ``return``/``throw``/command-name prefixes stripped from an operand leave
    the prefix ending in a word character; concatenating directly produces
    ``return$(...)``, which 5.1 parses as a command name.  Insert a space in
    that case (an assignment rewrite never ends in a word character, so the
    guard cannot misfire there).
    """
    if (
        prefix
        and (prefix[-1].isalnum() or prefix[-1] == "_")
        and _match_assignment(prefix) is None
    ):
        return f"{prefix} {inner}"
    return _build_replacement(prefix, inner)


def _strip_command_prefix(expr: str, start: int, *, check_keywords: bool = True) -> tuple[str, int]:
    """Strip a leading command name (e.g. ``Write-Output ``) from *expr*.

    Returns ``(stripped_expr, adjusted_start)``.
    When *check_keywords* is True (default), PowerShell keywords (if, foreach, …)
    are never stripped. When False, any command prefix is stripped.
    """
    m = _COMMAND_PREFIX_RE.match(expr)
    if m:
        cmd = m.group(0).strip().lower()
        if not check_keywords or cmd not in _PS_KEYWORDS:
            expr_part = expr[m.end():]
            if expr_part and expr_part[0] in '$(["\'@0123456789':
                return expr_part, start + m.end()
    return expr, start


_RETURN_THROW_OPERAND_RE = re.compile(r"^(return|throw)(\s+)(.*)$",
                                      re.IGNORECASE | re.DOTALL)

_REGION_PLACEHOLDER_RE = re.compile(r"__kimixrgn\d+x\d+__")


def _lift_region_placeholders(expr: str) -> tuple[str, list[str]]:
    """Split opaque multi-line-region placeholder words out of *expr*.

    ``pwsh_transform`` splices placeholder words onto lines where a multi-line
    string/comment/here-string region opens or closes mid-line.  A placeholder
    stands for region text (e.g. a block comment) and must survive as a
    standalone token — it must never be folded into a generated ``if (...)``
    operand, or the substitution step would re-inject the region's literal
    text (``#>``) inside the generated expression (a 5.1 parse error).

    Returns ``(clean_expr, placeholders)``.  When the operand consists of
    nothing but placeholders (e.g. a here-string literal used directly as
    the ``??`` value) the placeholders are kept in place — the substitution
    re-injects a valid literal — and an empty ``clean_expr`` is reported so
    the caller does not misread the operand as missing.
    """
    found = _REGION_PLACEHOLDER_RE.findall(expr)
    if not found:
        return expr, []
    cleaned = _REGION_PLACEHOLDER_RE.sub(" ", expr).strip()
    if not cleaned:
        return expr, []
    return cleaned, found


def _keeps_prefix(keeps: list[str]) -> str:
    """Render lifted placeholders as a prefix segment (or "")."""
    return (" " + " ".join(keeps) + " ") if keeps else ""


def _strip_keyword_operand(expr: str, start: int) -> tuple[str, int]:
    """Strip a leading ``return``/``throw`` keyword from *expr*.

    The keyword remains in the prefix (``return $(if ...)``) and only its
    operand is transformed.  Without this the keyword is folded into the
    coalesced operand (``if ($null -ne return $a)`` — a 5.1 parse error).
    ``return``/``throw`` are PowerShell keywords, so
    :func:`_strip_command_prefix` deliberately leaves them in place.
    """
    m = _RETURN_THROW_OPERAND_RE.match(expr)
    if m:
        return m.group(3), start + len(m.group(1)) + len(m.group(2))
    return expr, start


# ===========================================================================
# $? adjacency check — shared by all transforms
# ===========================================================================

def _separate_trailing_comment(line: str, start: int, mask: bytearray
                         ) -> tuple[int, str]:
    """Split off a trailing line comment starting at *start*, if present.

    Returns ``(content_end, comment_str)`` where *content_end* is the index
    at which the content before the comment ends, and *comment_str* is the
    comment text (including the ``#``) or ``""`` if none.
    """
    for ri in range(max(start, 1), len(line)):
        if mask[ri] == 0 and mask[ri - 1] == 1 and line[ri] == "#":
            return ri, line[ri:]
    return len(line), ""


def _after_dollar_question(line: str, op_idx: int) -> bool:
    """Return True if *op_idx* is immediately after ``$?``.

    When True the first ``?`` of the operator at *op_idx* is actually the
    ``?`` of the ``$?`` automatic variable, so the operator should be skipped.

    Does NOT match ``$$?.`` — ``$$`` is a separate automatic variable.
    """
    return (
        op_idx > 0
        and line[op_idx - 1] == "$"
        and not (op_idx > 1 and line[op_idx - 2] == "$")
    )


# ===========================================================================
# Shared colon-context check
# ===========================================================================

def _is_scope_colon(line: str, i: int) -> bool:
    """Return ``True`` if the colon at *i* belongs to a ``$scope:var`` prefix."""
    # Scan backwards from the colon to find the preceding ``$`` variable prefix.
    j = i - 1
    while j >= 0 and (line[j].isalnum() or line[j] == "_"):
        j -= 1
    return j >= 0 and line[j] == "$"


def _is_drive_colon(line: str, i: int) -> bool:
    """Return ``True`` if the colon at *i* is a drive-reference colon.

    A single ASCII letter directly before the colon (itself not part of a
    longer word or a ``$`` reference) followed by ``\\``/``/`` or the end of
    the expression is a drive reference (``C:\\Temp``, ``C:/x``, ``C:``),
    not the branch separator of a ternary — ``?`` is also the Where-Object
    alias, so a line like ``gci | ? {...} ; Write-Output C:\\Temp`` must not
    be mistaken for a ternary whose separator colon is the drive letter.
    """
    if i < 1 or not (line[i - 1].isascii() and line[i - 1].isalpha()):
        return False
    j = i - 2
    if j >= 0 and (line[j].isalnum() or line[j] in "_$"):
        return False
    return i + 1 >= len(line) or line[i + 1] in "\\/ \t')},;|&"


_VALUE_EXPR_TAIL_RE = re.compile(
    r"(?:"
    r"\$[A-Za-z_][\w:]*|\$\?|\$\$|\$\^|\$\{[^}]+\}"  # variable references
    r"|@[A-Za-z_][\w:]*"  # splatting / array literals (@obj, @{...} ends with })
    r"|[)\]}]"  # closed groups / indexers / braces
    r"|'[^']*'|\"[^\"]*\""  # string literals
    r"|[0-9][0-9.]*"  # numeric literals
    r"|\[[\w.]+\]::\w+"  # static member access ([Math]::PI)
    r")(?:\.\w+|\[[^\]]*\]|--|\+\+)*$"
)
"""Matches an expression that ends like a value: a variable reference, string
or numeric literal, closed group, or static member access, with optional
member/index chains (``$obj.Name``, ``$a[0]``, ``'x'``, ``[Math]::PI``)."""


def _looks_like_value_expr(expr: str) -> bool:
    """Return ``True`` when *expr* ends like a value expression.

    The null-conditional (``?.``/``?[``) and null-coalescing (``??``)
    transforms only apply to value expressions; a bareword left operand is
    5.1 command text whose ``?`` characters are filename wildcards
    (``rm f?.bak``, ``Write-Host a??b``) and must be left untouched.
    """
    expr = expr.rstrip()
    if not expr:
        return False
    if expr[-1] in ")]}":
        return True
    return _VALUE_EXPR_TAIL_RE.search(expr) is not None


# ===========================================================================
# Expression boundary helpers
# ===========================================================================

def _is_null_conditional_qmark(line: str, i: int) -> bool:
    """Return True if ``?`` at *i* is part of a null-conditional operator.

    Both flavours count: ``?.`` (member) and ``?[`` (index).  Expression
    scans use this guard to pass through a null-conditional instead of
    treating its ``?`` as a stop character, so a mixed chain such as
    ``$a?[$i]?.b`` keeps its full base (``$a?[$i]``) for the ``?.`` pass.
    """
    return i + 1 < len(line) and line[i + 1] in ".["


def _is_double_colon(line: str, i: int) -> bool:
    """Return True if ``:`` at *i* is part of ``::`` (static member access)."""
    return (i + 1 < len(line) and line[i + 1] == ":") or (i > 0 and line[i - 1] == ":")


def _find_expr_start(line: str, end: int, mask: bytearray,
                     extra_stop: str = "") -> int:
    """Scan backwards from *end* to locate the start of the expression.

    *extra_stop* can contain additional delimiter characters (e.g. ``"?:``
    for null-conditional base scanning).
    """
    depth = 0
    stop_set = _EXPR_STOP + extra_stop if extra_stop else _EXPR_STOP
    for i in range(end - 1, -1, -1):
        if not mask[i]:
            continue
        c = line[i]
        if c in _DEPTH_CLOSE:
            depth += 1
        elif c in _DEPTH_OPEN:
            depth -= 1
            if depth < 0:
                return i + 1
        elif depth == 0 and c in stop_set:
            if c == "?":
                if _is_null_conditional_qmark(line, i):
                    continue
                if _after_dollar_question(line, i):
                    continue
            elif c == ":":
                if _is_double_colon(line, i) or _is_scope_colon(line, i):
                    continue
            return i + 1
    return 0


def _find_expr_end(line: str, start: int, mask: bytearray,
                   extra_stop: str = "") -> int:
    """Scan forwards from *start* to locate the end of the expression.

    *extra_stop* can contain additional delimiter characters (e.g. ``"?:"``
    so a ``??`` operand never swallows a co-located ternary's ``?``/``:``).
    The same context guards as :func:`_find_expr_start` apply: null-conditional
    ``?.``, the ``$?`` automatic variable, ``::`` static access and scope /
    drive colons do not end an expression.
    """
    depth = 0
    stop_set = _EXPR_STOP + extra_stop if extra_stop else _EXPR_STOP
    # Track mask value at the previous position to detect region boundaries.
    prev_mask = 1 if start == 0 else mask[start - 1]
    for i in range(start, len(line)):
        c = line[i]
        cur_mask = mask[i]
        if not cur_mask:
            # If we just stepped from outside (mask=1) into a region and the
            # character is '#', it is the start of a line comment — stop here.
            if prev_mask and c == "#":
                return i
            prev_mask = cur_mask
            continue
        prev_mask = cur_mask
        if c in _DEPTH_OPEN:
            depth += 1
        elif c in _DEPTH_CLOSE:
            depth -= 1
            if depth < 0:
                return i
        elif depth == 0 and c in stop_set:
            if c == "?":
                if _is_null_conditional_qmark(line, i):
                    continue
                if i > 0 and line[i - 1] == "$":
                    continue  # the $? automatic variable
            elif c == ":":
                if (
                    _is_double_colon(line, i)
                    or _is_scope_colon(line, i)
                    or _is_drive_colon(line, i)
                ):
                    continue
            return i
    return len(line)


def _expr_left(line: str, pos: int, mask: bytearray,
               extra_stop: str = "") -> tuple[int, int]:
    """Return (start, end) of the expression immediately left of *pos*."""
    end = pos
    while end > 0 and line[end - 1] == " ":
        end -= 1
    start = _find_expr_start(line, end, mask, extra_stop)
    return start, end


def _expr_right(line: str, pos: int, mask: bytearray,
                extra_stop: str = "") -> tuple[int, int]:
    """Return (start, end) of the expression immediately right of *pos*."""
    start = pos
    while start < len(line) and line[start] == " ":
        start += 1
    end = _find_expr_end(line, start, mask, extra_stop)
    return start, end


def _swallow_comma_operands(line: str, end: int, mask: bytearray) -> int:
    """Extend *end* over ``, <expr>`` continuations of a ``??`` right operand.

    PowerShell's comma operator binds tighter than ``??``, so ``$a ?? $b, 3``
    is ``$a ?? ($b, 3)`` in PowerShell 7.  Two comma flavours must NOT be
    swallowed: commas separating COMMAND ARGUMENTS (``f($a ?? 1, $b)`` — the
    enclosing opener belongs to a command/method call) and commas following a
    completed group (``Write-Output ($a ?? 1), 2`` — the operand already ended
    at the closer, so the char before the comma sits at a deeper level).
    """
    depth_arr = _compute_depths(line, mask)
    j = end
    while True:
        p = j - 1
        while p >= 0 and line[p] in " \t":
            p -= 1
        k = j
        while k < len(line) and line[k] in " \t":
            k += 1
        if (
            p < 0
            or k >= len(line)
            or line[k] != ","
            or not mask[k]
            or depth_arr[k] != depth_arr[p]
        ):
            return j
        d = depth_arr[k]
        if d > 0:
            # Identify the enclosing opener: walk left past nested groups.
            o = k - 1
            nested = 0
            opener = ""
            opener_pos = -1
            while o >= 0:
                if mask[o]:
                    ch = line[o]
                    if ch in ")]}":
                        nested += 1
                    elif ch in "([{":
                        if nested:
                            nested -= 1
                        elif depth_arr[o] == d - 1:
                            opener = ch
                            opener_pos = o
                            break
                o -= 1
            # A word/member directly before the opener marks a command or
            # method argument list: commas there separate arguments.
            if opener and opener_pos > 0 and (
                line[opener_pos - 1].isalnum() or line[opener_pos - 1] in "_)}]"
            ):
                return j
        nstart = k + 1
        while nstart < len(line) and line[nstart] in " \t":
            nstart += 1
        if nstart >= len(line):
            return j
        nend = _find_expr_end(line, nstart, mask)
        if nend <= nstart:
            return j
        j = nend


# ===========================================================================
# Generic operator transform engine
# ===========================================================================

def _find_next_op(line: str, op: str, mask: bytearray,
                  skip_dollar_q: bool = False, start: int = 0,
                  reverse: bool = False, plain_code_only: bool = False) -> int:
    """Find the next (or rightmost when *reverse* is True) occurrence of *op*
    in *line* that is outside regions.

    If *skip_dollar_q* is True, skip positions immediately after ``$?``.
    *start* restricts the search to indices >= *start* (ignored when *reverse*).
    When *plain_code_only* is True, only mask value 1 (ordinary code) matches:
    interpolated code inside ``"$(...)"`` (mask 2) is skipped — the chain
    transform handles interpolation operators in a dedicated second phase
    with region edges as hard boundaries.
    """
    search = line.rfind if reverse else line.find
    idx = search(op, start if not reverse else None)
    while idx != -1:
        if mask[idx] and (not plain_code_only or mask[idx] == 1) \
                and (not skip_dollar_q or not _after_dollar_question(line, idx)):
            return idx
        idx = search(op, idx + 1) if not reverse else line.rfind(op, 0, idx)
    return -1


def _transform_operator(
    line: str,
    op: str,
    builder: Callable,
    *,
    skip_dollar_q: bool = True,
    require_value_left: bool = False,
    operand_extra_stop: str = "",
    swallow_commas: bool = False,
    deep_ternary: bool = False,
) -> tuple[str, list[str]]:
    """Generic single-operator line transformer.

    *op* — the operator string to search for (e.g. ``"??"``).
    *builder* — callable(left_expr, right_expr, right_extra) → inner_str.
    *skip_dollar_q* — when True, skip positions immediately after ``$?``.
    *require_value_left* — when True, only transform when the left operand
      ends like a value expression (see :func:`_looks_like_value_expr`);
      a bareword left operand is 5.1 command text with wildcard characters.
    *operand_extra_stop* — extra delimiter characters for the operand scans
      (e.g. ``"?:"`` so a ``??`` never swallows a co-located ternary).
    *swallow_commas* — extend the right operand over same-depth ``, expr``
      continuations (comma binds tighter than ``??`` in PowerShell 7).
    *deep_ternary* — pre-transform ternaries inside the operands (a
      parenthesized ``( $c ? 1 : 2 )`` operand would otherwise be copied
      verbatim into the generated braces, where the ternary pass — which
      refuses ``?`` inside ``{ }`` — can never reach it).

    Returns ``(transformed_line, warnings)``.
    """
    warnings: list[str] = []
    op_len = len(op)
    if op not in line:
        return line, warnings
    search = 0
    while True:
        mask = _line_mask(line)
        idx = _find_next_op(line, op, mask, skip_dollar_q, search)
        if idx == -1:
            break

        left_start, left_end = _expr_left(line, idx, mask, operand_extra_stop)
        left_expr = line[left_start:left_end].strip()
        left_expr, left_start = _strip_command_prefix(left_expr, left_start)
        left_expr, left_start = _strip_keyword_operand(left_expr, left_start)

        right_start, right_end = _expr_right(
            line, idx + op_len, mask, operand_extra_stop
        )
        if swallow_commas:
            right_end = _swallow_comma_operands(line, right_end, mask)
        right_expr = line[right_start:right_end].strip()
        right_extra = None

        # Region placeholders (spliced onto mid-line region boundaries by
        # ``pwsh_transform``) are opaque tokens: keep them out of the operands
        # so the substitution step never re-injects region text (``#>``)
        # inside the generated expression.
        keeps: list[str] = []
        left_expr, found = _lift_region_placeholders(left_expr)
        keeps.extend(found)
        right_expr, found = _lift_region_placeholders(right_expr)
        keeps.extend(found)

        # Parenthesized ternaries inside an operand would be copied verbatim
        # into the generated ``{ }`` braces, where the ternary pass can never
        # reach them (its brace guard) — pre-transform them here.
        if deep_ternary:
            if "?" in left_expr:
                left_expr, wl = _transform_ternary_line(left_expr)
                warnings.extend(wl)
            if "?" in right_expr:
                right_expr, wl = _transform_ternary_line(right_expr)
                warnings.extend(wl)

        if not left_expr or not right_expr:
            search = idx + 1
            continue
        if require_value_left and not _looks_like_value_expr(left_expr):
            search = idx + 1
            continue

        inner = builder(left_expr, right_expr, right_extra)
        # ``builder`` returns an ``if (...) {...} else {...}`` *statement*.  A
        # bare statement is only legal at pipeline/statement position (or as a
        # full ``$var = ...`` right-hand side); wherever the operator sits in an
        # *expression* position — inside ``(...)``/``[...]``/``{...}``, as a
        # command argument, or directly before a ``|``/``&&``/redirection — the
        # statement must be lifted into a value with ``$( ... )``.  Wrapping
        # unconditionally is semantically identical at statement position (the
        # sub-expression still emits the branch result) and fixes every
        # expression position, e.g. ``(if ...)`` which is a parse error.
        inner = f"$({inner})"
        warnings.append(f"{op} operator `{left_expr} {op} {right_expr}` rewritten to `{inner}`")
        line = _emit_replacement(
            line[:left_start] + _keeps_prefix(keeps), inner
        ) + line[right_end:]
        search = left_start
    return line, warnings


# ===========================================================================
# Transform: null-coalescing assignment  (??=)
# ===========================================================================

def _transform_nca_line(line: str) -> tuple[str, list[str]]:
    """Rewrite null-coalescing assignment ``$var ??= value``.

    Uses the generic expression scanner so braced variables (``${foo}``),
    property chains (``$obj.Name``) and indexed targets (``$arr[0]``) are
    all handled correctly.
    """
    warnings: list[str] = []
    if "??=" not in line:
        return line, warnings
    search = 0
    while True:
        mask = _line_mask(line)
        idx = _find_next_op(line, "??=", mask, True, search)
        if idx == -1:
            break

        left_start, left_end = _expr_left(line, idx, mask)
        var = line[left_start:left_end].strip()
        if not var:
            search = idx + 1
            continue

        val_start, val_end = _expr_right(line, idx + 3, mask)
        value = line[val_start:val_end].strip()
        # A chained coalesce in the value (``$x ??= $y ??= 3`` /
        # ``$x ??= $y ?? 3``) is right-associative in PowerShell 7.  The
        # plain value scan stops at the inner ``??=``'s ``=``, so extend the
        # value over the inner assignment first.
        while (
            value.endswith("??")
            and val_end < len(line)
            and line[val_end] == "="
        ):
            val_end = _find_expr_end(line, val_end + 1, mask)
            value = line[val_start:val_end].strip()
        # Transform the inner operators and lift the result into a ``$()``
        # value (an ``if`` statement emits nothing when assigned directly).
        yield_suffix = ""
        if "??" in value:
            vmask = _line_mask(value)
            inner_idx = _find_next_op(value, "??=", vmask, True)
            if inner_idx != -1:
                is_, ie_ = _expr_left(value, inner_idx, vmask)
                yield_var = value[is_:ie_].strip()
                value, w = _transform_nca_line(value)
                warnings.extend(w)
                # The chained assignment's statement emits nothing; re-yield
                # the assigned variable so the outer ??= assigns its value.
                yield_suffix = f"; {yield_var}"
            value, w = _transform_nc_line(value)
            warnings.extend(w)
            new_inner = (
                f"if ($null -eq {var}) {{ {var} = $({value}{yield_suffix}) }}"
            )
        else:
            new_inner = f"if ($null -eq {var}) {{ {var} = {value} }}"
        warnings.append(
            f"null-coalescing assignment `{var} ??= ...` "
            f"rewritten to `{new_inner}`"
        )
        line = _emit_replacement(line[:left_start].rstrip(), new_inner) + line[val_end:]
        search = left_start
    return line, warnings


# ===========================================================================
# Transform: null-coalescing  (??)
# ===========================================================================

def _transform_nc_line(line: str) -> tuple[str, list[str]]:
    """Transform every ``??`` on *line* into PS 5.1 compatible ``if`` form."""

    def _builder(left: str, right: str, _extra: None) -> str:
        return f"if ($null -ne {left}) {{ {left} }} else {{ {right} }}"

    return _transform_operator(
        line,
        "??",
        _builder,
        require_value_left=True,
        # ``??`` binds tighter than the ternary and the comma operator, so
        # its operands stop at a co-located ternary's ``?``/``:`` and swallow
        # same-level ``, expr`` continuations; parenthesized ternaries inside
        # an operand are pre-transformed (deep_ternary) so no raw ``? :``
        # survives inside the generated braces (a 5.1 parse error there).
        operand_extra_stop="?:",
        swallow_commas=True,
        deep_ternary=True,
    )


# ===========================================================================
# Transform: ternary  (? :)
# ===========================================================================

def _find_matching_colon(line: str, start: int, mask: bytearray,
                         depth_arr: list[int], ref_depth: int = 0) -> int:
    """Find the colon that separates the true/false branches of a ternary.

    The separator colon sits at the *same* bracket-nesting level as the ternary
    ``?`` (``ref_depth``): ``0`` for a statement-level ternary, ``> 0`` when the
    whole ternary is wrapped in parentheses such as ``($c ? $a : $b)``.  Once the
    depth drops below *ref_depth* the enclosing group has closed without a
    separator colon, so there is no matching ternary and we stop.
    """
    for i in range(start, len(line)):
        if not mask[i]:
            continue
        if depth_arr[i] < ref_depth:
            # The ternary's own group (e.g. the wrapping parenthesis) closed
            # before any separator colon appeared -- not a ternary.
            break
        if line[i] != ":" or depth_arr[i] != ref_depth:
            continue
        if _is_double_colon(line, i) or _is_scope_colon(line, i):
            continue
        if _is_drive_colon(line, i):
            continue
        return i
    return -1


def _brace_open_depths(line: str, mask: bytearray) -> list[int]:
    """Number of unclosed ``{`` before each character of *line*.

    A ``:`` nested inside ``{ ... }`` may belong to hashtable / switch /
    script-block syntax rather than a ternary separator, so the caller skips
    ternary detection when the ``?`` sits at a non-zero brace depth.  Characters
    inside strings / comments (``mask`` == 0) never open or close a brace.
    """
    depths = [0] * len(line)
    bc = 0
    for i, ch in enumerate(line):
        depths[i] = bc
        if mask[i]:
            if ch == "{":
                bc += 1
            elif ch == "}":
                bc -= 1
    return depths


def _transform_ternary_line(line: str) -> tuple[str, list[str]]:
    """Rewrite ternary ``$cond ? $true : $false`` into an ``if`` statement."""
    warnings: list[str] = []
    if "?" not in line:
        return line, warnings
    # Positions already tried and found to be incomplete ternaries (guards
    # passed, separator colon found, but an operand came out empty).  They
    # must not be re-selected on the next restart or the loop would spin.
    rejected: set[int] = set()
    while True:
        mask = _line_mask(line)
        depth_arr = _compute_depths(line, mask)
        # Number of unclosed ``{`` before each character.  A ``:`` inside ``{ ... }``
        # may belong to hashtable / switch / script-block syntax rather than a
        # ternary, so a ternary whose ``?`` sits inside an open brace is left
        # untouched (colons nested in parentheses are unambiguous ternary markers).
        brace_open = _brace_open_depths(line, mask)
        # Rewrite the RIGHTMOST eligible ternary first.  Its branch arms may
        # contain nested ternaries; rewriting inner-to-outer keeps each nested
        # ``? :`` out of the emitted ``{ ... }`` arm blocks, where the brace
        # guard would leave them raw (a 5.1 parse error).  Sequential ternaries
        # on one line are unaffected: every restart re-finds the new rightmost.
        pos = len(line) - 1
        colon_pos = -1
        while pos >= 0:
            if pos in rejected:
                pos -= 1
                continue
            if (
                line[pos] == "?"
                and mask[pos]
                and not _after_dollar_question(line, pos)
            ):
                # ``?`` in command position (the Where-Object alias) is not a
                # ternary: it follows a pipe/operator or starts the statement,
                # so there is no condition expression to its left.
                k = pos - 1
                while k >= 0 and line[k] == " ":
                    k -= 1
                if k < 0 or line[k] in "|&;,":
                    pos -= 1
                    continue
                # Inside an open ``{`` the colon is ambiguous (hashtable /
                # switch / script-block): skip, matching the documented
                # brace-depth limitation — with two unambiguous exceptions:
                # a ternary nested inside ``(...)`` within the braces (colons
                # in parentheses are always ternary separators), and a ternary
                # in ``return``/``throw`` operand position (the keyword
                # precedes the condition, so no hashtable/switch colon can
                # be involved).
                if brace_open[pos] > 0 and depth_arr[pos] - brace_open[pos] == 0:
                    cs, ce = _expr_left(line, pos, mask)
                    # ``return``/``throw`` precede the condition INSIDE the
                    # condition text (``return $c ? 1 : 2``); a keyword-led
                    # condition is the unambiguous operand case.
                    if not re.match(
                        r"(?i)^(?:return|throw)[ \t]", line[cs:ce].lstrip()
                    ):
                        pos -= 1
                        continue
                colon_pos = _find_matching_colon(
                    line, pos + 1, mask, depth_arr, depth_arr[pos]
                )
                if colon_pos == -1:
                    pos -= 1
                    continue
                break
            pos -= 1
        if pos < 0:
            break
        cond_start, cond_end = _expr_left(line, pos, mask)
        condition = line[cond_start:cond_end].strip()
        true_expr = line[pos + 1:colon_pos].strip()
        false_start, false_end = _expr_right(line, colon_pos + 1, mask)
        # PowerShell's comma operator binds tighter than the ternary, so the
        # false branch swallows comma-separated values at the same nesting
        # level: ``@($c ? 7 : 8, 5)`` is ``@($c ? 7 : (8, 5))`` in PS 7 (ONE
        # array element when the condition is true).  _find_expr_end tracks
        # depth relative to its own scan start, so such a comma would
        # otherwise cut the false arm short and change the emitted semantics.
        ref_depth = depth_arr[colon_pos]
        j = false_end
        while j < len(line):
            k = j
            while k < len(line) and line[k] in " \t":
                k += 1
            if not (
                k < len(line)
                and line[k] == ","
                and mask[k]
                and depth_arr[k] == ref_depth
            ):
                break
            nstart = k + 1
            while nstart < len(line) and line[nstart] in " \t":
                nstart += 1
            if nstart >= len(line):
                break
            nend = _find_expr_end(line, nstart, mask)
            if nend <= nstart:
                break
            j = nend
        false_end = j
        false_expr = line[false_start:false_end].strip()
        suffix = line[false_end:]
        if not _match_assignment(line[:cond_start]):
            condition, cond_start = _strip_command_prefix(condition, cond_start, check_keywords=False)
        condition, cond_start = _strip_keyword_operand(condition, cond_start)
        # Region placeholders (mid-line region boundaries spliced by
        # ``pwsh_transform``) are opaque tokens and must not be folded into
        # the emitted arms — re-injecting region text (``#>``) inside the
        # generated ``if`` is a 5.1 parse error.
        keeps: list[str] = []
        condition, found = _lift_region_placeholders(condition)
        keeps.extend(found)
        true_expr, found = _lift_region_placeholders(true_expr)
        keeps.extend(found)
        false_expr, found = _lift_region_placeholders(false_expr)
        keeps.extend(found)
        if not condition or not true_expr or not false_expr:
            # Incomplete ternary (e.g. a bare ``?`` alias invocation that
            # slipped past the position guard): leave it and keep looking
            # for eligible ternaries further left.
            rejected.add(pos)
            continue
        inner = f"if ({condition}) {{ {true_expr} }} else {{ {false_expr} }}"
        if depth_arr[pos] > 0 or suffix.lstrip()[:1] in ("|", "&", ">"):
            # Either the ternary sits in an *expression* position
            # (inside ``(...)``/``[...]``, so ``depth_arr[pos] > 0``) or
            # its value is piped / chained / redirected.  A bare ``if``
            # statement is illegal there: ``(if ...)`` is a parse error
            # in 5.1.  Lift it into a value with ``$( ... )``.
            inner = f"$({inner})"
        warnings.append(
            f"ternary operator `{condition} ? {true_expr} : {false_expr}` "
            f"rewritten to `{inner}`"
        )
        line = _emit_replacement(
            line[:cond_start] + _keeps_prefix(keeps), inner
        ) + suffix
        rejected.clear()  # positions shifted; re-evaluate everything
    return line, warnings


# ===========================================================================
# Transform: pipeline chain operators  (&& / ||)
# ===========================================================================

def _enclosing_group_opener(
    line: str, pos: int, mask: bytearray, depths: list[int], depth: int
) -> str:
    """Return the opener char of the bracket group nesting *pos* at *depth*.

    Returns "" at the top level (``depth`` <= 0) or when no opener is found.
    Used to tell expression positions (``( ... )`` / ``[ ... ]``) from
    scriptblock bodies (``{ ... }``): a chain inside the former must be
    lifted into ``$( ... )`` (a statement sequence is a 5.1 parse error
    inside plain parens), while a chain inside a scriptblock is rewritten in
    place (statements are legal there).
    """
    if depth <= 0:
        return ""
    j = pos - 1
    nested = 0
    while j >= 0:
        if mask[j]:
            ch = line[j]
            if ch in ")]}":
                nested += 1
            elif ch in "([{":
                if nested:
                    nested -= 1
                elif depths[j] == depth - 1:
                    return ch
        j -= 1
    return ""


def _find_interp_op(line: str, op: str, mask: bytearray) -> int:
    """Find the next *op* inside interpolated code (mask value 2)."""
    idx = line.find(op)
    while idx != -1:
        if mask[idx] == 2:
            return idx
        idx = line.find(op, idx + 1)
    return -1


def _mask0_span(line: str, mask: bytearray, k: int) -> tuple[int, int]:
    """Return the maximal mask-0 span (start, end) containing index *k*."""
    s = k
    while s > 0 and not mask[s - 1]:
        s -= 1
    e = k + 1
    n = len(line)
    while e < n and not mask[e]:
        e += 1
    return s, e


def _interp_edge_or_literal(line: str, mask: bytearray, k: int) -> str:
    """Classify the mask-0 character at *k* inside an interpolated region.

    The interpolation's own ``$(`` / ``)`` boundary characters are mask 2, so
    any mask-0 hit while scanning inside one is either a nested string
    literal (starts with ``'``/``"`` — transparent for boundary scans; the
    operand continues past it) or a comment/region edge (a hard boundary).
    Returns ``"literal"`` or ``"edge"``.
    """
    s, _e = _mask0_span(line, mask, k)
    return "literal" if line[s] in "'\"" else "edge"


def _transform_chain_line(line: str) -> tuple[str, list[str]]:
    """Rewrite pipeline chain operators ``&&`` and ``||``.

    Chain operators are LEFT-associative in PowerShell 7
    (``A || B && C`` is ``(A || B) && C``), so each operator is rewritten as
    a *sequential* ``if ($?)`` / ``if (-not $?)`` statement — never nested
    inside a sibling operator's block, which would wrongly drop later
    segments when an earlier condition short-circuits.  Operands are bounded
    by the nearest statement boundary at the operator's nesting depth: a
    ``;`` separator, the opener/closer of the brace/paren group the operator
    sits in, the next chain operator at the same depth, or a trailing
    comment.  Operators inside scriptblocks (``{ ... }``) are rewritten in
    place at their own depth, so a trailing ``&&`` can never leak into a
    scriptblock body.

    Two positions need the sequence lifted into ``$( ... )`` instead:
    expression positions — a chain inside ``( ... )`` / ``[ ... ]`` (its
    combined output is the group's value in PS 7, and bare statements are a
    5.1 parse error there) — and the right-hand side of an assignment (the
    chained segments' combined output is the assigned value).  Chains inside
    interpolated sub-expressions (``"$( A && B )"``) are rewritten with
    region edges as hard boundaries: the statement sequence is valid there.
    """
    warnings: list[str] = []
    if "&&" not in line and "||" not in line:
        return line, warnings

    def _emit(
        prefix: str,
        left_seg: str,
        right_seg: str,
        best_op: str,
        wrap_value: bool,
        suffix: str,
        assign: "re.Match[str] | None" = None,
    ) -> str:
        condition = "$?" if best_op == "&&" else "-not $?"
        if prefix and not prefix[-1].isspace():
            prefix += " "
        if assign is not None:
            # ``$x = A && B`` -> ``$x = $(A; if ($?) { B })``: the variable
            # receives the combined output of the executed segments.
            rest = assign.group(3).strip()
            head = f"{rest}; " if rest else "; "
            return (
                f"{prefix}{assign.group(1)} "
                f"$({head}if ({condition}) {{ {right_seg} }}){suffix}"
            )
        head = f"{left_seg}; " if left_seg else "; "
        inner = f"{head}if ({condition}) {{ {right_seg} }}"
        if wrap_value:
            inner = f"$({inner})"
        return f"{prefix}{inner}{suffix}"

    def _rewrite_segments(left_seg: str, right_seg: str) -> tuple[str, str]:
        # A segment may still contain a chain operator at a DEEPER nesting
        # level (e.g. the ``||`` inside ``A && { B || C }``); rewrite it
        # recursively so no raw operator survives into the 5.1 output.
        if "&&" in left_seg or "||" in left_seg:
            left_seg, wl = _transform_chain_line(left_seg)
            warnings.extend(wl)
        if "&&" in right_seg or "||" in right_seg:
            right_seg, wl = _transform_chain_line(right_seg)
            warnings.extend(wl)
        return left_seg, right_seg

    # Phase 1 — chains in ordinary code (mask 1).  Region characters
    # (mask 0) are transparent for boundary scans.
    while True:
        mask = _line_mask(line)
        and_pos = _find_next_op(line, "&&", mask, plain_code_only=True)
        or_pos = _find_next_op(line, "||", mask, plain_code_only=True)
        if and_pos == -1 and or_pos == -1:
            break
        if and_pos == -1:
            best_pos, best_op = or_pos, "||"
        elif or_pos == -1:
            best_pos, best_op = and_pos, "&&"
        elif and_pos < or_pos:
            best_pos, best_op = and_pos, "&&"
        else:
            best_pos, best_op = or_pos, "||"

        depths = _compute_depths(line, mask)
        depth = depths[best_pos]

        # Left boundary: the nearest ``;`` at the operator's depth, or the
        # opener that established this depth level (its content starts after
        # it).  String/region characters (mask 0) are transparent.
        lo = best_pos
        while lo > 0:
            k = lo - 1
            if mask[k]:
                if depths[k] < depth:
                    break
                if depths[k] == depth and line[k] == ";":
                    break
            lo = k

        # Right boundary: scan forward at the operator's depth, stopping at
        # a statement separator (``;``), the closer ending this depth level,
        # the next chain operator at the same depth, or a trailing comment
        # (which must stay OUTSIDE the generated ``{ }`` block — a ``#``
        # inside braces would comment out the closing ``}``).
        hi = best_pos + 2
        prev_code = True  # the operator itself precedes the right operand
        while hi < len(line):
            c = line[hi]
            cur = mask[hi]
            if not cur:
                if prev_code and c == "#":
                    break
                prev_code = False
                hi += 1
                continue
            prev_code = True
            if depths[hi] < depth:
                break
            if depths[hi] == depth:
                if c == ";" or c in ")]}":
                    break
                if line.startswith("&&", hi) or line.startswith("||", hi):
                    break
            hi += 1

        left_seg = line[lo:best_pos].strip()
        right_seg = line[best_pos + 2:hi].strip()
        left_seg, right_seg = _rewrite_segments(left_seg, right_seg)

        # Expression position: a chain inside ``( ... )`` / ``[ ... ]`` must
        # become a value (statements are a 5.1 parse error inside plain
        # parens; the combined segment output is the group's value in PS 7).
        # An assignment left segment (``$x = A && B``) likewise: the assigned
        # value is the combined output of the executed segments.
        opener = _enclosing_group_opener(line, best_pos, mask, depths, depth)
        # NOTE: ``opener in "(["`` is not enough — "" is a substring of any
        # string, so the empty (top-level) opener must be excluded explicitly.
        wrap_value = bool(opener) and opener in "(["
        assign_split = None
        if not opener and depth == 0:
            m = _CHAIN_ASSIGN_RE.match(left_seg)
            if m and m.group(3).strip():
                assign_split = m

        prefix = line[:lo]
        new_line = _emit(
            prefix, left_seg, right_seg, best_op, wrap_value, line[hi:],
            assign=assign_split,
        )
        warnings.append(
            f"pipeline chain `{left_seg} {best_op} {right_seg}` "
            f"rewritten to `{new_line}`"
        )
        line = new_line

    # Phase 2 — chains inside interpolated sub-expressions ("$( ... )" code,
    # mask 2).  The sequential-statement rewrite IS valid there (verified
    # against 5.1), but region edges are hard boundaries: mask-0 characters
    # end the boundary scans instead of being transparent.
    while True:
        mask = _line_mask(line)
        and_pos = _find_interp_op(line, "&&", mask)
        or_pos = _find_interp_op(line, "||", mask)
        if and_pos == -1 and or_pos == -1:
            break
        if and_pos == -1:
            best_pos, best_op = or_pos, "||"
        elif or_pos == -1:
            best_pos, best_op = and_pos, "&&"
        elif and_pos < or_pos:
            best_pos, best_op = and_pos, "&&"
        else:
            best_pos, best_op = or_pos, "||"

        depths = _compute_depths(line, mask)
        depth = depths[best_pos]

        lo = best_pos
        while lo > 0:
            k = lo - 1
            if not mask[k]:
                # A nested string literal (``$a && 'x'``) is transparent;
                # anything else is the region edge (the interpolation's own
                # ``"$(`` opens here).
                if _interp_edge_or_literal(line, mask, k) == "literal":
                    lo = _mask0_span(line, mask, k)[0]
                    continue
                break
            if depths[k] < depth:
                break
            if depths[k] == depth and line[k] == ";":
                break
            lo = k

        hi = best_pos + 2
        while hi < len(line):
            cur = mask[hi]
            if not cur:
                # A nested string literal (``$a && 'x'``) extends the
                # operand; anything else is the region edge (the
                # interpolation's own ``)" closes here).
                if _interp_edge_or_literal(line, mask, hi) == "literal":
                    hi = _mask0_span(line, mask, hi)[1]
                    continue
                break
            if depths[hi] < depth:
                break
            if depths[hi] == depth:
                if line[hi] == ";" or line[hi] in ")]}":
                    break
                if line.startswith("&&", hi) or line.startswith("||", hi):
                    break
            hi += 1

        left_seg = line[lo:best_pos].strip()
        right_seg = line[best_pos + 2:hi].strip()
        left_seg, right_seg = _rewrite_segments(left_seg, right_seg)
        opener = _enclosing_group_opener(line, best_pos, mask, depths, depth)
        wrap_value = bool(opener) and opener in "(["

        prefix = line[:lo]
        new_line = _emit(
            prefix, left_seg, right_seg, best_op, wrap_value, line[hi:]
        )
        warnings.append(
            f"pipeline chain `{left_seg} {best_op} {right_seg}` "
            f"rewritten to `{new_line}`"
        )
        line = new_line
    return line, warnings


# ===========================================================================
# Null-conditional helpers
# ===========================================================================

def _scan_member_name(line: str, ms: int, mask: bytearray) -> int:
    """Scan a member name starting at *ms*; return the index after it."""
    if ms >= len(line):
        return ms
    c0 = line[ms]
    if c0 == "'":
        return _scan_single_quoted(line, ms)
    if c0 == '"':
        return _scan_double_quoted(line, ms)
    if c0 != "$":
        me = ms
        while me < len(line) and (line[me].isalnum() or line[me] == "_"):
            me += 1
        return me
    # Variable member ($var, ${var}, $? etc.)
    me = ms + 1
    if me >= len(line):
        return me
    ch = line[me]
    if ch == "{":
        bd = 1
        me += 1
        while me < len(line) and bd > 0:
            if line[me] == "{":
                bd += 1
            elif line[me] == "}":
                bd -= 1
            me += 1
    elif ch in "?$^":
        me += 1  # single-char automatic variables ($? $$ $^)
    else:
        while me < len(line) and (line[me].isalnum() or line[me] in "_:"):
            me += 1
    return me


def _scan_method_args(line: str, start: int, mask: bytearray) -> tuple[str, int]:
    """If *start* points to ``(``, scan the method argument list.

    Returns ``(args_string, index_after_closing_paren)``.
    """
    j = start
    while j < len(line) and line[j] == " ":
        j += 1
    if j >= len(line) or line[j] != "(":
        return "", start
    d = 1
    k = j + 1
    while k < len(line) and d > 0:
        if mask[k]:
            if line[k] == "(":
                d += 1
            elif line[k] == ")":
                d -= 1
        k += 1
    return line[j:k], k


# ===========================================================================
# Transform: null-conditional  (?. and ?[)
# ===========================================================================

def _transform_null_conditional_dot(line: str) -> tuple[str, list[str]]:
    """Rewrite null-conditional member access (``?.``)."""
    return _transform_null_conditional_line(line, "?.")


def _transform_null_conditional_bracket(line: str) -> tuple[str, list[str]]:
    """Rewrite null-conditional index access (``?[``)."""
    return _transform_null_conditional_line(line, "?[")


def _transform_null_conditional_line(line: str, op: str) -> tuple[str, list[str]]:
    """Rewrite null-conditional member access (``?.``) or index access (``?[``)."""
    warnings: list[str] = []
    is_dot = op == "?."
    op_len = len(op)
    if op not in line:
        return line, warnings
    search = 0
    while True:
        mask = _line_mask(line)
        idx = _find_next_op(line, op, mask, True, search)
        if idx == -1:
            return line, warnings

        expr_start, expr_end = _expr_left(line, idx, mask, "?:")
        base = line[expr_start:expr_end].strip()
        base, expr_start = _strip_command_prefix(base, expr_start)
        base, expr_start = _strip_keyword_operand(base, expr_start)
        keeps: list[str] = []
        base, found = _lift_region_placeholders(base)
        keeps.extend(found)
        if not base or not _looks_like_value_expr(base):
            # A bareword "base" is 5.1 command text whose ``?`` is a
            # filename wildcard (``rm f?.bak``), not a null-conditional.
            search = idx + op_len
            continue

        if is_dot and "?[" in base:
            # Mixed chain (``$a?[$i]?.b``): the index null-conditional is
            # part of the dot base.  Transform it here so no raw ``?[`` is
            # left inside the generated condition, where the later bracket
            # pass would misread its base (the scan would absorb the
            # ``$null -ne`` operator text).
            base, wl = _transform_null_conditional_line(base, "?[")
            warnings.extend(wl)
            if not base:
                search = idx + op_len
                continue

        if is_dot:
            segments: list[tuple[str, int]] = []  # (member_expr, end_pos)
            prefixes = [base]  # accumulated dotted prefixes
            cur = idx
            while cur < len(line) - 1 and line[cur:cur + 2] == "?.":
                ms = cur + 2
                while ms < len(line) and line[ms] == " ":
                    ms += 1
                me = _scan_member_name(line, ms, mask)
                if me == ms:
                    break
                mem = line[ms:me]
                args, me = _scan_method_args(line, me, mask)
                segments.append((f".{mem}{args}", me))
                prefixes.append(prefixes[-1] + segments[-1][0])
                cur = me
            if not segments:
                search = idx + op_len
                continue
            # Build nested if chain from innermost to outermost
            full_expr = prefixes[-1]  # full dotted expression
            for pfx in reversed(prefixes[:-1]):
                full_expr = f"if ($null -ne {pfx}) {{ {full_expr} }}"
            inner = f"$({full_expr})"
            end_pos = segments[-1][1]
            orig_expr = base + "?." + "?.".join(seg[0][1:] for seg in segments)
        else:
            bracket_depth = 1
            bracket_end = idx + 2
            while bracket_end < len(line) and bracket_depth > 0:
                c = line[bracket_end]
                if mask[bracket_end]:
                    if c == "[":
                        bracket_depth += 1
                    elif c == "]":
                        bracket_depth -= 1
                bracket_end += 1
            index_expr = line[idx + 2:bracket_end - 1]
            inner = f"$(if ($null -ne {base}) {{ {base}[{index_expr}] }})"
            end_pos = bracket_end
            orig_expr = f"{base}?[{index_expr}]"

        kind = "member access" if is_dot else "index"
        warnings.append(
            f"null-conditional {kind} `{orig_expr}` "
            f"rewritten to `{inner}`"
        )
        line = _emit_replacement(
            line[:expr_start] + _keeps_prefix(keeps), inner
        ) + line[end_pos:]
        search = 0


# ===========================================================================
# Transform dispatch
# ===========================================================================

_TRANSFORMS = (
    _transform_nca_line,
    _transform_null_conditional_dot,
    _transform_null_conditional_bracket,
    _transform_nc_line,
    _transform_ternary_line,
    _transform_chain_line,
)


# ===========================================================================
# Public API
# ===========================================================================

def pwsh_transform(code: str) -> tuple[str, list[str]]:
    """Transform PowerShell 7.x syntax into PowerShell 5.1 compatible syntax.

    Returns ``(transformed_code, warnings)`` where *warnings* is a list of
    human-readable messages describing each transformation that was applied.
    """
    code = _join_continuation_lines(code)
    lines = code.split("\n")
    mask = _build_region_mask(code)
    multi = _find_multiline_regions(code, mask, lines)
    # Byte offset where each line starts within *code* (for slicing the
    # full-code mask per line).
    line_starts: list[int] = []
    off = 0
    for ln in lines:
        line_starts.append(off)
        off += len(ln) + 1

    result: list[str] = []
    all_warnings: list[str] = []

    for i, line in enumerate(lines):
        if i in multi:
            # The line is inside a multi-line string/comment/here-string
            # region.  Such regions can open or close *mid-line*, leaving
            # live code before/after them (e.g. a here-string closing ``"@``
            # followed by ``; $y = $a ?? "d"``): a whole-line skip would leave
            # operators in that code raw (5.1 parse errors).  Replace each
            # region span with an opaque placeholder word, run the normal
            # pipeline on the placeholder line (whose mask is then derivable
            # per-line), and substitute the region text back afterwards.
            base = line_starts[i]
            nline = len(line)
            runs: list[tuple[int, int]] = []
            j = 0
            while j < nline:
                if not mask[base + j]:
                    k = j
                    while k < nline and not mask[base + k]:
                        k += 1
                    runs.append((j, k))
                    j = k
                else:
                    j += 1
            if runs == [(0, nline)]:
                # Wholly inside the region — nothing live on this line.
                result.append(line)
                continue
            if runs:
                replacements: list[tuple[str, str]] = []
                parts: list[str] = []
                prev = 0
                for ridx, (s, e) in enumerate(runs):
                    ph = f"__kimixrgn{i}x{ridx}__"
                    replacements.append((ph, line[s:e]))
                    parts.append(line[prev:s])
                    parts.append(ph)
                    prev = e
                parts.append(line[prev:])
                pline = "".join(parts)
                for xform in _TRANSFORMS:
                    pline, w = xform(pline)
                    if w:
                        all_warnings.extend(f"Line {i + 1}: {msg}" for msg in w)
                for ph, orig_text in replacements:
                    pline = pline.replace(ph, orig_text)
                result.append(pline)
                continue
        for xform in _TRANSFORMS:
            line, w = xform(line)
            if w:
                all_warnings.extend(f"Line {i + 1}: {msg}" for msg in w)
        result.append(line)

    return "\n".join(result), all_warnings


def _find_multiline_regions(code: str, mask: bytearray, lines: list[str]) -> set[int]:
    """Return a set of line indices that are inside multi-line regions.

    Multi-line regions are strings, comments, or here-strings that span
    across two or more lines. Lines wholly inside such regions must be
    skipped by line-level transforms.
    """
    multi: set[int] = set()
    i = 0
    n = len(code)
    line_idx = 0
    while i < n:
        if mask[i] == 0:
            start_line = line_idx
            while i < n and mask[i] == 0:
                if code[i] == "\n":
                    line_idx += 1
                i += 1
            if line_idx > start_line:
                multi.update(range(start_line, line_idx + 1))
        else:
            if code[i] == "\n":
                line_idx += 1
            i += 1
    return multi


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
    else:
        text = sys.stdin.read()
    result, warnings = pwsh_transform(text)
    for w in warnings:
        print(f"[WARNING] {w}", file=sys.stderr)
    print(result)
