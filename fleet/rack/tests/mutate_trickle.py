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
  'test_trickle.SnapshotTrickleTests.test_incoherent_permit_and_multiple_rows_do_not_publish_history'),
 ('local_dependency',
  'index.html',
  'b.grupo==null || b.grupo===n',
  'b.grupo==null',
  'test_trickle.MergedCellTests.test_adverse_freshness_and_local_dependency'),
 ('future_sensor',
  'index.html',
  'age<0 || age>120',
  'age>120',
  'test_trickle.MergedCellTests.test_adverse_freshness_and_local_dependency'),
 ('incomplete_tick',
  'index.html',
  'else if(!high && !recent) unknown();',
  'else if(false) unknown();',
  'test_trickle.MergedCellTests.test_adverse_freshness_and_local_dependency'),
 ('maintain',
  'snapshot.py',
  "row.get('accion') in ('pausar','mantener','reanudar')",
  "row.get('accion') in ('pausar','reanudar')",
  'test_trickle.SnapshotTrickleTests.test_current_pressure_keep_iso_and_middle_band'),
 ('whitelist',
  'serve.py',
  "        clean['guard'] = _guard_snapshot.validate_guard(data['guard'], allow_extra=True)",
  "        clean['guard'] = data['guard']",
  'test_trickle.SnapshotTrickleTests.test_bool_pressure_is_rejected'),
 ('relaxed_producer',
  'snapshot.py',
  'def validate_guard(value, *, allow_extra=False):',
  'def validate_guard(value, *, allow_extra=True):',
  'test_trickle.SnapshotTrickleTests.test_extended_blocker_makes_producer_tick_incomplete'),
 ('history',
  'snapshot.py',
  'return confirmed\n    finally:db.close()',
  'return None\n    finally:db.close()',
  'test_trickle.SnapshotTrickleTests.test_durable_history_survives_log_rotation'),
 ('merged',
  'index.html',
  "const held=g.status==='Paused' && r.length<g.slots;",
  'const held=false;',
  'test_trickle.MergedCellTests.test_high'),
 ('cpu_attribution',
  'index.html',
  "p.memory<t.pause.memory ? 'memory' : ['io','cpu'].find(k=>k in t.pause && p[k]>t.pause[k])",
  "'cpu'",
  'test_trickle.MergedCellTests.test_io_memory_and_open'),
 ('tren_repo_exacto',
  'tren.py',
  """        name = _repo(row['repo']).lower()
        _require(name in configured and name not in seen, 'repo no permitido o repetido')
        seen.add(name)
        repo = configured[name]""",
  """        repo = _repo(row['repo'])
        _require(repo in repos and repo not in seen, 'repo no permitido o repetido')
        seen.add(repo)""",
  'test_tren.ValidatorTests.test_repo_spelling_is_canonical_and_comment_url_is_preserved')]

def run(root,test='test_trickle'):
    return subprocess.run([sys.executable,'-B','-m','unittest',test],cwd=str(root/'tests'),
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
        control=run(root/'rack',test)
        if control.returncode:raise AssertionError(name+' control: '+control.stderr)
        path.write_text(source.replace(anchor,replacement));result=run(root/'rack',test)
        if (result.returncode!=1 or 'FAIL:' not in result.stderr or
                'AssertionError:' not in result.stderr or 'ERROR:' in result.stderr):
            raise AssertionError(name+': '+result.stderr)
        print(name+': AssertionError; '+test,flush=True)
    print(str(len(CASES))+'/'+str(len(CASES))+' killed by assertion',flush=True)

if __name__=='__main__':main()
