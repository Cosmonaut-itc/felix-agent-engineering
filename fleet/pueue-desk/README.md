# Pueue en el desk

Servicio systemd **de usuario** para Pueue 4.0.4 en WSL, listo para instalar
tras U10 (NVMe). No instala binarios. El [retiro de deskpolicy](../deskpolicy/)
se despliega por separado durante el corte coordinado de los clientes a Pueue.

Desde la MacBook: `fleet/pueue-desk/deploy-desk.sh --seco` comprueba sin escribir;
`fleet/pueue-desk/deploy-desk.sh` instala por `ssh desk`. Requiere `pueue` y
`pueued` en `/home/felix/.local/bin`, systemd de usuario y permiso para
`loginctl enable-linger felix`. El instalador lo pide antes de detener un
`pueued` manual: si se rechaza, termina con error y el daemon manual sigue vivo.
Un fallo de `aplicar` no reinicia `pueued`: `pueue-desk-mode.service` reintenta
cada 30 s y deja el error en `journalctl --user -u pueue-desk-mode`.
Linger inicia el servicio al arrancar la distro; el arranque de Windows debe
iniciar WSL por su mecanismo habitual (SSH despierta la distro).

`control.py` contiene la única tabla de cuotas: hasito 3/4, jale 2/3,
otros 1/1 y pg5434 1/1 (día/noche). Noche: [05:00Z, 14:00Z).
La suma es 7/9 incluyendo pg5434; Pueue no impone un máximo global. Los timers
y el arranque aplican el modo UTC vigente, sin interrumpir tareas vivas.
Usar sólo esos grupos; `default` y otros grupos quedan fuera de estas cuotas.
El instalador elimina `suites` sólo si no tiene ninguna tarea.

Ver fila: `ssh desk '~/.local/bin/pueue status -g hasito'`.
Pausar a mano, dejando terminar tareas:
`ssh desk '/usr/bin/python3 -I ~/.local/lib/pao-pueue-desk/control.py pausar hasito'`.
Reanudar: `ssh desk '~/.local/bin/pueue start -g hasito'`.
El guardia respeta grupos previamente pausados. **Si el guardia ya había
pausado el grupo, usar siempre el comando `pausar` anterior**: Pueue no distingue
una segunda pausa manual de la suya. Ese comando retira la propiedad del guardia
bajo el mismo candado; una pausa directa de Pueue no puede expresar ese relevo.

Cada 60 s el guardia lee PSI avg60 y MemAvailable. Pausa con E/S >40,
CPU >30 o RAM <4 GiB; reanuda sólo sus grupos con E/S <20, CPU <15 y RAM ≥6 GiB.
Los bordes intermedios mantienen el estado. Registro JSONL UTC, con lecturas,
decisiones y acciones: `ssh desk 'tail -f ~/.local/state/pao/pueue-desk/guardia.log'`.
Rota a `.1` al llegar a 1 MiB. Estado y candado comparten ese directorio.
Ante errores de lectura o de Pueue falla sin reanudar; consultar también
`journalctl --user -u pueue-desk-guard.service`.

Retirar: `fleet/pueue-desk/withdraw-desk.sh` (admite `--seco`). Detiene y
deshabilita timers y daemon y quita unidades; conserva cola, estado, biblioteca
y linger. No drenar tareas automáticamente: retirar cuando hayan terminado.

Pruebas: `/usr/bin/python3 -I -m unittest discover -s fleet/pueue-desk/tests -v`.
ShellCheck: `shellcheck --severity=warning fleet/pueue-desk/*-desk.sh`.
