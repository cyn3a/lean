<#
  Build-Chain.ps1 — adjacent-version breaking-change chain for Lean toolchains.

  Diffs each version against only its immediate neighbour: 4.16 -> 4.17 -> 4.18 -> ...
  That is N-1 diffs instead of N*(N-1), and produces a per-release changelog:
  "what broke in THIS release".

  What it does:
    1. Installs each requested Lean toolchain via elan (idempotent).
    2. Produces ONE snapshot per toolchain, auto-selecting the dumper:
       tries the modern dumper (needs `loadExts`, Lean >= ~4.24);
       falls back to the legacy dumper (older Lean). Snapshots are cached.
    3. Diffs consecutive pairs into causes\<base>__<target>.ndjson and chain.csv,
       one row per hop with a column per cause class.

  IMPORTANT — a chain is not a decomposition of a long jump. Summing the hops
  OVERCOUNTS a direct migration, because it also counts churn: things that changed
  and changed back, and constants that only ever existed in an intermediate version.
  Measured on 4.16 -> 4.24 -> 4.32: hops sum to 33,313 breaks but the direct
  4.16 -> 4.32 diff is 19,084. Use -WithEndpoints to also emit the first-vs-last
  diff (one extra diff) when you care about the big jump as well as each hop.

  Example:
    .\Build-Chain.ps1 -VersionFile versions.txt -WithEndpoints
    .\Build-Chain.ps1 -Versions 4.16.0,4.24.0,4.32.0
#>

param(
  [string[]] $Versions,                        # e.g. -Versions 4.16.0,4.24.0,4.32.0
  [string]   $VersionFile   = "versions.txt",  # used only if -Versions is not given
  [string]   $Modules       = "Init Lean",     # modules each snapshot imports
  [string]   $ModernDumper  = "EnvSnapshot.lean",
  [string]   $LegacyDumper  = "EnvSnapshot_legacy.lean",
  [string]   $Differ        = "EnvDiff.lean",
  [string]   $SnapDir       = "snapshots",
  [string]   $CauseDir      = "causes",
  [string]   $ChainCsv      = "chain.csv",
  [switch]   $WithEndpoints,                   # also diff first vs last (1 extra diff)
  [switch]   $Downgrade,                       # chain backwards too (target -> base)
  [switch]   $Reinstall                        # force toolchain reinstall
)

$ErrorActionPreference = "Stop"

# --- preflight -------------------------------------------------------------
if (-not (Get-Command elan -ErrorAction SilentlyContinue)) {
  throw "elan not found on PATH. Install from https://github.com/leanprover/elan"
}
foreach ($f in @($ModernDumper, $LegacyDumper, $Differ)) {
  if (-not (Test-Path $f)) { throw "missing required file: $f" }
}

if (-not $Versions -or $Versions.Count -eq 0) {
  if (-not (Test-Path $VersionFile)) {
    throw "no -Versions given and $VersionFile not found (one version per line)."
  }
  $Versions = Get-Content $VersionFile |
              ForEach-Object { $_.Trim() } |
              Where-Object   { $_ -and -not $_.StartsWith('#') }
}
# Sort by real version order so 4.9.0 < 4.10.0 (a string sort gets this wrong).
$Versions = $Versions | Sort-Object { [version]$_ }

New-Item -ItemType Directory -Force -Path $SnapDir, $CauseDir | Out-Null
$moduleArgs = $Modules -split '\s+'

# The closed cause set, mirroring `allCauses` in EnvDiff.lean. One CSV column each.
$AllCauses = @(
  'const.absent','const.renamed','const.kind-changed','const.arity-changed',
  'const.binders-changed','const.universes-changed','const.type-changed',
  'const.class-changed','const.reducibility-changed','const.protected-changed',
  'const.fields-changed','const.ctors-changed',
  'instance.absent','instance.priority-changed',
  'deprecation.alias-absent','tactic.absent',
  'syntax.category-absent','syntax.token-absent','syntax.kind-absent','token.absent',
  'attr.absent','option.absent','option.default-changed',
  'simp.lemma-absent','simp.unfold-absent'
)
# Causes that produce NO compiler error — read these by hand however small the count.
$SilentCauses = @('option.default-changed','const.reducibility-changed',
                  'instance.priority-changed','simp.lemma-absent','simp.unfold-absent')

function Invoke-Lean {
  param([string]$Version, [string[]]$LeanArgs, [string]$LogPath)
  & elan run "leanprover/lean4:v$Version" lean @LeanArgs *> $LogPath
  return $LASTEXITCODE
}

