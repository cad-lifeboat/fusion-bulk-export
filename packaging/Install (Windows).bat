@echo off
rem Fusion Bulk Export installer for Windows: copies the FusionBulkExport folder that sits
rem next to this file into Fusion's add-ins folder. Nothing else is changed.
setlocal
set "RESULT=1"
set "SOURCE=%~dp0FusionBulkExport"
set "ADDINS=%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns"

if not exist "%SOURCE%\FusionBulkExport.manifest" goto nosource
if "%APPDATA%"=="" goto failed

if not exist "%ADDINS%" mkdir "%ADDINS%"
if not exist "%ADDINS%" goto failed
rem /IS /IT: copy every file, even one robocopy thinks unchanged (the zip gives all files one
rem date, so a manifest of the same size would otherwise keep the old version).
robocopy "%SOURCE%" "%ADDINS%\FusionBulkExport" /MIR /IS /IT /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto failed

echo Installed. In Fusion press Shift+S and find FusionBulkExport in the list: type its name in Search, or check Add-ins under Type on the left. Check its Run on Startup box first, then switch Run on. The window closes when you switch it on; that is normal.
set "RESULT=0"
goto end

:nosource
echo Cannot find the FusionBulkExport folder next to this installer.
echo Unzip the whole download first, then run this installer again from the unzipped folder.
goto end

:failed
echo Copying failed. Close Fusion and run this installer again.

:end
echo.
pause
exit /b %RESULT%
