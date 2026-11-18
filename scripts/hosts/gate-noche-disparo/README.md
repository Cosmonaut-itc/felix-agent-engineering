# Disparo local del gate nocturno

Un LaunchAgent de la Mini despacha `gate-noche.yml` a la 01:35 local. El cron
de GitHub puede llegar con horas de retraso y el gate sólo corre dentro de su
ventana; el cron de GitHub se conserva como respaldo. Dos disparos en una
noche son inofensivos: el ciclo omite commits que ya tienen veredicto.

`./instalar.sh` instala desde HEAD publicado, verifica SHA-256 y conserva la
procedencia en `~/bin/gate-noche-disparo.origen`. Requiere en la Mini `gh` con
sesión que pueda despachar workflows del repositorio. Antes de instalar
comprueba que esa sesión ve activo el workflow; si no lo ve, no instala nada.
`./instalar.sh --en-seco` hace la misma comprobación con los bytes publicados,
por SSH y sin instalar. El log está en `~/Library/Logs/gate-noche-disparo.log`.

Para desinstalar, ejecuta `launchctl bootout gui/$(id -u)/com.cosmonaut.gate-noche-disparo`
y quita `~/bin/gate-noche-disparo.sh`,
`~/bin/gate-noche-disparo.origen` y
`~/Library/LaunchAgents/com.cosmonaut.gate-noche-disparo.plist`.

Pruebas en host remoto: `python3 -I -m unittest discover -s
scripts/hosts/gate-noche-disparo/tests -v`.
