@echo off
echo Instalando dependencias...
pip install -r requirements.txt
echo.
echo Iniciando SCANNER PRO en http://localhost:8080
python main.py
pause
