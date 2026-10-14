import json,os,pathlib,subprocess,tempfile,signal,time
root=pathlib.Path(__file__).resolve().parent
cfg=json.loads((pathlib.Path.home()/'.config/opencode/opencode.json').read_text())
summary=[]
for short,model,variant in [('deepseek','deepseek-v4.1-flash','low'),('minimax','minimax-m3','thinking'),('kimi','kimi-k3','low')]:
 agent='cheap-coder-'+short
 wd=pathlib.Path(tempfile.mkdtemp(prefix='go-check-'+short+'-'));(wd/'input.json').write_text('{"a":17,"b":23,"operation":"multiply"}\n')
 override={'mcp':{k:{'enabled':False} for k in cfg.get('mcp',{})},'agent':{agent:{'disable':False,'steps':3}},'share':'disabled'}
 env=dict(os.environ,OPENCODE_CONFIG_CONTENT=json.dumps(override))
 prompt='Encargo de validación, sin descendientes. Directorio autorizado: '+str(wd)+'. Sólo lee input.json usando read y calcula la operación. No edites ni ejecutes shell. Responde breve con estado y resultado. Presupuesto: 3 pasos y 120 segundos.'
 cmd=['opencode','run','--dir',str(wd),'--agent',agent,'--model','opencode-go/'+model,'--variant',variant,'--format','json']
 start=time.monotonic();p=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,text=True,start_new_session=True)
 try: out,err=p.communicate(prompt,timeout=120)
 except subprocess.TimeoutExpired:
  os.killpg(p.pid,signal.SIGTERM);out,err=p.communicate(timeout=10)
 (root/(short+'.jsonl')).write_text(out);(root/(short+'.stderr')).write_text(err)
 events=[]
 for line in out.splitlines():
  try:events.append(json.loads(line))
  except ValueError:pass
 sessions=list({e['sessionID'] for e in events if 'sessionID'in e})
 texts=[e.get('part',{}).get('text','') for e in events if e.get('type')=='text']
 errors=[e for e in events if e.get('type')=='error']
 result={'agent':agent,'model':model,'variant':variant,'exit_code':p.returncode,'seconds':round(time.monotonic()-start,1),'session_ids':sessions,'text':texts,'errors':errors,'events':[e.get('type')for e in events]}
 summary.append(result);(root/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False),flush=True)
