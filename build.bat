@echo off
chcp 65001 >nul
echo ========================================================
echo       BUILD SCREENCAM LITE - TAO FILE CHAY .EXE
echo ========================================================
echo.

:: 1. Kiem tra Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [LOI] Khong tim thay Python! Vui long cai dat Python 3.10+ va tich vao "Add python.exe to PATH".
    pause
    exit /b 1
)

:: 2. Cai dat cac thu vien can thiet
echo [1/4] Dang cai dat cac thu vien Python...
pip install -r requirements.txt
if errorlevel 1 (
    echo [LOI] Khong the cai dat thu vien Python. Vui long kiem tra ket noi mang.
    pause
    exit /b 1
)

:: 3. Kiem tra ffmpeg.exe trong thu muc bin
echo.
echo [2/4] Kiem tra FFmpeg...
if not exist "bin" mkdir bin
if not exist "bin\ffmpeg.exe" (
    echo Khong tim thay bin\ffmpeg.exe. Dang kiem tra FFmpeg he thong...
    where ffmpeg >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%i in ('where ffmpeg') do (
            echo Tim thay FFmpeg he thong tai: %%i
            copy /y "%%i" "bin\ffmpeg.exe" >nul
            goto ffmpeg_ready
        )
    )
    echo Dang tu dong tai FFmpeg tu GitHub...
    powershell -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Write-Host 'Dang tai FFmpeg...'; (New-Object System.Net.WebClient).DownloadFile('https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip', 'bin\ffmpeg.zip'); Write-Host 'Dang giai nen...'; Expand-Archive -Path 'bin\ffmpeg.zip' -DestinationPath 'bin\temp'; Move-Item 'bin\temp\*\bin\ffmpeg.exe' 'bin\ffmpeg.exe'; Remove-Item -Recurse -Force 'bin\temp', 'bin\ffmpeg.zip'"
)

:ffmpeg_ready
if not exist "bin\ffmpeg.exe" (
    echo [LOI] Khong the tai hoac tim thay bin\ffmpeg.exe!
    echo Vui long tai ffmpeg.exe va copy vao thu muc bin\ truoc khi build.
    pause
    exit /b 1
)
echo FFmpeg da san sang!

:: 4. Build bang PyInstaller
echo.
echo [3/4] Dang dong goi PyInstaller...
pyinstaller ScreenCamLite.spec --noconfirm --clean
if errorlevel 1 (
    echo [LOI] PyInstaller xay ra loi!
    pause
    exit /b 1
)

:: 5. Copy ffmpeg vao dist neu can
if not exist "dist\ScreenCamLite\_internal\bin\ffmpeg.exe" (
    if not exist "dist\ScreenCamLite\_internal\bin" mkdir "dist\ScreenCamLite\_internal\bin"
    copy /y "bin\ffmpeg.exe" "dist\ScreenCamLite\_internal\bin\ffmpeg.exe" >nul
)

echo.
echo ========================================================
echo [THANH CONG] Da tao file chay .exe hoan tat!
echo Duong dan: dist\ScreenCamLite\ScreenCamLite.exe
echo ========================================================
echo.
pause
