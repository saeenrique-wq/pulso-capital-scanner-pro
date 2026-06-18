@echo off
title PULSO CAPITAL MX - Scanner PRO
cd /d "C:\Users\saems\scanner"
:LOOP
echo [%date% %time%] Iniciando Scanner PRO...
"C:\Users\saems\AppData\Local\Programs\Python\Python312\python.exe" main.py >> scanner_log.txt 2>&1
echo [%date% %time%] Scanner detenido. Reiniciando en 10 segundos...
timeout /t 10 /nobreak
goto LOOP
