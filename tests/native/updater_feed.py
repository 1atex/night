import argparse,json,threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from marionette_driver.marionette import Marionette
from smoke import wait_for

class Feed(BaseHTTPRequestHandler):
 bad=False
 def log_message(self,*_):pass
 def do_GET(self):
  version=json.loads((Path(__file__).resolve().parents[2]/'package.json').read_text())['version'];parts=version.split('.');parts[-1]=str(int(parts[-1])+1);next_version='.'.join(parts);tag='v'+next_version+'-preview.1';name=f'night-browser-{next_version}-preview-windows-x64-setup.exe';prefix='https://github.com/1atex/night/releases/download/'+tag+'/'
  body=json.dumps([{'tag_name':tag,'assets':[{'name':name,'browser_download_url':prefix+name,'digest':'sha256:'+('bad' if self.bad else 'a'*64)},{'name':name.replace('-setup.exe','.zip'),'browser_download_url':prefix+name.replace('-setup.exe','.zip'),'digest':'sha256:'+'b'*64}]}]).encode()
  self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)

def run(binary,app_ini,output):
 output.mkdir(parents=True,exist_ok=True);server=ThreadingHTTPServer(('127.0.0.1',0),Feed);threading.Thread(target=server.serve_forever,daemon=True).start();endpoint=f'http://127.0.0.1:{server.server_port}/feed'
 b=Marionette(bin=binary,headless=True,prefs={'night.onboarding.completed':True},app_args=['-app',app_ini],gecko_log=str(output/'updater-feed.log'))
 def chrome(script,*args):b.set_context('chrome');return b.execute_script('const w=Services.wm.getMostRecentWindow("navigator:browser");'+script,script_args=list(args))
 def check():
  b.set_context('chrome');value=b.execute_async_script('const done=arguments[arguments.length-1];ChromeUtils.importESModule("resource:///modules/night/NightUpdates.sys.mjs").NightUpdates.check().then(value=>done({value}),error=>done({error:String(error)}));');assert 'error' not in value,value;return value['value']
 try:
  b.start_session();wait_for(lambda:chrome('return !!w.gNightBrowser;'))
  chrome('w.updateRequests=[];w.updateObserver={endpoint:arguments[0],observe(channel){const request=channel.QueryInterface(Ci.nsIHttpChannel);if(request.URI.spec.startsWith("https://api.github.com/repos/1atex/night/")){w.updateRequests.push(request.URI.spec);request.redirectTo(Services.io.newURI(this.endpoint));}}};Services.obs.addObserver(w.updateObserver,"http-on-modify-request");',endpoint)
  candidate=check();assert candidate and candidate['sha256']=='a'*64 and candidate['url'].startswith('https://github.com/1atex/night/releases/download/'),candidate;assert 'sums' not in candidate
  Feed.bad=True;assert check() is None
  requests=chrome('return w.updateRequests;');assert requests==['https://api.github.com/repos/1atex/night/releases?per_page=20']*2,requests
  result={'passed':True,'new_repository_feed':True,'two_asset_release_supported':True,'github_digest_used':True,'invalid_digest_rejected':True};(output/'updater-feed-result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)
 finally:
  try:chrome('if(w.updateObserver)Services.obs.removeObserver(w.updateObserver,"http-on-modify-request");')
  except Exception:pass
  b.cleanup();b.instance=None;server.shutdown();server.server_close()
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--binary',required=True);p.add_argument('--app-ini',required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();run(a.binary,a.app_ini,a.output)
