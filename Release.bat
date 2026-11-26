@echo off
cd /d "%~dp0"
rem One click: build the installer, make the GitHub release (installer attached), then publish catalog.json.
rem Needs GitHub CLI once:  winget install GitHub.cli   then   gh auth login
where gh >nul 2>nul
if errorlevel 1 goto nogh
gh auth status >nul 2>nul
if errorlevel 1 goto noauth
for /f "tokens=3 delims= " %%V in ('findstr /b /c:"VERSION = " papro.py') do set "VER=%%~V"
echo Releasing PAPro %VER% ...
set "PAPRO_RELEASE=1"
call "Build EXE.bat"
if errorlevel 1 goto failed
if not exist "dist\PAPro-Setup-%VER%.exe" goto failed
set "NOTES=--generate-notes"
if exist "RELEASE-NOTES-%VER%.md" set "NOTES=--notes-file RELEASE-NOTES-%VER%.md"
echo.
echo Creating the GitHub release v%VER% ...
gh release create v%VER% "dist\PAPro-Setup-%VER%.exe" --repo sjames1066/papro --target main --title "PAPro %VER%" %NOTES%
if errorlevel 1 goto failed
echo.
echo Publishing catalog.json ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0release_catalog.ps1" -Version %VER%
if errorlevel 1 goto failed
echo.
echo Done: PAPro %VER% is released and announced.
pause
exit /b 0

:nogh
echo GitHub CLI is not installed. Run:  winget install GitHub.cli
echo then:  gh auth login   and run Release.bat again.
pause
exit /b 1

:noauth
echo Not signed in to GitHub. Run:  gh auth login   then run Release.bat again.
pause
exit /b 1

:failed
echo.
echo Release stopped - see the message above. Nothing after the failed step was done.
pause
exit /b 1
