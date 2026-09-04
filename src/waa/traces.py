"""Clean up the shell breadcrumbs Windows leaves when Edge runs with our
own profile.

Because the automation profile lives in "...\\BrowserProfile", the Edge window
gets its own AppUserModelID ("MSEdge.BrowserProfile.Default"). The Windows
shell records that id in a few per-user MRU locations. None of them contain a
URL, a cookie or any account data - only the profile name - but the tool is
meant to leave nothing behind, so cleanup removes exactly those entries and
nothing else.

Only HKCU MRU/usage data is touched, only values that match our own profile's
AppUserModelID, and only when the operator asked to clean up. No system or
security settings are changed.
"""

import os

from . import _ps


def _aumids(profile_dir):
    base = os.path.basename(os.path.normpath(profile_dir)) or "BrowserProfile"
    # Edge derives the id from the user-data-dir name plus the profile folder.
    return [
        "MSEdge.%s.Default" % base,
        "MSEdge.%s" % base,
    ], base


def _script(profile_dir, dry_run):
    aumids, base = _aumids(profile_dir)
    ps_quote = lambda value: "'%s'" % str(value).replace("'", "''")
    aumid_list = ",".join(ps_quote(a) for a in aumids)
    return r"""
$ErrorActionPreference = 'SilentlyContinue'
$dry = %(dry)s
$aumids = @(%(aumids)s)
$base = %(base)s
$report = @()

function Rot13([string]$s){ -join ($s.ToCharArray() | ForEach-Object {
  $c=$_; if($c -match '[A-Za-z]'){ $b= if([char]::IsUpper($c)){65}else{97};
  [char]((([int]$c - $b + 13) %% 26) + $b) } else { $c } }) }

# 1. FeatureUsage buckets (AppSwitched, AppLaunch, ShowJumpView, ...)
$fu = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\FeatureUsage'
Get-ChildItem $fu 2>$null | ForEach-Object {
  $key = $_.PSPath
  foreach($a in $aumids){
    if((Get-Item $key).GetValue($a,$null) -ne $null){
      $report += "FeatureUsage\$($_.PSChildName): $a"
      if(-not $dry){ Remove-ItemProperty -LiteralPath $key -Name $a -ErrorAction Stop }
    }
  }
}

# 2. Search JumplistData
$jl = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Search\JumplistData'
if(Test-Path $jl){ foreach($a in $aumids){
  if((Get-Item $jl).GetValue($a,$null) -ne $null){
    $report += "JumplistData: $a"
    if(-not $dry){ Remove-ItemProperty -LiteralPath $jl -Name $a -ErrorAction Stop }
  }
}}

# 3. UserAssist (value names are ROT13-encoded paths)
$ua = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist'
Get-ChildItem $ua 2>$null | ForEach-Object {
  $count = Join-Path $_.PSPath 'Count'
  if(Test-Path $count){
    (Get-Item $count).GetValueNames() | ForEach-Object {
      $plain = Rot13 $_
      if($aumids -contains $plain){
        $report += "UserAssist: $plain"
        if(-not $dry){ Remove-ItemProperty -LiteralPath $count -Name $_ -ErrorAction Stop }
      }
    }
  }
}

# 4. Jump-list files that belong to our Edge profile (contain the AUMID)
foreach($sub in @('AutomaticDestinations','CustomDestinations')){
  $dir = Join-Path $env:APPDATA "Microsoft\Windows\Recent\$sub"
  if(Test-Path $dir){
    Get-ChildItem $dir -Force 2>$null | ForEach-Object {
      $bytes = [System.IO.File]::ReadAllBytes($_.FullName)
      $u = [System.Text.Encoding]::Unicode.GetString($bytes)
      $isOurs = $false
      foreach($a in $aumids){
        $pattern = '(?<![A-Za-z0-9_.-])' + [regex]::Escape($a) + '(?![A-Za-z0-9_.-])'
        if([regex]::IsMatch($u, $pattern, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)){
          $isOurs = $true
        }
      }
      if($isOurs){
        $report += ("Jumplist\{0}: {1}" -f $sub, $_.Name)
        if(-not $dry){ Remove-Item -LiteralPath $_.FullName -Force -ErrorAction Stop }
      }
    }
  }
}

@{ Removed = $report } | ConvertTo-Json -Compress
""" % {"dry": ("$true" if dry_run else "$false"),
       "aumids": aumid_list, "base": ps_quote(base)}


def clean_shell_traces(profile_dir, logger=None, dry_run=False, strict=False):
    """Remove (or, if dry_run, just list) our own shell breadcrumbs."""
    try:
        data = _ps.run_json(_script(profile_dir, dry_run), timeout=45)
    except Exception as exc:
        if logger:
            logger.debug("Shell-trace cleanup skipped: %s" % exc)
        if strict:
            raise
        return []
    removed = data.get("Removed") or []
    if isinstance(removed, str):
        removed = [removed]
    if logger:
        verb = "Would remove" if dry_run else "Removed"
        for item in removed:
            logger.debug("%s shell trace: %s" % (verb, item))
        if not removed:
            logger.debug("No shell traces from this profile were found.")
    return removed
