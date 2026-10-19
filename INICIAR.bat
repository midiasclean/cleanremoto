@echo off
rem ================================================================
rem  Clean Remoto - abre o menu (permitir acesso OU acessar outro)
rem ================================================================
setlocal
cd /d "%~dp0"

where py >nul 2>&1 && (set PY=py) || (set PY=python)

%PY% --version >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Python nao encontrado. Instale em https://www.python.org/downloads/
  echo  e marque a opcao "Add python.exe to PATH" durante a instalacao.
  echo.
  pause
  exit /b 1
)

echo Verificando componentes (so na primeira vez)...
%PY% -m pip install -q -r "%~dp0requirements.txt"

%PY% "%~dp0cleanremoto.py"
if errorlevel 1 pause
endlocal
