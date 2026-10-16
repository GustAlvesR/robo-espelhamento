@echo off
rem Usado pelo Agendador de Tarefas. Repassa argumentos extras (ex.: --dry-run).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Ambiente virtual nao encontrado. Veja o README, secao Instalacao.
  exit /b 1
)
".venv\Scripts\python.exe" -m rotas_extras %*
