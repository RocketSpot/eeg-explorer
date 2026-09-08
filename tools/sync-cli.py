"""Manual retry/restore/status for authorized private repository synchronization."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from explorer.store import Store
from explorer.server import default_root
from explorer.sync import SyncManager
p=argparse.ArgumentParser();p.add_argument('--data-dir',default=str(default_root()));p.add_argument('command',choices=['status','retry','restore']);p.add_argument('--session-id');p.add_argument('--revision',type=int);a=p.parse_args()
s=Store(a.data_dir,recover=False);sync=SyncManager(s,start_worker=False)
try:
 if a.command=='status':print(json.dumps(sync.status(),indent=2))
 elif a.command=='retry':sync.retry();print('Queued failed work. Open EEG Explorer to resume its background worker.')
 else:
  if not a.session_id or a.revision is None:p.error('restore requires --session-id and --revision')
  print(json.dumps(sync.restore(a.session_id,a.revision),indent=2))
finally:sync.close()
