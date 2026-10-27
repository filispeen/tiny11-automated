<#
.SYNOPSIS
    UUP to Windows Setup Directory & WIM Converter for Tiny11 Automated

.DESCRIPTION
    Assembles a bootable Windows 11 ISO layout (setup root, boot.wim, and install.wim)
    directly from downloaded UUP ESD/CAB files using wimlib-imagex, avoiding the need
    to download or unpack a pre-built 6.5 GB ISO. Also handles post-mount virtual
    edition upgrades (e.g. Professional -> Education) and Cumulative Update / EKB
    servicing on the mounted image.

.NOTES
    Author: kelexine (https://github.com/kelexine)
#>

function Write-UUPLog {
    param([string]$Message, [string]$Level = "INFO")
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $logLine = "[$timestamp] [$Level] $Message"
    Write-Host $logLine
    if ($logFile -and (Test-Path (Split-Path $logFile -Parent -ErrorAction SilentlyContinue))) {
        Add-Content -Path $logFile -Value $logLine -ErrorAction SilentlyContinue
    }
}

function Ensure-Wimlib {
    [CmdletBinding()]
    param()

    $candidates = @(
        (Get-Command "wimlib-imagex.exe" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
        (Get-Command "wimlib-imagex" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
        (Join-Path $PSScriptRoot "wimlib-imagex.exe"),
        "C:\ProgramData\chocolatey\bin\wimlib-imagex.exe",
        (Join-Path $env:TEMP "wimlib\wimlib-imagex.exe")
    )

    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) {
            return $c
        }
    }

    Write-UUPLog "wimlib-imagex not found in PATH. Installing wimlib..." -Level Info
    $choco = Get-Command "choco.exe" -ErrorAction SilentlyContinue
    if ($choco) {
        & choco install wimlib -y --no-progress | Out-Null
        $chocoWimlib = "C:\ProgramData\chocolatey\bin\wimlib-imagex.exe"
        if (Test-Path $chocoWimlib) {
            return $chocoWimlib
        }
    }

    $wimlibDir = Join-Path $env:TEMP "wimlib"
    $wimlibZip = Join-Path $env:TEMP "wimlib.zip"
    New-Item -ItemType Directory -Path $wimlibDir -Force | Out-Null
    $url = "https://wimlib.net/downloads/wimlib-1.14.4-windows-x86_64-bin.zip"
    Invoke-WebRequest -Uri $url -OutFile $wimlibZip -UseBasicParsing
    Expand-Archive -Path $wimlibZip -DestinationPath $wimlibDir -Force
    Remove-Item $wimlibZip -Force -ErrorAction SilentlyContinue

    $exe = Get-ChildItem -Path $wimlibDir -Filter "wimlib-imagex.exe" -Recurse | Select-Object -First 1
    if (-not $exe) {
        throw "Failed to locate wimlib-imagex.exe after downloading wimlib."
    }
    return $exe.FullName
}

function Expand-UUPCabinet {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$CabPath,

        [Parameter(Mandatory = $true)]
        [string]$DestinationDir
    )

    New-Item -ItemType Directory -Path $DestinationDir -Force | Out-Null
    & expand.exe $CabPath -F:* $DestinationDir *> $null
    $items = Get-ChildItem -Path $DestinationDir -Force -ErrorAction SilentlyContinue
    if (-not $items) {
        $fallbackFile = Join-Path $DestinationDir ([System.IO.Path]::GetFileNameWithoutExtension($CabPath))
        & expand.exe $CabPath $fallbackFile *> $null
    }
}

