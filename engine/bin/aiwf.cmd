@echo off
rem aiwf - CLI launcher for Windows.
rem Resolves paths relative to this file so the repo can live anywhere,
rem then forwards to the Python module (mirrors the POSIX bin/aiwf).
setlocal
for %%I in ("%~dp0..") do set "ENGINE_DIR=%%~fI"
set "PYTHONPATH=%ENGINE_DIR%;%PYTHONPATH%"
python -m aiwf %*