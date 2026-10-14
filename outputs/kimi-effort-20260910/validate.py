import http.server,threading,urllib.request,urllib.error,json,os,signal,pathlib,tempfile,subprocess,hashlib,time,datetime
ROOT=pathlib.Path(__file__).resolve().parent
UPSTREAM='https://opencode.ai/zen/go/v1/chat/completions'
records=[]
class Relay(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  if self.path!='/v1/chat/completions':self.send_error(404);return
  body=self.rfile.read(int(self.headers['Content-Length']));data=json.loads(body)
  record={'requested_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'upstream':UPSTREAM,'body_sha256':hashlib.sha256(body).hexdigest(),'request':{k:data[k] for k in ['model','reasoning_effort','thinking','stream','max_tokens','max_completion_tokens'] if k in data}}
  req=urllib.request.Request(UPSTREAM,data=body,headers={'Authorization':self.headers.get('Authorization',''),'Content-Type':'application/json','Accept':'text/event-stream'},method='POST')
  start=time.monotonic()
  try:
   with urllib.request.urlopen(req,timeout=90) as resp:status=resp.status;result=resp.read(4000000);headers=dict(resp.headers)
  except urllib.error.HTTPError as e:status=e.code;result=e.read();headers=dict(e.headers)
  except Exception as e:
   status=502;result=json.dumps({'error':type(e).__name__}).encode();headers={}
  record.update(status=status,seconds=round(time.monotonic()-start,3),response_bytes=len(result),response_sha256=hashlib.sha256(result).hexdigest(),response_headers={k:v for k,v in headers.items() if k.lower() in ['content-type','x-request-id','request-id']})
  chunks=[]
  for line in result.decode(errors='replace').splitlines():
   if line.startswith('data: ') and line!='data: [DONE]':
    try:chunks.append(json.loads(line[6:]))
    except ValueError:pass
  efforts=[]
  def scan(o,path=''):
   if isinstance(o,dict):
    for k,v in o.items():
     if 'effort'in k.lower():efforts.append({'path':path+'.'+k,'value':v})
     scan(v,path+'.'+k)
   elif isinstance(o,list):
    for i,v in enumerate(o):scan(v,path+f'[{i}]')
  for ch in chunks:scan(ch)
  record['response']={'models':sorted({c['model']for c in chunks if c.get('model')}),'ids':sorted({c['id']for c in chunks if c.get('id')}),'usage':[c['usage']for c in chunks if c.get('usage')],'effort_fields':efforts,'reasoning_chars':sum(len(ch.get('delta',{}).get('reasoning_content') or '')for c in chunks for ch in c.get('choices',[])),'finish_reasons':[ch['finish_reason']for c in chunks for ch in c.get('choices',[]) if ch.get('finish_reason')],'top_level_fields':sorted({k for c in chunks for k in c})}
  if status!=200:record['error_body']=result.decode(errors='replace')[:1000]
  records.append(record);(ROOT/'wire-metadata.json').write_text(json.dumps(records,indent=2,ensure_ascii=False))
  self.send_response(status);self.send_header('Content-Type',next((v for k,v in headers.items()if k.lower()=='content-type'),'text/event-stream'));self.send_header('Content-Length',str(len(result)));self.end_headers()
  try:self.wfile.write(result)
  except BrokenPipeError:pass
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Relay);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
cfg=json.loads((pathlib.Path.home()/'.config/opencode/opencode.json').read_text());wd=pathlib.Path(tempfile.mkdtemp(prefix='kimi-effort-')).resolve()
override={'mcp':{k:{'enabled':False}for k in cfg.get('mcp',{})},'share':'disabled','agent':{'cheap-coder-kimi':{'disable':False,'steps':1,'permission':{'*':'deny'}}},'provider':{'opencode-go':{'options':{'baseURL':f'http://127.0.0.1:{server.server_port}/v1'}}}}
env=dict(os.environ,OPENCODE_CONFIG_CONTENT=json.dumps(override))
prompt='Prueba técnica autocontenida. Sin herramientas, archivos ni descendientes. Responde sólo con el resultado y una comprobación de una frase. ¿Cuál es el menor entero positivo n que deja resto 2 al dividirse por 5, resto 3 al dividirse por 7 y resto 4 al dividirse por 9? Presupuesto: una respuesta, 120 segundos.'
summary=[]
try:
 for variant in ['low','high']:
  start=time.monotonic();cmd=['opencode','run','--dir',str(wd),'--agent','cheap-coder-kimi','--model','opencode-go/kimi-k3','--variant',variant,'--format','json']
  p=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,text=True,start_new_session=True)
  try:out,err=p.communicate(prompt,timeout=120)
  except subprocess.TimeoutExpired:
   os.killpg(p.pid,signal.SIGTERM);out,err=p.communicate(timeout=10)
  (ROOT/(variant+'.jsonl')).write_text(out);(ROOT/(variant+'.stderr')).write_text(err)
  events=[]
  for line in out.splitlines():
   try:events.append(json.loads(line))
   except ValueError:pass
  s={'variant':variant,'exit_code':p.returncode,'seconds':round(time.monotonic()-start,2),'session_ids':list({e['sessionID']for e in events if e.get('sessionID')}),'text':[e.get('part',{}).get('text')for e in events if e.get('type')=='text'],'errors':[e for e in events if e.get('type')=='error']}
  summary.append(s);(ROOT/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False));print(json.dumps(s,ensure_ascii=False),flush=True)
  for sid in s['session_ids']:
   result=subprocess.run(['opencode','export',sid],capture_output=True,text=True,timeout=20,check=True);(ROOT/(variant+'-export.json')).write_text(result.stdout)
finally:server.shutdown();server.server_close();thread.join(timeout=5)
print(json.dumps({'wire_requests':len(records),'relay_stopped':not thread.is_alive(),'expected_result':next(n for n in range(1,316)if n%5==2 and n%7==3 and n%9==4)},ensure_ascii=False),flush=True)
