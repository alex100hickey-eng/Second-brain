import os, json, sys
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
from supabase import create_client
sb = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_KEY'])
WATERMARK = int(sys.argv[1]) if len(sys.argv) > 1 else 358
rows = sb.table('Agent Outputs').select('*').eq('agent_name','system_event').gt('id', WATERMARK).order('id',desc=True).limit(200).execute().data or []
new=[]
for r in rows:
    try: e=json.loads(r['output_text']); e['id']=r['id']; new.append(e)
    except: pass
werr=[e for e in new if e.get('level') in ('error','critical')]
print(f'post-fix system_event rows (id>{WATERMARK}): {len(new)} | error/critical: {len(werr)}')
for e in werr[:15]:
    print(f"  id{e['id']} {e.get('ts','')[:19]} {e.get('component')}: {e.get('message','')[:60]}")
