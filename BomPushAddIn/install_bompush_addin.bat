@echo off
setlocal

rem install_bompush_addin.bat
rem
rem One-time per-engineer setup for the BOM Push to JobBOSS Inventor
rem add-in. Copies only the small .addin manifest into this machine's
rem local Inventor Addins folder — the manifest's <Assembly> tag points
rem at an absolute UNC path on the shared drive, so the actual DLL is
rem never copied here. Every future rebuild only needs to update the
rem DLL on the share; nothing on this machine ever needs to change
rem again after running this once.
rem
rem Safe to re-run — if the manifest is already current, this just
rem overwrites it with itself.

set "SHARED_ADDIN=\\SYS\sys\BOMIntegration\Releases\BomPushAddIn\BomPushAddIn.addin"
set "LOCAL_ADDINS_DIR=%APPDATA%\Autodesk\Inventor 2026\Addins"
set "OLD_BUNDLE_DIR=%LOCAL_ADDINS_DIR%\BomPushAddIn"

echo BOM Push to JobBOSS - Add-In Installer
echo ========================================
echo.

rem --- Warn if Inventor is currently running -----------------------------
rem A locked .addin/.dll can silently fail to copy/delete rather than
rem erroring clearly, so it's worth catching this up front instead of
rem debugging a confusing partial-install afterward.
tasklist /FI "IMAGENAME eq Inventor.exe" 2>NUL | find /I "Inventor.exe" >NUL
if %ERRORLEVEL% EQU 0 (
    echo Inventor is currently running. Please close it before continuing.
    echo.
    pause
    exit /b 1
)

rem --- Confirm the shared manifest is actually reachable ------------------
if not exist "%SHARED_ADDIN%" (
    echo ERROR: Could not find the shared add-in manifest at:
    echo   %SHARED_ADDIN%
    echo Check that you have access to \\SYS\sys\BOMIntegration and that
    echo a build has actually been deployed there.
    echo.
    pause
    exit /b 1
)

rem --- Remove any old bundle-style install (manifest + DLL together) ------
rem An old install here would carry the same ClassId/ClientId as the new
rem manifest — leaving both registered risks Inventor loading the wrong
rem one, or refusing to load either. Only removes this specific add-in's
rem old folder, nothing else in Addins\.
if exist "%OLD_BUNDLE_DIR%" (
    echo Removing old add-in install: %OLD_BUNDLE_DIR%
    rmdir /S /Q "%OLD_BUNDLE_DIR%"
)

rem --- Copy in the current manifest ----------------------------------------
if not exist "%LOCAL_ADDINS_DIR%" (
    echo ERROR: Inventor Addins folder not found:
    echo   %LOCAL_ADDINS_DIR%
    echo Confirm Inventor 2026 is actually installed on this machine.
    echo.
    pause
    exit /b 1
)

copy /Y "%SHARED_ADDIN%" "%LOCAL_ADDINS_DIR%\BomPushAddIn.addin" >NUL
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to copy the manifest. Check permissions on:
    echo   %LOCAL_ADDINS_DIR%
    echo.
    pause
    exit /b 1
)

echo.
echo Done. BomPushAddIn.addin installed to:
echo   %LOCAL_ADDINS_DIR%
echo.
echo Start Inventor normally - the add-in will load its DLL directly
echo from the shared location. Nothing further to do on this machine.
echo.
pause