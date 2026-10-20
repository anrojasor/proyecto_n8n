@echo off
REM Arranca n8n con la configuración que necesita el proyecto de deduplicación.

REM n8n 2.x deshabilita Execute Command por defecto; esto lo vuelve a habilitar.
set NODES_EXCLUDE=[]

REM n8n 2.x solo deja leer/escribir archivos en ~/.n8n-files; se amplía a la carpeta del proyecto.
set N8N_RESTRICT_FILE_ACCESS_TO=C:\dedup

REM Zona horaria para el Schedule Trigger y las fechas de los logs.
set GENERIC_TIMEZONE=America/Bogota

REM Windows no maneja permisos de archivo tipo Unix.
set N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS=false

n8n start
