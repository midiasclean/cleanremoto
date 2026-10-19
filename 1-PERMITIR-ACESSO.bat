@echo off
rem ================================================================
rem  Compartilha ESTE computador (modo host).
rem  Mostra uma senha; passe o IP e a senha para quem vai acessar.
rem ================================================================
setlocal
cd /d "%~dp0"
where py >nul 2>&1 && (set PY=py) || (set PY=python)
%PY% --version >nul 2>&1 || (echo Python nao encontrado - veja o README.md & pause & exit /b 1)
%PY% -m pip install -q -r "%~dp0requirements.txt"
%PY% "%~dp0cleanremoto.py" host
if errorlevel 1 pause
endlocal
