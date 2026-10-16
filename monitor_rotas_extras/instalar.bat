@echo off
rem Instala o monitor: ambiente virtual, dependencias e arquivos de configuracao.
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto sem_python

echo Usando: %PY%
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" || goto python_antigo

if not exist ".venv\Scripts\python.exe" (
  echo Criando ambiente virtual em .venv ...
  %PY% -m venv .venv || goto falhou
)

echo Instalando dependencias. Atras de proxy, defina antes: set HTTPS_PROXY=http://proxy.empresa:porta
".venv\Scripts\python.exe" -m pip install --upgrade pip || goto falhou
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto falhou

if not exist "config.yaml" copy "config.example.yaml" "config.yaml" >nul
if not exist ".env" copy ".env.example" ".env" >nul

echo.
echo Instalacao concluida. Falta:
echo   1. config.yaml: preencher app.url_rotas com a URL da tela ROTA EXTRA
echo   2. .env: preencher ROTAS_TEAMS_WEBHOOK_URL
echo   3. iniciar_monitor.bat --login        (login no AppSheet, uma vez)
echo   4. iniciar_monitor.bat --test-notify  (testa o Teams)
echo   5. iniciar_monitor.bat --once --dry-run
echo   6. iniciar_monitor.bat                (deixa rodando)
pause
exit /b 0

:sem_python
echo Python nao encontrado. Instale pelo python.org com "Install Now" e marque "Add python.exe to PATH".
pause
exit /b 1

:python_antigo
echo E preciso Python 3.10 ou mais novo.
pause
exit /b 1

:falhou
echo Falhou. Veja a mensagem acima. Em rede corporativa costuma ser proxy: veja o README, secao 1.
pause
exit /b 1