function Get-UUPCabinetClassification {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$UUPDir
    )

    $fodCabs = @()
    $setupDuCabs = @()
    $safeOsDuCabs = @()
    $osUpdatePackages = @()

    $allFiles = Get-ChildItem -Path $UUPDir -File
    foreach ($f in $allFiles) {
        $name = $f.Name
        $low = $name.ToLowerInvariant()

        if ($low -match 'aggregatedmetadata|desktopdeployment') {
            continue
        }

        if ($low.EndsWith(".msu")) {
            $osUpdatePackages += $f
            continue
        }

        if ($low.EndsWith(".cab")) {
            $isKbOrSsu = ($low -match 'kb\d+' -or $low.StartsWith("ssu-"))
            if (-not $isKbOrSsu) {
                $fodCabs += $f
                continue
            }

            # Inspect KB CAB listing to distinguish Setup DU and SafeOS DU from OS updates
            $listing = (& expand.exe -D $f.FullName 2>$null) -join "`n"
            if ($listing -match 'SetupHost\.exe|setup\.exe' -and $listing -notmatch 'winsxs') {
                $setupDuCabs += $f
            }
            elseif ($listing -match 'winpeshl\.ini|reagent\.xml' -and $listing -notmatch 'SetupHost\.exe') {
                $safeOsDuCabs += $f
            }
            else {
                $osUpdatePackages += $f
            }
        }
    }

    # Sort OS updates: SSU first, then ascending by KB number so EKBs/prereqs install before LCUs
    $sortedUpdates = $osUpdatePackages | Sort-Object -Property @(
        @{ Expression = { if ($_.Name -match '^SSU-') { 0 } elseif ($_.Extension -eq '.cab') { 1 } else { 2 } } },
        @{ Expression = { if ($_.Name -match 'KB(\d+)') { [int]$Matches[1] } else { 0 } } }
    )

    return @{
        FodCabs          = $fodCabs
        SetupDuCabs      = $setupDuCabs
        SafeOsDuCabs     = $safeOsDuCabs
        OSUpdatePackages = $sortedUpdates
    }
}

