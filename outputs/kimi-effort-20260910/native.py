import json,os,signal,pathlib,tempfile,subprocess,time,shutil
ROOT=pathlib.Path(__file__).resolve().parent
cfg=json.loads((pathlib.Path.home()/'.config/opencode/opencode.json').read_text());wd=pathlib.Path(tempfile.mkdtemp(prefix='kimi-native-')).resolve();plugins=wd/'.opencode/plugins';plugins.mkdir(parents=True);shutil.copy2(ROOT/'capture.js',plugins/'capture.js')
override={'mcp':{k:{'enabled':False}for k in cfg.get('mcp',{})},'share':'disabled','agent':{'cheap-coder-kimi':{'disable':False,'steps':1,'permission':{'*':'deny'}}}}
base_env=dict(os.environ,OPENCODE_CONFIG_CONTENT=json.dumps(override))
prompt='Prueba técnica autocontenida. Sin herramientas, archivos ni descendientes. Responde sólo con el resultado y una comprobación de una frase. ¿Cuál es el menor entero positivo n que deja resto 2 al dividirse por 5, resto 3 al dividirse por 7 y resto 4 al dividirse por 9? Presupuesto: una respuesta, 120 segundos.'
summary=[]
for variant in ['low','high']:
 env=dict(base_env,GO_EFFORT_METADATA=str(ROOT/('native-'+variant+'-wire.jsonl')))
 cmd=['opencode','run','--dir',str(wd),'--title','Kimi effort validation '+variant,'--agent','cheap-coder-kimi','--model','opencode-go/kimi-k3','--variant',variant,'--format','json']
 start=time.monotonic();p=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,text=True,start_new_session=True)
 try:out,err=p.communicate(prompt,timeout=120)
 except subprocess.TimeoutExpired:
  os.killpg(p.pid,signal.SIGTERM);out,err=p.communicate(timeout=10)
 (ROOT/('native-'+variant+'.jsonl')).write_text(out);(ROOT/('native-'+variant+'.stderr')).write_text(err)
 events=[]
 for line in out.splitlines():
  try:events.append(json.loads(line))
  except ValueError:pass
 s={'variant':variant,'exit_code':p.returncode,'seconds':round(time.monotonic()-start,2),'session_ids':list({e['sessionID']for e in events if e.get('sessionID')}),'text':[e.get('part',{}).get('text')for e in events if e.get('type')=='text'],'errors':[{'name':e.get('error',{}).get('name'),'message':e.get('error',{}).get('data',{}).get('message')}for e in events if e.get('type')=='error']}
 summary.append(s);(ROOT/'native-summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False));print(json.dumps(s,ensure_ascii=False),flush=True)
 for sid in s['session_ids']:
  result=subprocess.run(['opencode','export',sid],capture_output=True,text=True,timeout=20,check=True);(ROOT/('native-'+variant+'-export.json')).write_text(result.stdout)
shutil.rmtree(plugins)
print('Temporary diagnostic plugin removed; all CLI processes terminated.',flush=True)
