@echo off
rem Builds dist\AutoE.exe - a single file you can share. Needs: py -m pip install mss opencv-python-headless numpy pillow pyinstaller
cd /d "%~dp0"
rem a running AutoE.exe can't be replaced: the build would fail and leave the OLD exe in dist
tasklist /fi "imagename eq AutoE.exe" | findstr /i "AutoE.exe" >nul && (echo. & echo  Close Auto E first ^(also from the tray / Task Manager^), then run build.bat again. & echo. & pause & exit /b 1)
py detector.py || (echo. & echo  Detector self-check failed, exe NOT rebuilt. & pause & exit /b 1)
rem moves the real mouse a few pixels and back: leave the pointer alone for a second
py test_fullauto.py || (echo. & echo  Full-auto self-check failed, exe NOT rebuilt. & pause & exit /b 1)
py -m PyInstaller --noconfirm --onefile --windowed --name AutoE auto_e.py || (echo. & echo  Build FAILED, dist\AutoE.exe is the old version. & pause & exit /b 1)
echo.
echo  Done: dist\AutoE.exe is up to date.
pause