function Initialize-WorkspaceFromUUP {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$UUPDir,

        [Parameter(Mandatory = $true)]
        [string]$TargetIsoDir,

        [Parameter(Mandatory = $true)]
        [string]$ScratchDir,

        [int]$ImageIndex = 1
    )

    $UUPDir = (Resolve-Path $UUPDir).Path
    $wimlib = Ensure-Wimlib
    Write-UUPLog "Using wimlib-imagex at: $wimlib" -Level Info

    # 1. Load uup_metadata.json if present
    $metaCandidates = @(
        (Join-Path $UUPDir "uup_metadata.json"),
        (Join-Path (Split-Path $UUPDir -Parent) "uup_metadata.json"),
        (Join-Path (Get-Location).Path "uup_metadata.json")
    )
    $uupMeta = $null
    foreach ($mc in $metaCandidates) {
        if ($mc -and (Test-Path $mc)) {
            try {
                $uupMeta = Get-Content $mc -Raw | ConvertFrom-Json
                Write-UUPLog "Loaded UUP metadata from $mc (Build: $($uupMeta.windows_build), Edition: $($uupMeta.uup_edition))" -Level Info
                break
            }
            catch {
                Write-UUPLog "Warning: could not parse ${mc}: $($_.Exception.Message)" -Level Warning
            }
        }
    }

    # 2. Locate the primary edition metadata ESD (contains Index 1=Setup, Index 2=WinRE, Index 3=OS)
    $expectedEditionPrefix = switch ($ImageIndex) {
        1 { "core_" }
        4 { "professional_" }
        6 { "professional_" }
        7 { "professionaln_" }
        default { "core_" }
    }
    if ($uupMeta -and $uupMeta.uup_edition) {
        $expectedEditionPrefix = "$($uupMeta.uup_edition.ToLowerInvariant())_"
    }

    $esdFiles = Get-ChildItem -Path $UUPDir -Filter "*.esd" -File
    $metadataEsd = $esdFiles | Where-Object { $_.Name.ToLowerInvariant().StartsWith($expectedEditionPrefix) } | Select-Object -First 1

    if (-not $metadataEsd) {
        foreach ($candidate in $esdFiles) {
            & $wimlib info $candidate.FullName 3 *> $null
            if ($LASTEXITCODE -eq 0) {
                $metadataEsd = $candidate
                break
            }
        }
    }

    if (-not $metadataEsd) {
        throw "Could not find a valid UUP edition metadata ESD (e.g. ${expectedEditionPrefix}<lang>.esd) in $UUPDir"
    }
    Write-UUPLog "Selected UUP metadata ESD: $($metadataEsd.Name)" -Level Info

    # 3. Classify CAB/MSU packages and convert FOD CABs into referenceable ESDs
    $classified = Get-UUPCabinetClassification -UUPDir $UUPDir
    $tempEsdDir = Join-Path $UUPDir "_temp_esds"
    New-Item -ItemType Directory -Path $tempEsdDir -Force | Out-Null

    if ($classified.FodCabs.Count -gt 0) {
        Write-UUPLog "Converting $($classified.FodCabs.Count) Feature-on-Demand CAB package(s) into temporary ESDs..." -Level Info
        if ($PSVersionTable.PSVersion.Major -ge 7) {
            $cabsToProcess = @($classified.FodCabs | Where-Object { -not (Test-Path (Join-Path $tempEsdDir "$($_.BaseName).esd")) })
            $cabsToProcess | ForEach-Object -ThrottleLimit 4 -Parallel {
                $cab = $_
                $tempDir = $using:tempEsdDir
                $wim = $using:wimlib
                $esd = Join-Path $tempDir "$($cab.BaseName).esd"
                $extractDir = Join-Path $tempDir "ext_$($cab.BaseName)"

                New-Item -ItemType Directory -Path $extractDir -Force | Out-Null
                & expand.exe $cab.FullName -F:* $extractDir *> $null
                $items = Get-ChildItem -Path $extractDir -Force -ErrorAction SilentlyContinue
                if (-not $items) {
                    $fallbackFile = Join-Path $extractDir ([System.IO.Path]::GetFileNameWithoutExtension($cab.FullName))
                    & expand.exe $cab.FullName $fallbackFile *> $null
                }
                & $wim capture $extractDir $esd --compress=none --no-acls --norpfix "Edition Package" "Edition Package" *> $null
                Remove-Item -Path $extractDir -Recurse -Force -ErrorAction SilentlyContinue
            }
        } else {
            foreach ($cab in $classified.FodCabs) {
                $esdOut = Join-Path $tempEsdDir "$($cab.BaseName).esd"
                if (Test-Path $esdOut) { continue }

                $cabExtractDir = Join-Path $tempEsdDir "ext_$($cab.BaseName)"
                Expand-UUPCabinet -CabPath $cab.FullName -DestinationDir $cabExtractDir
                & $wimlib capture $cabExtractDir $esdOut --compress=none --no-acls --norpfix "Edition Package" "Edition Package" *> $null
                Remove-Item -Path $cabExtractDir -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }

    # 4. Create ISO root structure from Index 1 of metadata ESD
    Write-UUPLog "Applying UUP Setup Media root (Index 1) to $TargetIsoDir..." -Level Info
    New-Item -ItemType Directory -Path $TargetIsoDir -Force | Out-Null
    & $wimlib apply $metadataEsd.FullName 1 $TargetIsoDir --no-acls --norpfix | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "wimlib-imagex failed to apply Setup Media root from $($metadataEsd.Name)"
    }

    # Remove ei.cfg if present so Windows Setup works seamlessly
    $eiCfg = Join-Path $TargetIsoDir "sources\ei.cfg"
    if (Test-Path $eiCfg) {
        Remove-Item $eiCfg -Force -ErrorAction SilentlyContinue
    }

    # Extract boot fonts from Index 3 into boot\fonts and efi\microsoft\boot\fonts
    $fontsTempDir = Join-Path $tempEsdDir "fonts_extract"
    New-Item -ItemType Directory -Path $fontsTempDir -Force | Out-Null
    & $wimlib extract $metadataEsd.FullName 3 "/Windows/Boot/Fonts" --ref="$UUPDir\*.esd" --ref="$tempEsdDir\*.esd" --dest-dir="$fontsTempDir" --no-acls --norpfix *> $null
    $extractedFonts = Join-Path $fontsTempDir "Fonts"
    if (Test-Path $extractedFonts) {
        $bootFonts = Join-Path $TargetIsoDir "boot\fonts"
        $efiFonts = Join-Path $TargetIsoDir "efi\microsoft\boot\fonts"
        New-Item -ItemType Directory -Path $bootFonts -Force | Out-Null
        New-Item -ItemType Directory -Path $efiFonts -Force | Out-Null
        Copy-Item -Path "$extractedFonts\*" -Destination $bootFonts -Force -ErrorAction SilentlyContinue
        Copy-Item -Path "$extractedFonts\*" -Destination $efiFonts -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -Path $fontsTempDir -Recurse -Force -ErrorAction SilentlyContinue

    # Apply Setup Dynamic Update CABs (if any) directly to sources/
    foreach ($sdu in $classified.SetupDuCabs) {
        Write-UUPLog "Applying Setup Dynamic Update: $($sdu.Name)" -Level Info
        Expand-UUPCabinet -CabPath $sdu.FullName -DestinationDir (Join-Path $TargetIsoDir "sources")
    }

    # 5. Build winre.wim and sources\boot.wim (Index 1: Windows PE, Index 2: Windows Setup)
    Write-UUPLog "Building winre.wim and sources\boot.wim from UUP Index 2..." -Level Info
    $winreWim = Join-Path $tempEsdDir "winre.wim"
    $bootWim = Join-Path $TargetIsoDir "sources\boot.wim"
    New-Item -ItemType Directory -Path (Split-Path $bootWim -Parent) -Force | Out-Null

    & $wimlib export $metadataEsd.FullName 2 $winreWim --compress=maximum --boot | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "wimlib-imagex failed to export winre.wim from $($metadataEsd.Name)"
    }

    Copy-Item -Path $winreWim -Destination $bootWim -Force
    & $wimlib info $bootWim 1 "Microsoft Windows PE (x64)" "Microsoft Windows PE (x64)" --image-property FLAGS=9 *> $null
    & $wimlib export $metadataEsd.FullName 2 $bootWim "Microsoft Windows Setup (x64)" "Microsoft Windows Setup (x64)" --compress=maximum --boot | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "wimlib-imagex failed to export Index 2 into sources\boot.wim"
    }
    & $wimlib info $bootWim 2 --image-property FLAGS=2 *> $null

    # Configure boot.wim Index 2 (Windows Setup environment: WinPE paths + setup sources)
    $peWorkDir = Join-Path $tempEsdDir "pe_setup_work"
    New-Item -ItemType Directory -Path $peWorkDir -Force | Out-Null
    & $wimlib extract $bootWim 2 "/Windows/System32/config/SYSTEM" --dest-dir="$peWorkDir" --no-acls --norpfix *> $null
    $peSystemHive = Join-Path $peWorkDir "SYSTEM"
    if (Test-Path $peSystemHive) {
        & reg.exe load "HKLM\UUP_PE_SYS" $peSystemHive *> $null
        & reg.exe add "HKLM\UUP_PE_SYS\ControlSet001\Control\WinPE" /v InstRoot /t REG_SZ /d "X:\`$windows.~bt\" /f *> $null
        & reg.exe add "HKLM\UUP_PE_SYS\ControlSet001\Control\WinPE" /v SystemRoot /t REG_SZ /d "X:\`$windows.~bt\Windows" /f *> $null
        & reg.exe unload "HKLM\UUP_PE_SYS" *> $null
    }

    $bootSourcesList = @(
        "background_cli.bmp", "background_svr.bmp", "compatprovider.dll", "cryptosetup.dll",
        "dism.exe", "dismapi.dll", "dismcore.dll", "dismcoreps.dll", "dismprov.dll",
        "ext-ms-win-advapi32-encryptedfile-l1-1-0.dll", "folderprovider.dll", "hwreqchk.dll",
        "idwbinfo.txt", "imagingprovider.dll", "input.dll", "lang.ini", "locale.nls",
        "logprovider.dll", "mediaetw.dll", "ndiscompl.dll", "nlsbres.dll", "ntdsbmsg.dll",
        "osfilter.dll", "pnpibs.dll", "reagent.admx", "reagent.dll", "reagent.xml",
        "rollback.exe", "schema.dat", "segoeui.ttf", "setup.exe", "setupcompat.dll",
        "setupcore.dll", "setuphost.exe", "setupmgr.dll", "setupplatform.cfg",
        "setupplatform.dll", "setupplatform.exe", "setupprep.exe", "kelijke.dll",
        "smiengine.dll", "spflvrnt.dll", "spprgrss.dll", "spwizeng.dll", "spwizimg.dll",
        "spwizres.dll", "sqmapi.dll", "testplugin.dll", "unattend.dll", "unbcl.dll",
        "upgloader.dll", "upgradeagent.dll", "upgradeagent.xml", "uxlib.dll", "uxlibres.dll",
        "vhdprovider.dll", "w32uiimg.dll", "w32uires.dll", "warning.bmp", "wdsclient.dll",
        "wdsclientapi.dll", "wdscore.dll", "wdscsl.dll", "wdsimage.dll", "wdstptc.dll",
        "wdsutil.dll", "wimprovider.dll", "win32ui.dll", "winsetup.dll", "wpx.dll", "xmllite.dll"
    )

    $updateCmdFile = Join-Path $peWorkDir "boot_update_cmds.txt"
    $cmdLines = New-Object System.Collections.Generic.List[string]
    $cmdLines.Add("delete '/Windows/System32/winpeshl.ini' --force")
    if (Test-Path $peSystemHive) {
        $cmdLines.Add("add '$peSystemHive' '/Windows/System32/config/SYSTEM'")
    }

    $isoSourcesDir = Join-Path $TargetIsoDir "sources"
    $bmpFile = Join-Path $isoSourcesDir "background_cli.bmp"
    if (-not (Test-Path $bmpFile)) {
        $bmpFile = Join-Path $isoSourcesDir "background_svr.bmp"
    }
    if (Test-Path $bmpFile) {
        $cmdLines.Add("add '$bmpFile' '/sources/background.bmp'")
        $cmdLines.Add("add '$bmpFile' '/Windows/system32/setup.bmp'")
    }

    foreach ($relFile in $bootSourcesList) {
        $fullCandidate = Join-Path $isoSourcesDir $relFile
        if (Test-Path $fullCandidate) {
            $cmdLines.Add("add '$fullCandidate' '/sources/$relFile'")
        }
    }

    # Also copy language resources (e.g. en-us/*.mui) for setup files present in bootSourcesList
    $langDirs = Get-ChildItem -Path $isoSourcesDir -Directory -ErrorAction SilentlyContinue
    foreach ($ld in $langDirs) {
        foreach ($relFile in $bootSourcesList) {
            $muiCandidate = Join-Path $ld.FullName "$relFile.mui"
            if (Test-Path $muiCandidate) {
                $cmdLines.Add("add '$muiCandidate' '/sources/$($ld.Name)/$relFile.mui'")
            }
        }
    }

    Set-Content -Path $updateCmdFile -Value ($cmdLines -join "`r`n") -Encoding UTF8
    & $wimlib update $bootWim 2 "<$updateCmdFile" *> $null
    & $wimlib optimize $bootWim *> $null
    Remove-Item -Path $peWorkDir -Recurse -Force -ErrorAction SilentlyContinue

    # 6. Synthesize sources\install.wim (Index 1) from UUP ESDs
    $installWim = Join-Path $TargetIsoDir "sources\install.wim"
    Write-UUPLog "Synthesizing sources\install.wim from UUP packages via wimlib-imagex..." -Level Info
    & $wimlib export $metadataEsd.FullName 3 $installWim --ref="$UUPDir\*.esd" --ref="$tempEsdDir\*.esd" --compress=fast | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "wimlib-imagex failed to synthesize install.wim from UUP ESD packages."
    }

    if (Test-Path $winreWim) {
        & $wimlib update $installWim 1 --command="add '$winreWim' '/Windows/System32/Recovery/winre.wim'" *> $null
    }

    # Clean up temporary FOD ESDs to reclaim disk space before mounting
    Remove-Item -Path $tempEsdDir -Recurse -Force -ErrorAction SilentlyContinue

    $virtualEdition = $null
    if ($ImageIndex -eq 4) {
        $virtualEdition = "Education"
    }
    elseif ($uupMeta -and $uupMeta.virtual_edition) {
        $virtualEdition = $uupMeta.virtual_edition
    }

    $detectedBuild = $null
    if ($uupMeta -and $uupMeta.windows_build) {
        $detectedBuild = [string]$uupMeta.windows_build
    }

    Write-UUPLog "UUP workspace initialized successfully at $TargetIsoDir (install.wim Index 1 ready)." -Level Info
    return @{
        WimlibPath       = $wimlib
        MetadataEsd      = $metadataEsd.FullName
        InstallWimPath   = $installWim
        BootWimPath      = $bootWim
        VirtualEdition   = $virtualEdition
        BuildNumber      = $detectedBuild
        OSUpdatePackages = $classified.OSUpdatePackages
        Metadata         = $uupMeta
    }
}

