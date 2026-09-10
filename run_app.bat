@echo off
REM Launch the Retrieval Studio (local browser UI).
cd /d "%~dp0"
python -m streamlit run app.py
pause
