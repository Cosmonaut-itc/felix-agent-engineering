# deskpolicy v1

Módulo stdlib Python 3.9+ para el contrato [desk-politica-v1](../contracts/desk-politica-v1.md).
Admite en WSL bajo el mutex compartido, conserva claims inciertas y asigna
CPUs nocturnas disjuntas. No modifica ni importa hostgate.

El contrato contiene los esquemas, rutas fijas y obligaciones de Jale y Hasito.
Los adaptadores de esos repos se entregan por separado.

## Probar en el desk

La MacBook sólo edita. La suite necesita Linux, `/proc`, `flock` y `taskset`.
Todos los fixtures se crean bajo el cwd del mirror con `testing.Harness`.
La CLI sólo acepta rutas, reloj, boot y carga de prueba bajo `RUN_SUITE_HARNESS`.
El contrato documenta las costuras y las rutas privadas obligatorias.

PAO comprueba carga menor de 12 y 120 s desde el último arranque antes de usar:

```sh
REMOTE_RUN_HOST=desk remote-run start --dir "$PWD" --name desk-politica-u1 \
  --no-git --script fleet/deskpolicy/tests/desk-run.sh
REMOTE_RUN_HOST=desk remote-run wait -q <run-id>
```

`desk-run.sh` vuelve a comprobar los gates y ejecuta la suite bajo afinidad:

```sh
/usr/bin/taskset -c 0-1 /usr/bin/python3 -I fleet/deskpolicy/tests/run.py
```

Después ejecuta `bin/pao check` y ShellCheck sobre los tres guiones si está
disponible. Esas comprobaciones también permanecen bajo CPUs 0 y 1.

El gate usa el mtime de directorios reales de runs como cota conservadora del
último arranque. La creación de `started` cambia ese mtime. Un cierre posterior
puede retrasar la prueba, pero nunca adelantarla. No abre archivos de corridas
ajenas ni sigue enlaces simbólicos de sus directorios.

Las pruebas usan CPUs lógicas 0 a 11 para contabilización y carreras.
Sólo la prueba de afinidad ejecuta carga hija, sobre la primera asignación,
CPU 0. Los siete hijos son intérpretes breves que consultan su propia afinidad.
Ninguna carga de prueba se amplía fuera de las CPUs 0 y 1 del runner.

## Instalar después del APTO

Los guiones se entregan sin ejecutar. No habilitan automáticamente noche.
En el desk, después del drenado y de preparar la raíz privada:

```sh
bash fleet/deskpolicy/deploy-desk.sh --apto <referencia-de-revision>
```

La instalación crea configuración de día y un manifiesto SHA-256.
Acepta repetir una instalación idéntica con la misma referencia.
Rechaza contenido distinto y no inicia un writer. Antes de instalar comprueba
raíz 0700, locks 0600 sobre sus inodes originales y drenado. Los locks
históricos 0644 requieren el chmod explícito del procedimiento de corte.
Un error detectado durante la publicación devuelve los nombres nuevos al staging.
PAO verifica después los consumidores y recursos externos, registra evidencia,
arma el preflight y arranca explícitamente `publicar` en una sesión desacoplada.
Ampliar presupuestos por encima de lo armado o cambiar código o boot exige
un armado nuevo. Reducir presupuestos conserva el armado y frena entradas.

Para retirada conservando cargas y evidencia:

```sh
bash fleet/deskpolicy/withdraw-desk.sh --apto <referencia-de-revision>
```

Persiste noche desactivada y publica día. Los locks, registros y binarios
permanecen hasta el drenado. Si el censo falla, el cambio de configuración ya
impide renovar noche y el arriendo anterior vence en un máximo de 60 s.

## Archivos del módulo

`model.py` valida JSON, relojes y reparto. `store.py` mantiene paths, archivos
atómicos, flocks e identidades Linux. `engine.py` implementa la transacción y
el ciclo de vida. `cli.py` expone operaciones y el wrapper con taskset.
`testing.py` contiene las costuras de prueba. `installation.py` valida el corte
y publica runtime y configuración de día desde staging. Sus pruebas operan
sólo sobre fixtures privados; no ejecutan el guion de despliegue.

El wrapper opcional imprime primero el recibo y luego la salida del guion.
Con `--recibo` guarda el JSON en el run remoto antes de ejecutar carga.
Un guion admitido con salida 75 conserva un recibo admitted.
Una repetición devuelve execute=false. No ejecuta de nuevo.
Una sesión con hijos pendientes impide cerrar y conserva incertidumbre.
El recibo guarda `script_exit_code` por separado y el wrapper devuelve ese
código aunque el cierre no se pueda acreditar. Los recibos admitidos no pierden
su identidad ni se convierten en una denegación.

El wrapper conserva la sesión del runner. Su control usa otro grupo y la carga
permanece en el grupo que mata `remote-run stop`, con TERM o KILL. Después de
recoger al guion cierra con causa `stopped` si ambas sesiones están vacías.
El keeper Jale acredita su handoff con fd 8 y un token por fd heredado.
Puede conservar su propia sesión y su stop por grupo.

Los pesos normativos están en el contrato. Hasito declara 1 a 4, con omisión 4.
Jale declara 4, 2 o 1 según el comando. El canario sigue en 4 corridas y peso 8.
De día no se suman pesos para admitir y cada guion conserva las 12 CPUs.
