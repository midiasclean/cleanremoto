@echo off
rem ================================================================
rem  Gera um unico CleanRemoto.exe (nao precisa Python na outra
rem  maquina). O arquivo fica na pasta "dist".
rem ================================================================
setlocal
cd /d "%~dp0"
where py >nul 2>&1 && (set PY=py) || (set PY=python)
%PY% --version >nul 2>&1 || (echo Python nao encontrado - veja o README.md & pause & exit /b 1)

echo Instalando dependencias e PyInstaller...
%PY% -m pip install -q -r "%~dp0requirements.txt" pyinstaller

echo Gerando o executavel...
%PY% -m PyInstaller --noconfirm --onefile --windowed --name CleanRemoto ^
  --collect-all pynput --collect-all mss "%~dp0cleanremoto.py"

echo.
echo Pronto! O executavel esta em:  %~dp0dist\CleanRemoto.exe
pause
endlocal
