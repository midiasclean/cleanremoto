@echo off
rem ================================================================
rem  Acessa OUTRO computador (modo viewer).
rem  Abre a tela para digitar o IP e a senha informados pelo host.
rem ================================================================
setlocal
cd /d "%~dp0"
where py >nul 2>&1 && (set PY=py) || (set PY=python)
%PY% --version >nul 2>&1 || (echo Python nao encontrado - veja o README.md & pause & exit /b 1)
%PY% -m pip install -q -r "%~dp0requirements.txt"
%PY% "%~dp0cleanremoto.py"
if errorlevel 1 pause
endlocal
