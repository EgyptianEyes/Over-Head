import sys, json, io, unittest, urllib.error, email.message
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import overhead, monitor
class Resp:
 def __init__(self,data): self.data=data
 def __enter__(self): return self
 def __exit__(self,*args): return False
 def read(self,*args): return json.dumps(self.data).encode()
class Tests(unittest.TestCase):
 def setUp(self):
  overhead._provider_retry_until.clear(); overhead._provider_backoff.clear(); overhead._provider_request_counts.clear();overhead._provider_last_status.clear()
 def test_normal(self):
  urls=[]
  def request(req,timeout=8): urls.append(req.full_url);return Resp({'ac':[{'hex':'a'}]})
  with patch.object(overhead.urllib.request,'urlopen',side_effect=request):
   self.assertEqual(overhead.fetch_aircraft(overhead.Settings(latitude=51.5,longitude=-3.3,radius_nm=25)),[{'hex':'a'}])
  self.assertEqual(len(urls),1);self.assertIn('/v2/point/51.5/-3.3/25',urls[0])
 def test_429_fallback_and_cooldown(self):
  urls=[]; headers=email.message.Message();headers['x-ratelimit-remaining']='0';headers['x-ratelimit-reset']='34300'
  def request(req,timeout=8):
   urls.append(req.full_url)
   if 'adsb.lol' in req.full_url: raise urllib.error.HTTPError(req.full_url,429,'rate limited',headers,None)
   return Resp({'ac':[{'hex':'a'}]})
  with patch.object(overhead.urllib.request,'urlopen',side_effect=request):
   overhead.fetch_aircraft(overhead.Settings())
   overhead.fetch_aircraft(overhead.Settings())
  self.assertEqual(len(urls),3)
  self.assertEqual(overhead._provider_request_counts['ADSB.lol'],1)
  self.assertGreater(overhead.aircraft_provider_status()['ADSB.lol']['retry_in_seconds'],34000)
 def test_both_limited(self):
  def request(req,timeout=8): raise urllib.error.HTTPError(req.full_url,429,'x',{},None)
  with patch.object(overhead.urllib.request,'urlopen',side_effect=request):
   with self.assertRaises(overhead.AircraftFeedRateLimited): overhead.fetch_aircraft(overhead.Settings())
   with self.assertRaises(overhead.AircraftFeedRateLimited): overhead.fetch_aircraft(overhead.Settings())
  self.assertEqual(sum(overhead._provider_request_counts.values()),2)
 def test_recovery(self):
  overhead._provider_retry_until['ADSB.lol']=overhead.time.monotonic()-1
  urls=[]
  def request(req,timeout=8):urls.append(req.full_url);return Resp({'ac':[]})
  with patch.object(overhead.urllib.request,'urlopen',side_effect=request):overhead.fetch_aircraft(overhead.Settings())
  self.assertIn('adsb.lol',urls[0])
 def test_tracking_uses_fallback(self):
  overhead._provider_retry_until['ADSB.lol']=overhead.time.monotonic()+999
  with patch.object(overhead.urllib.request,'urlopen',return_value=Resp({'ac':[{'hex':'406b82','flight':'BAW283','lat':51.5,'lon':-3.3,'seen_pos':0}]})) as call:
   plane,callsign=monitor.fetch_tracked_aircraft(['BAW283'])
   self.assertEqual(callsign,'BAW283');self.assertIn('opendata.adsb.fi',call.call_args[0][0].full_url)
 def test_lease(self):
  lease=monitor.ActiveMonitorLease();self.assertFalse(lease.active());lease.touch();self.assertTrue(lease.active());lease.last_seen-=15;self.assertFalse(lease.active())
if __name__=='__main__':unittest.main(verbosity=2)
