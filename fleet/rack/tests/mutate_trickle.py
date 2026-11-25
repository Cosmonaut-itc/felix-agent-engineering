"""Directed snapshot/panel mutants; no browser/network/host state."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SOURCE=Path(__file__).resolve().parents[1]
CASES=[('incoherent_history',
  'snapshot.py',
  "            if confirmed is not None:raise ValueError('Confirmación sin intento')",
  '            pass',
  'SnapshotTrickleTests.test_incoherent_permit_and_multiple_rows_do_not_publish_history'),
 ('local_dependency',
  'index.html',
  'b.grupo==null || b.grupo===n',
  'b.grupo==null',
  'MergedCellTests.test_adverse_freshness_and_local_dependency'),
 ('future_sensor',
  'index.html',
  'age<0 || age>120',
  'age>120',
  'MergedCellTests.test_adverse_freshness_and_local_dependency'),
 ('incomplete_tick',
  'index.html',
  'else if(!high && !recent) unknown();',
  'else if(false) unknown();',
  'MergedCellTests.test_adverse_freshness_and_local_dependency'),
 ('maintain',
  'snapshot.py',
  "row.get('accion') in ('pausar','mantener','reanudar')",
  "row.get('accion') in ('pausar','reanudar')",
  'SnapshotTrickleTests.test_current_pressure_keep_iso_and_middle_band'),
 ('whitelist',
  'serve.py',
  "        clean['guard'] = _guard_snapshot.validate_guard(data['guard'], allow_extra=True)",
  "        clean['guard'] = data['guard']",
  'SnapshotTrickleTests.test_bool_pressure_is_rejected'),
 ('relaxed_producer',
  'snapshot.py',
  'def validate_guard(value, *, allow_extra=False):',
  'def validate_guard(value, *, allow_extra=True):',
  'SnapshotTrickleTests.test_extended_blocker_makes_producer_tick_incomplete'),
 ('history',
  'snapshot.py',
  'return confirmed\n    finally:db.close()',
  'return None\n    finally:db.close()',
  'SnapshotTrickleTests.test_durable_history_survives_log_rotation'),
 ('merged',
  'index.html',
  "const held=g.status==='Paused' && r.length<g.slots;",
  'const held=false;',
  'MergedCellTests.test_high'),
 ('cpu_attribution',
  'index.html',
  "p.memory<t.pause.memory ? 'memory' : ['io','cpu'].find(k=>k in t.pause && p[k]>t.pause[k])",
  "'cpu'",
  'MergedCellTests.test_io_memory_and_open')]

def run(root,test=None):
    return subprocess.run([sys.executable,'-B','-m','unittest','test_trickle'+('.'+test if test else '')],cwd=str(root/'tests'),
        env=dict(os.environ,TMPDIR='/tmp',PYTHONDONTWRITEBYTECODE='1'),capture_output=True,text=True,timeout=30)

def main():
    result=run(SOURCE)
    if result.returncode:raise AssertionError('Nominal: '+result.stderr)
    print('Nominal: '+result.stderr.strip(),flush=True)
    for name,file,anchor,replacement,test in CASES:
        root=Path(tempfile.mkdtemp(prefix='sen6417-rack-mutant-',dir='/tmp'))/'fleet'
        shutil.copytree(SOURCE.parent,root,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        path=root/'rack'/file;source=path.read_text()
        if source.count(anchor)!=1:raise AssertionError('Anchor '+name)
        path.write_text(source.replace(anchor,replacement));result=run(root/'rack',test)
        if result.returncode!=1 or 'FAIL:' not in result.stderr or 'ERROR:' in result.stderr:raise AssertionError(name+': '+result.stderr)
        print(name+': AssertionError; '+test,flush=True)
    print(str(len(CASES))+'/'+str(len(CASES))+' killed by assertion',flush=True)

if __name__=='__main__':main()
