# deskpolicy v1

Módulo stdlib Python 3.9+ para el contrato [desk-politica-v1](../contracts/desk-politica-v1.md).
Admite en WSL bajo el mutex compartido, conserva claims inciertas y asigna
CPUs disjuntas. No modifica ni importa hostgate.

El contrato contiene los esquemas, rutas fijas y obligaciones de Jale y Hasito.
Los adaptadores de esos repos se entregan por separado.

## Probar en el desk

La MacBook sólo edita. La suite necesita Linux, `/proc`, `flock` y `taskset`.
Todos los fixtures se crean bajo el cwd del mirror con `testing.Harness`.
La CLI de producción no acepta rutas ni relojes de prueba.

PAO comprueba carga menor de 12 y 120 s desde el último arranque antes de usar:

```sh
REMOTE_RUN_HOST=desk remote-run start --dir "$PWD" --name desk-politica-u1 \
  --no-git --script fleet/deskpolicy/tests/desk-run.sh
REMOTE_RUN_HOST=desk remote-run wait -q <run-id>
```

`desk-run.sh` vuelve a comprobar los gates y ejecuta:

```sh
/usr/bin/taskset -c 0-1 /usr/bin/python3 -I fleet/deskpolicy/tests/run.py
```

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
No sustituye instalaciones existentes y no inicia un writer.
PAO verifica después los consumidores y recursos externos, registra evidencia,
arma el preflight y arranca explícitamente `publicar` en una sesión desacoplada.
El armado queda invalidado al editar configuración, código o boot.

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
`testing.py` es la única entrada para inyectar datos de prueba.

El wrapper opcional imprime primero el recibo y luego la salida del guion.
Con `--recibo` guarda el JSON en el run remoto antes de ejecutar carga.
Un guion admitido con salida 75 conserva un recibo admitted.
Una repetición devuelve execute=false. No ejecuta de nuevo.
Una sesión con hijos pendientes impide cerrar y conserva incertidumbre.