# --- steps 1+2: install toolchains, build/cache snapshots ------------------
$ok = @()
foreach ($v in $Versions) {
  $snap = Join-Path $SnapDir "snap_$v.ndjson"
  if ((Test-Path $snap) -and (Get-Item $snap).Length -gt 0) {
    Write-Host "[$v] cached snapshot, reuse"; $ok += $v; continue
  }
  $tc = "leanprover/lean4:v$v"
  if ($Reinstall -or -not (elan toolchain list | Select-String -SimpleMatch $tc)) {
    Write-Host "[$v] installing toolchain ..."
    & elan toolchain install $tc *> (Join-Path $SnapDir "install_$v.txt")
    if ($LASTEXITCODE -ne 0) { Write-Warning "[$v] install failed; skipping"; continue }
  }
  # Modern first, legacy as fallback. Exit codes are distinct where it matters:
  #   modern on new Lean -> 0 ; modern on old Lean -> compile error (nonzero)
  #   legacy on old Lean -> 0 ; legacy on new Lean -> empty-extension self-check (3)
  $log = Join-Path $SnapDir "log_$v.txt"; $used = $null
  foreach ($dumper in @($ModernDumper, $LegacyDumper)) {
    $code = Invoke-Lean -Version $v -LeanArgs (@('--run',$dumper,$snap) + $moduleArgs) -LogPath $log
    if ($code -eq 0) { $used = $dumper; break }
  }
  if ($used) { Write-Host "[$v] snapshot OK via $used"; $ok += $v }
  else { Write-Warning "[$v] both dumpers failed (see $log) -> new API breakpoint at this version" }
}
if ($ok.Count -lt 2) { throw "need at least two good snapshots; got $($ok.Count)." }

$diffVer = $ok[-1]
Write-Host "diffing under $diffVer"

# --- step 3: diff consecutive pairs ---------------------------------------
# Count "break" records per cause with plain string ops (fast; avoids per-line JSON parsing).
# Json.mkObj emits keys alphabetically, so "cause" is always the first field.
function Get-CauseCounts {
  param([string]$Path)
  $counts = @{}; foreach ($c in $AllCauses) { $counts[$c] = 0 }
  $total = 0
  Get-Content $Path -ReadCount 5000 | ForEach-Object {
    foreach ($line in $_) {
      if ($line.Contains('"sev":"break"')) {
        $i = $line.IndexOf('"cause":"'); if ($i -lt 0) { continue }
        $i += 9; $j = $line.IndexOf('"', $i); if ($j -lt 0) { continue }
        $c = $line.Substring($i, $j - $i)
        if ($counts.ContainsKey($c)) { $counts[$c]++ } else { $counts[$c] = 1 }
        $total++
      }
    }
  }
  return @{ Total = $total; Counts = $counts }
}

function Invoke-Pair {
  param([string]$Base, [string]$Target)
  $out = Join-Path $CauseDir "${Base}__${Target}.ndjson"
  $null = Invoke-Lean -Version $diffVer `
            -LeanArgs @('--run',$Differ,(Join-Path $SnapDir "snap_$Base.ndjson"),
                        (Join-Path $SnapDir "snap_$Target.ndjson"),$out) `
            -LogPath (Join-Path $CauseDir "log_${Base}__${Target}.txt")
  $r = Get-CauseCounts -Path $out
  $silent = 0; foreach ($s in $SilentCauses) { $silent += $r.Counts[$s] }
  $row = [ordered]@{ from = $Base; to = $Target; breaks = $r.Total; silent = $silent }
  foreach ($c in $AllCauses) { $row[$c] = $r.Counts[$c] }
  Write-Host ("  {0,-9} -> {1,-9} : {2,7} breaks  ({3} silent)" -f $Base,$Target,$r.Total,$silent)
  return [pscustomobject]$row
}

$rows = @()
Write-Host "=== chain (adjacent hops) ==="
for ($i = 0; $i -lt $ok.Count - 1; $i++) {
  $rows += Invoke-Pair -Base $ok[$i] -Target $ok[$i+1]
  if ($Downgrade) { $rows += Invoke-Pair -Base $ok[$i+1] -Target $ok[$i] }
}
if ($WithEndpoints -and $ok.Count -gt 2) {
  Write-Host "=== endpoint (first vs last: the direct jump) ==="
  $rows += Invoke-Pair -Base $ok[0] -Target $ok[-1]
}

$rows | Export-Csv -Path $ChainCsv -NoTypeInformation -Encoding UTF8
Write-Host ""
Write-Host "wrote $ChainCsv (one row per hop, one column per cause class)"
Write-Host "     $CauseDir\*.ndjson (per-hop cause sets), $SnapDir\snap_*.ndjson (reusable)"
if ($WithEndpoints -and $ok.Count -gt 2) {
  $chainSum = ($rows | Where-Object { $_.from -ne $ok[0] -or $_.to -ne $ok[-1] } |
               Measure-Object -Property breaks -Sum).Sum
  $direct   = ($rows | Where-Object { $_.from -eq $ok[0] -and $_.to -eq $ok[-1] }).breaks
  Write-Host ""
  Write-Host "hops sum to $chainSum breaks; the direct $($ok[0]) -> $($ok[-1]) diff is $direct."
  Write-Host "The gap is churn (changed-and-changed-back, plus intermediate-only constants)."
  Write-Host "Per-hop rows answer 'what broke in this release'; the endpoint row answers"
  Write-Host "'what breaks if I jump straight there'. They are different questions."
}