function Invoke-UUPPostMountServicing {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$ScratchDir,

        [Parameter(Mandatory = $true)]
        [string]$TargetIsoDir,

        [Parameter(Mandatory = $true)]
        [string]$UUPDir,

        [Parameter(Mandatory = $true)]
        [object]$UUPState,

        [switch]$IntegrateUpdates
    )

    if ($UUPState -is [System.Collections.IList]) {
        $UUPState = $UUPState | Where-Object { $_ -is [hashtable] } | Select-Object -Last 1
    }
    if ($UUPState -isnot [hashtable]) {
        throw "UUPState must be a hashtable, received: $($UUPState.GetType().FullName)"
    }

    $dismExe = Join-Path $TargetIsoDir "sources\dism.exe"
    if (-not (Test-Path $dismExe)) {
        $dismExe = "dism.exe"
    }

    # 1. Virtual Edition upgrade (e.g., Professional -> Education for INDEX=4)
    if ($UUPState.VirtualEdition) {
        Write-UUPLog "Upgrading mounted UUP base edition to virtual edition: $($UUPState.VirtualEdition)..." -Level Info
        & $dismExe /English /Image:"$ScratchDir" /Set-Edition:$($UUPState.VirtualEdition) | Out-Host
        if ($LASTEXITCODE -ne 0) {
            Write-UUPLog "Warning: DISM /Set-Edition:$($UUPState.VirtualEdition) returned exit code $LASTEXITCODE" -Level Warning
        }
    }

    # 2. Integrate updates (full LCU/SSU/EKB when -IntegrateUpdates is set; lightweight EKB-only otherwise)
    $packages = $UUPState.OSUpdatePackages
    if ($packages -and $packages.Count -gt 0) {
        $scratchTemp = Join-Path $env:TEMP "uup_dism_scratch"
        New-Item -ItemType Directory -Path $scratchTemp -Force | Out-Null

        foreach ($pkg in $packages) {
            $sizeMb = [math]::Round($pkg.Length / 1MB, 1)
            $isSmallCab = ($pkg.Extension.ToLowerInvariant() -eq ".cab" -and $pkg.Length -lt 50MB)

            if (-not $IntegrateUpdates -and -not $isSmallCab) {
                Write-UUPLog "Skipping heavy update package $($pkg.Name) ($sizeMb MB) because -IntegrateUpdates is disabled." -Level Info
                continue
            }

            Write-UUPLog "Integrating UUP update package: $($pkg.Name) ($sizeMb MB)..." -Level Info
            & $dismExe /English /ScratchDir:"$scratchTemp" /Image:"$ScratchDir" /Add-Package /PackagePath:"$($pkg.FullName)" | Out-Host
            if ($LASTEXITCODE -ne 0) {
                Write-UUPLog "Warning: DISM /Add-Package for $($pkg.Name) returned exit code $LASTEXITCODE" -Level Warning
            }
        }

        Remove-Item -Path $scratchTemp -Recurse -Force -ErrorAction SilentlyContinue
    }

    # 3. Refresh detected build number (CurrentBuild.UBR) from offline SOFTWARE hive or UUP metadata
    $softwareHive = Join-Path $ScratchDir "Windows\System32\config\SOFTWARE"
    if (Test-Path $softwareHive) {
        & reg.exe load "HKLM\UUP_VER_CHECK" $softwareHive *> $null
        try {
            $regKey = "HKLM:\UUP_VER_CHECK\Microsoft\Windows NT\CurrentVersion"
            if (Test-Path $regKey) {
                $props = Get-ItemProperty -Path $regKey -ErrorAction SilentlyContinue
                if ($props.CurrentBuild -and $null -ne $props.UBR) {
                    $resolvedVer = "$($props.CurrentBuild).$($props.UBR)"
                    $script:DetectedBuildNumber = $resolvedVer
                    $script:DetectedFullVersion = "10.0.$resolvedVer"
                    Write-UUPLog "Verified post-UUP servicing OS build number: $resolvedVer" -Level Info
                }
            }
        }
        finally {
            [gc]::Collect()
            [gc]::WaitForPendingFinalizers()
            & reg.exe unload "HKLM\UUP_VER_CHECK" *> $null
        }
    }

    if ((-not $script:DetectedBuildNumber -or $script:DetectedBuildNumber -eq "unknown" -or $script:DetectedBuildNumber -match '\.1$') -and $UUPState.BuildNumber) {
        $script:DetectedBuildNumber = $UUPState.BuildNumber
        $script:DetectedFullVersion = "10.0.$($UUPState.BuildNumber)"
        Write-UUPLog "Using UUP metadata build number: $($script:DetectedBuildNumber)" -Level Info
    }
}
