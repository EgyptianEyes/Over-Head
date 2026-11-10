import sys, json, io, unittest, urllib.error, email.message
import tempfile
import threading
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
  overhead._provider_request_windows.clear()
  overhead._provider_empty_counts.clear()
  overhead._provider_empty_counts.clear()
 def test_invalid_response_cools_provider(self):
  with patch.object(overhead.urllib.request,'urlopen',side_effect=[Resp({'ac':'invalid'}),Resp({'ac':[]})]) as request:
   self.assertEqual(overhead.fetch_aircraft(overhead.Settings()),[])
   self.assertGreater(overhead._provider_retry_until['ADSB.lol'],overhead.time.monotonic())
   self.assertEqual(request.call_count,2)
 def test_partially_valid_response_drops_invalid_positions(self):
  response=Resp({'ac':[
   {'hex':'406b82','lat':51,'lon':0,'seen_pos':0},
   {'hex':'ffffff','lat':float('nan'),'lon':0,'seen_pos':0},
   {'hex':'abcdef','lat':51,'lon':0,'seen_pos':float('inf')},
  ]})
  with patch.object(overhead.urllib.request,'urlopen',return_value=response) as request:
   records=overhead.fetch_aircraft(overhead.Settings())
  self.assertEqual([record['hex'] for record in records],['406b82'])
  self.assertEqual(request.call_count,1)
 def test_primary_empty_only_verifies_secondary_every_third_response(self):
  with patch.object(overhead.urllib.request,'urlopen',return_value=Resp({'ac':[]})) as request:
   self.assertEqual(overhead.fetch_aircraft(overhead.Settings()),[])
   self.assertEqual(overhead.fetch_aircraft(overhead.Settings()),[])
   self.assertEqual(request.call_count,2)
   self.assertEqual(overhead.fetch_aircraft(overhead.Settings()),[])
   self.assertEqual(request.call_count,4)
 def test_empty_primary_periodically_accepts_secondary_aircraft(self):
  responses=[Resp({'ac':[]}),Resp({'ac':[]}),Resp({'ac':[]}),
             Resp({'ac':[{'hex':'406b82','lat':51,'lon':0,'seen_pos':0}]})]
  with patch.object(overhead.urllib.request,'urlopen',side_effect=responses) as request:
   for _ in range(2):self.assertEqual(overhead.fetch_aircraft(overhead.Settings()),[])
   aircraft=overhead.fetch_aircraft(overhead.Settings())
  self.assertEqual(request.call_count,4)
  self.assertEqual(aircraft[0]['hex'],'406b82')
 def test_conflicting_callsign_falls_back(self):
  first=[{'hex':code,'flight':'BAW283','lat':51,'lon':0,'seen_pos':0} for code in ('406b82','ffffff')]
  with patch.object(overhead.urllib.request,'urlopen',side_effect=[Resp({'ac':first}),Resp({'ac':[first[0]]})]) as request:
   plane,callsign=monitor.fetch_tracked_aircraft(['BAW283'])
   self.assertEqual(plane['hex'],'406b82');self.assertEqual(request.call_count,2)
 def test_http_date_retry_after(self):
  future=email.utils.format_datetime(overhead.datetime.datetime.now(overhead.datetime.timezone.utc)+overhead.datetime.timedelta(seconds=120))
  delay=overhead._retry_after_seconds(future)
  self.assertGreater(delay,118);self.assertLessEqual(delay,120)
 def test_payload_timestamp_exposes_stale_http_200(self):
  record={'hex':'406b82','lat':51,'lon':0,'seen_pos':0}
  with patch.object(overhead.time,'time',return_value=1000),patch.object(overhead.urllib.request,'urlopen',side_effect=[Resp({'now':700,'ac':[record]}),Resp({'now':1000,'ac':[record]})]) as request:
   result=overhead.fetch_provider_aircraft('/v2/hex/406b82')
  self.assertEqual(request.call_count,2);self.assertEqual(result[0]['observed_at'],1000)
 def test_missing_and_stale_target_fall_back(self):
  for primary in ([], [{'hex':'406b82','lat':51,'lon':0,'seen_pos':300}]):
   self.setUp()
   with patch.object(overhead.urllib.request,'urlopen',side_effect=[Resp({'ac':primary}),Resp({'ac':[{'hex':'406b82','lat':51,'lon':0,'seen_pos':1}]})]) as request:
    records=overhead.fetch_provider_aircraft('/v2/hex/406b82')
   self.assertEqual(request.call_count,2)
   self.assertEqual(records[0]['source'],'ADSB.fi')
   self.assertLess(records[0]['seen_pos'],2)
 def test_conflicting_target_not_accepted(self):
  with patch.object(overhead.urllib.request,'urlopen',side_effect=[Resp({'ac':[{'hex':'ffffff','lat':51,'lon':0,'seen_pos':0}]}),Resp({'ac':[]})]):
   self.assertEqual(overhead.fetch_provider_aircraft('/v2/hex/406b82'),[])
 def test_budget_exhaustion(self):
  with patch.object(overhead,'AGGREGATE_REQUESTS_PER_MINUTE',1),patch.object(overhead.urllib.request,'urlopen',return_value=Resp({'ac':[]})) as request:
   overhead.fetch_aircraft(overhead.Settings())
   with self.assertRaises(OSError):overhead.fetch_aircraft(overhead.Settings())
   self.assertEqual(request.call_count,1)
 def test_distinct_tracking_targets_do_not_share_a_global_timer(self):
  with tempfile.TemporaryDirectory() as temporary,patch.object(monitor.time,'monotonic',return_value=1000):
   store=monitor.VisitorSessionStore(overhead.Settings(),Path(temporary))
   for candidate in ('hex:406B82','hex:ABC123'):
    session,_=store.get_or_create();store.touch(session,active=True)
    session.last_fetch_at=1000;session.refresh_reasons.clear()
    session.tracker.start(candidate,(candidate,));store.request_refresh(session,'tracking')
   first,_=store.claim_due();second,_=store.claim_due()
   self.assertIsNotNone(first);self.assertIsNotNone(second);self.assertIsNot(first,second)
   self.assertEqual(store.next_acquisition_at,0)
 def test_normal(self):
  urls=[]
  def request(req,timeout=8): urls.append(req.full_url);return Resp({'ac':[{'hex':'a','lat':51,'lon':0,'seen_pos':0}]})
  with patch.object(overhead.urllib.request,'urlopen',side_effect=request):
     self.assertEqual(overhead.fetch_aircraft(overhead.Settings(latitude=51.5,longitude=-3.3,radius_nm=25))[0]['hex'],'a')
  self.assertEqual(len(urls),1);self.assertIn('/v2/point/51.5/-3.3/25',urls[0])
 def test_429_fallback_and_cooldown(self):
  urls=[]; headers=email.message.Message();headers['x-ratelimit-remaining']='0';headers['x-ratelimit-reset']='34300'
  def request(req,timeout=8):
   urls.append(req.full_url)
   if 'adsb.lol' in req.full_url: raise urllib.error.HTTPError(req.full_url,429,'rate limited',headers,None)
   return Resp({'ac':[{'hex':'a','lat':51,'lon':0,'seen_pos':0}]})
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
class AcquisitionWorkloadTests(unittest.TestCase):
  class EmptyStore:
    def get(self, *args):
      return None

  def setUp(self):
    for state in (overhead._provider_retry_until, overhead._provider_backoff,
            overhead._provider_request_counts, overhead._provider_last_status,
            overhead._provider_request_windows):
      state.clear()

  def test_multi_visitor_volume_latency_and_provider_safe_history(self):
    clock = [1000.0]
    requests = []

    def upstream(request, timeout=8):
      requests.append((clock[0], request.full_url))
      if clock[0] >= 1060 and 'api.adsb.lol' in request.full_url:
        raise urllib.error.HTTPError(request.full_url, 429, 'limited', {'Retry-After': '120'}, None)
      identifier = request.full_url.rsplit('/', 1)[-1].lower()
      cycle = int((clock[0] - 1000) / 30)
      return Resp({'now': clock[0] + 1_700_000_000, 'ac': [{
        'hex': identifier, 'flight': 'TST123', 'r': 'G-TEST',
        'lat': 51.5 + cycle * 0.01, 'lon': -0.1, 'seen_pos': 0,
        'alt_baro': 10000 + cycle * 100, 'gs': 200 + cycle,
        'baro_rate': 200 + cycle * 10, 'track': 90,
      }]})

    with tempfile.TemporaryDirectory() as temporary, patch.object(monitor.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(
      overhead.time, 'time', side_effect=lambda: clock[0] + 1_700_000_000
    ), patch.object(overhead.urllib.request, 'urlopen', side_effect=upstream):
      store = monitor.VisitorSessionStore(overhead.Settings(), Path(temporary))
      visitors = []
      for index in range(12):
        session, _ = store.get_or_create()
        identifier = f'{index % 4 + 1:06X}'
        session.tracker.start(identifier, ('hex:' + identifier,))
        session.last_fetch_at = clock[0]
        visitors.append(session)
      observation_times = {session.session_id: [] for session in visitors}
      for cycle in range(5):
        clock[0] = 1000.0 + cycle * 30
        for session in visitors:
          store.touch(session, active=True)
          session.refresh_reasons.discard('activation')
        for _ in range(len(visitors)):
          selected, _ = store.claim_due()
          self.assertIsNotNone(selected)
          self.assertIn(selected.acquisition_kind, {'tracking', 'tracking_cache'})
          cached = selected.cached_tracking_result
          selected.cached_tracking_result = None
          monitor.update_state(
            selected.state, selected.settings, False, self.EmptyStore(), self.EmptyStore(),
            tracker=selected.tracker, persist_frame=False, tracking_only=True,
            cached_tracking_result=cached, perform_tracking_lookup=cached is None,
          )
          if cached is None:
            store.remember_tracking_result(selected)
          observation_times[selected.session_id].append(selected.state.position_observed_at_monotonic)
        self.assertIsNone(store.claim_due()[0])
      self.assertEqual(len(requests), 21)
      self.assertEqual(overhead._provider_request_counts, {'ADSB.lol': 9, 'ADSB.fi': 12})
      self.assertTrue(all(times == [1000, 1030, 1060, 1090, 1120] for times in observation_times.values()))
      for session in visitors:
        samples = session.state.metric_observations
        self.assertEqual(len(samples), 4)
        self.assertEqual([sample['altitude'] for sample in samples], [10100, 10200, 10300, 10400])
        self.assertEqual([sample['speed'] for sample in samples], [201, 202, 203, 204])
        self.assertEqual([sample['vertical_rate'] for sample in samples], [210, 220, 230, 240])
        self.assertEqual(len({sample['distance_nm'] for sample in samples}), 4)
        self.assertEqual(session.state.payload['observation_source'], 'ADSB.fi')
        self.assertEqual([item['age_seconds'] for item in session.state.payload['metric_history_observations']], [30, 60, 90])
      print('\nWorkload: 12 visitors / 4 aircraft / 5 ticks: 21 upstream requests (20 observations + 1 HTTP 429); simulated observation latency 0s, cadence 30s.')

  def test_shared_missing_and_failed_lookups_are_not_repeated(self):
    for failure in (False, True):
      self.setUp()
      with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary, patch.object(monitor.time, 'monotonic', return_value=1000):
        store = monitor.VisitorSessionStore(overhead.Settings(), Path(temporary))
        for _ in range(4):
          session, _ = store.get_or_create()
          store.touch(session, active=True)
          session.last_fetch_at = 1000
          session.refresh_reasons.clear()
          session.tracker.start('BAW123', ('BAW123',))
        effect = OSError('offline') if failure else None
        with patch.object(monitor, 'fetch_tracked_aircraft', side_effect=effect, return_value=(None, '')) as fetch:
          for _ in range(4):
            selected, _ = store.claim_due()
            self.assertIsNotNone(selected)
            cached = selected.cached_tracking_result
            selected.cached_tracking_result = None
            monitor.update_state(selected.state, selected.settings, False, self.EmptyStore(), self.EmptyStore(),
                       tracker=selected.tracker, persist_frame=False, tracking_only=True,
                       cached_tracking_result=cached, perform_tracking_lookup=cached is None)
            if cached is None:
              store.remember_tracking_result(selected)
          self.assertEqual(fetch.call_count, 1)
          expected = 'provider_unavailable' if failure else 'not_observed'
          self.assertTrue(all(session.state.payload['tracking_acquisition_outcome'] == expected for session in store.sessions.values()))

  def test_ten_visitors_tracking_one_aircraft_share_one_upstream_lookup(self):
    with tempfile.TemporaryDirectory() as temporary, patch.object(monitor.time, 'monotonic', return_value=1000):
      store = monitor.VisitorSessionStore(overhead.Settings(), Path(temporary))
      visitors = []
      for _ in range(10):
        session, _ = store.get_or_create()
        session.tracker.start('406B82', ('hex:406B82',))
        session.last_fetch_at = 1000
        store.touch(session, active=True)
        session.refresh_reasons.clear()
        visitors.append(session)
      plane = {'hex': '406b82', 'flight': 'BAW5', 'lat': 51, 'lon': 0, 'seen_pos': 0, 'alt_baro': 30000}
      with patch.object(monitor, 'fetch_tracked_aircraft', return_value=(plane, 'BAW5')) as fetch:
        for _ in visitors:
          selected, _ = store.claim_due()
          self.assertIsNotNone(selected)
          cached = selected.cached_tracking_result
          selected.cached_tracking_result = None
          monitor.update_state(selected.state, selected.settings, False, self.EmptyStore(), self.EmptyStore(),
                               tracker=selected.tracker, tracking_only=True, persist_frame=False,
                               cached_tracking_result=cached, perform_tracking_lookup=cached is None)
          if cached is None:
            store.remember_tracking_result(selected)
      self.assertEqual(fetch.call_count, 1)
      self.assertEqual(sum(bool(session.state.payload['tracking_found']) for session in visitors), 10)

  def test_inactive_and_expired_sessions_do_not_acquire(self):
    with tempfile.TemporaryDirectory() as temporary, patch.object(monitor.time, 'monotonic', return_value=1000):
      store = monitor.VisitorSessionStore(overhead.Settings(), Path(temporary), session_ttl_seconds=60)
      session, _ = store.get_or_create()
      session.tracker.start('BAW123', ('BAW123',))
      self.assertIsNone(store.claim_due()[0])
      store.touch(session, active=True)
      session.lease.last_seen = 980
      self.assertIsNone(store.claim_due()[0])
      self.assertIsNone(store.claim_due(now=1061)[0])
      self.assertEqual(store.sessions, {})

  def test_budget_overload_is_bounded_and_does_not_starve_targets(self):
    clock = [1000.0]
    observed = set()

    def upstream(request, timeout=8):
      identifier = request.full_url.rsplit('/', 1)[-1].lower()
      observed.add(identifier)
      return Resp({'ac': [{'hex': identifier, 'lat': 51, 'lon': 0, 'seen_pos': 0}]})

    with tempfile.TemporaryDirectory() as temporary, patch.object(monitor.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(
      overhead.urllib.request, 'urlopen', side_effect=upstream
    ) as request:
      store = monitor.VisitorSessionStore(overhead.Settings(), Path(temporary), max_sessions=64)
      for index in range(32):
        session, _ = store.get_or_create()
        identifier = f'{index + 1:06X}'
        session.tracker.start(identifier, ('hex:' + identifier,))
        session.last_fetch_at = clock[0]
      attempts = []
      for cycle in range(3):
        clock[0] = 1000 + cycle * 30
        for session in store.sessions.values():
          store.touch(session, active=True)
          session.refresh_reasons.discard('activation')
        for _ in range(32):
          selected, _ = store.claim_due()
          self.assertIsNotNone(selected)
          monitor.update_state(selected.state, selected.settings, False, self.EmptyStore(), self.EmptyStore(),
                     tracker=selected.tracker, persist_frame=False, tracking_only=True)
          store.remember_tracking_result(selected)
        attempts.append(request.call_count)
      self.assertEqual(attempts, [30, 30, 60])
      self.assertEqual(len(observed), 32)
      print('\nOverload: 32 unique targets: cumulative upstream attempts at 0/30/60s = 30/30/60; every target served by 60s, budget never bypassed.')

  def test_slow_shared_response_does_not_turn_thirty_seconds_into_sixty(self):
    clock = [1000.0]
    with tempfile.TemporaryDirectory() as temporary, patch.object(monitor.time, 'monotonic', side_effect=lambda: clock[0]):
      store = monitor.VisitorSessionStore(overhead.Settings(), Path(temporary))
      source, _ = store.get_or_create()
      source.tracker.start('BAW123', ('BAW123',))
      source.last_fetch_at = 1000
      store.touch(source, active=True)
      source.refresh_reasons.clear()
      selected, _ = store.claim_due()
      plane = {'hex': '406b82', 'flight': 'BAW123', 'lat': 51, 'lon': 0, 'seen_pos': 0}

      def slow_lookup(candidates):
        clock[0] = 1008
        return plane, 'BAW123'

      with patch.object(monitor, 'fetch_tracked_aircraft', side_effect=slow_lookup):
        monitor.update_state(source.state, source.settings, False, self.EmptyStore(), self.EmptyStore(),
                   tracker=source.tracker, tracking_only=True, persist_frame=False)
      store.remember_tracking_result(source)
      clock[0] = 1025
      visitor, _ = store.get_or_create()
      visitor.tracker.start('BAW123', ('BAW123',))
      visitor.last_fetch_at = clock[0]
      store.touch(visitor, active=True)
      visitor.refresh_reasons.clear()
      selected, _ = store.claim_due()
      self.assertIs(selected, visitor)
      self.assertEqual(selected.acquisition_kind, 'tracking_cache')
      self.assertEqual(selected.last_tracking_at, 1000)
      cached = visitor.cached_tracking_result
      monitor.update_state(visitor.state, visitor.settings, False, self.EmptyStore(), self.EmptyStore(),
                 tracker=visitor.tracker, tracking_only=True, persist_frame=False,
                 cached_tracking_result=cached, perform_tracking_lookup=False)
      self.assertEqual(visitor.state.payload['telemetry']['seen_pos'], 17)
      clock[0] = 1030
      store.touch(source, active=True)
      source.refresh_reasons.clear()
      selected, _ = store.claim_due()
      self.assertIsNotNone(selected)
      self.assertEqual(selected.acquisition_kind, 'tracking')


  def test_stale_responses_are_not_reported_as_aircraft_absence(self):
    stale = {'hex': '406b82', 'flight': 'BAW123', 'lat': 51, 'lon': 0, 'seen_pos': 300}
    tracker = monitor.FlightTrackingState()
    tracker.start('BAW123', ('BAW123',))
    state = monitor.FrameState()
    with patch.object(overhead.urllib.request, 'urlopen', return_value=Resp({'ac': [stale]})) as request:
      monitor.update_state(state, overhead.Settings(), False, self.EmptyStore(), self.EmptyStore(),
                 tracker=tracker, tracking_only=True, persist_frame=False)
    self.assertEqual(request.call_count, 2)
    self.assertEqual(state.payload['tracking_acquisition_outcome'], 'stale')
    self.assertEqual(state.payload['mode'], 'position_unavailable')
    self.assertEqual(state.metric_observations, [])

  def test_repeated_observation_and_older_provider_do_not_add_history(self):
    state = monitor.FrameState()
    tracker = monitor.FlightTrackingState()
    tracker.start('BAW123', ('hex:406B82',))
    plane = {'hex': '406b82', 'flight': 'BAW123', 'lat': 51, 'lon': 0, 'seen_pos': 0,
         'alt_baro': 10000, 'observed_at': 2000, 'gs': 200, 'source': 'ADSB.lol'}
    for now, record in ((1000, plane), (1030, dict(plane, seen_pos=30)),
              (1035, dict(plane, seen_pos=40, observed_at=1995, source='ADSB.fi', alt_baro=5000))):
      with patch.object(monitor.time, 'monotonic', return_value=now), patch.object(monitor.time, 'time', return_value=now + 1000), patch.object(
        monitor, 'fetch_tracked_aircraft', return_value=(record, 'BAW123')
      ):
        monitor.update_state(state, overhead.Settings(), False, self.EmptyStore(), self.EmptyStore(),
                   tracker=tracker, tracking_only=True, persist_frame=False)
    self.assertEqual(len(state.metric_observations), 1)
    self.assertEqual(state.payload['altitude'], 10000)
    self.assertTrue(all(sample is None for sample in state.payload['metric_history'].values()))

  def test_same_target_cannot_be_claimed_twice_while_pending(self):
    with tempfile.TemporaryDirectory() as temporary, patch.object(monitor.time, 'monotonic', return_value=1000):
      store = monitor.VisitorSessionStore(overhead.Settings(), Path(temporary))
      for _ in range(2):
        session, _ = store.get_or_create()
        session.tracker.start('BAW123', ('BAW123',))
        store.touch(session, active=True)
        session.last_fetch_at = 1000
        session.refresh_reasons.clear()
      self.assertIsNotNone(store.claim_due()[0])
      self.assertIsNone(store.claim_due()[0])

  def test_geographic_stale_response_is_not_an_empty_success(self):
    record = {'hex': '406b82', 'lat': 51, 'lon': 0, 'seen_pos': 300}
    state = monitor.FrameState()
    with patch.object(overhead.urllib.request, 'urlopen', return_value=Resp({'ac': [record]})) as request:
      monitor.update_state(state, overhead.Settings(), False, self.EmptyStore(), self.EmptyStore(), persist_frame=False)
    self.assertEqual(request.call_count, 2)
    self.assertEqual(state.payload['acquisition_outcome'], 'stale')
    self.assertIsNone(state.last_acquisition_succeeded_at)
    self.assertEqual(state.metric_observations, [])

  def test_slow_route_enrichment_does_not_block_another_tracking_acquisition(self):
    route_started = threading.Event()
    release_route = threading.Event()
    calls = []

    class SlowRoutes:
      api_key = ""

      def get(self, plane):
        route_started.set()
        release_route.wait(2)
        return {}

    def lookup(candidates):
      hex_code = candidates[0].split(":", 1)[-1]
      calls.append(hex_code)
      return ({"hex": hex_code.lower(), "flight": "TST" + hex_code[-2:],
           "lat": 51, "lon": 0, "seen_pos": 0}, "TST" + hex_code[-2:])

    first_state, second_state = monitor.FrameState(), monitor.FrameState()
    first_tracker, second_tracker = monitor.FlightTrackingState(), monitor.FlightTrackingState()
    first_tracker.start("hex:000001", ("hex:000001",))
    second_tracker.start("hex:000002", ("hex:000002",))
    with patch.object(monitor, "fetch_tracked_aircraft", side_effect=lookup):
      try:
        monitor.update_state(first_state, overhead.Settings(), False, self.EmptyStore(), self.EmptyStore(),
                   routes=SlowRoutes(), tracker=first_tracker, tracking_only=True, persist_frame=False)
        self.assertTrue(route_started.wait(1))
        monitor.update_state(second_state, overhead.Settings(), False, self.EmptyStore(), self.EmptyStore(),
                   routes=SlowRoutes(), tracker=second_tracker, tracking_only=True, persist_frame=False)
        self.assertEqual(calls, ["000001", "000002"])
        self.assertEqual(second_state.payload["aircraft_id"], "000002")
      finally:
        release_route.set()

  def test_slow_enrichment_cannot_overwrite_a_newer_same_aircraft_position(self):
    route_started = threading.Event()
    release_first = threading.Event()
    second_route_done = threading.Event()

    class OrderedRoutes:
      api_key = ""
      count = 0

      def get(self, plane):
        self.count += 1
        if self.count == 1:
          route_started.set()
          release_first.wait(2)
        else:
          second_route_done.set()
        return {}

    records = [
      {"hex": "406b82", "flight": "BAW5", "lat": 51, "lon": 0, "seen_pos": 0, "alt_baro": 10000},
      {"hex": "406b82", "flight": "BAW5", "lat": 52, "lon": 0, "seen_pos": 0, "alt_baro": 20000},
    ]
    state = monitor.FrameState()
    tracker = monitor.FlightTrackingState()
    tracker.start("406B82", ("hex:406B82",))
    routes = OrderedRoutes()
    with patch.object(monitor, "fetch_tracked_aircraft", side_effect=[(records[0], "BAW5"), (records[1], "BAW5")]):
      monitor.update_state(state, overhead.Settings(), False, self.EmptyStore(), self.EmptyStore(),
                           routes=routes, tracker=tracker, tracking_only=True, persist_frame=False)
      self.assertTrue(route_started.wait(1))
      with state.lock:
        first_future = state.last_enrichment_future
      monitor.update_state(state, overhead.Settings(), False, self.EmptyStore(), self.EmptyStore(),
                           routes=routes, tracker=tracker, tracking_only=True, persist_frame=False)
      self.assertTrue(second_route_done.wait(1))
      with state.lock:
        second_future = state.last_enrichment_future
      self.assertIsNot(first_future, second_future)
      second_future.result(timeout=2)
      self.assertEqual(state.payload["aircraft_latitude"], 52)
      release_first.set()
      first_future.result(timeout=2)
      self.assertEqual(state.payload["aircraft_latitude"], 52)
      self.assertEqual(state.payload["altitude"], 20000)
    release_first.set()

  def test_sixty_four_position_updates_never_exceed_four_enrichment_workers(self):
    release_routes = threading.Event()
    workers_started = threading.Event()
    counter_lock = threading.Lock()
    active = 0
    maximum_active = 0
    completed_routes = 0

    class BlockingRoutes:
      api_key = ""

      def get(self, plane):
        nonlocal active, maximum_active, completed_routes
        with counter_lock:
          active += 1
          maximum_active = max(maximum_active, active)
          if active == monitor.MAX_ENRICHMENT_WORKERS:
            workers_started.set()
        release_routes.wait(2)
        with counter_lock:
          active -= 1
          completed_routes += 1
        return {}

    states = []
    trackers = []

    def lookup(candidates):
      identifier = candidates[0].split(":", 1)[-1]
      return ({"hex": identifier.lower(), "flight": "TST" + identifier[-2:],
               "lat": 51, "lon": 0, "seen_pos": 0, "alt_baro": 10000}, "TST" + identifier[-2:])

    with patch.object(monitor, "fetch_tracked_aircraft", side_effect=lookup):
      for index in range(64):
        identifier = f"{index + 1:06X}"
        state = monitor.FrameState()
        tracker = monitor.FlightTrackingState()
        tracker.start(identifier, ("hex:" + identifier,))
        monitor.update_state(state, overhead.Settings(), False, self.EmptyStore(), self.EmptyStore(),
                             routes=BlockingRoutes(), tracker=tracker, tracking_only=True, persist_frame=False)
        states.append(state)
        trackers.append(tracker)
      try:
        self.assertTrue(workers_started.wait(1), "Bounded enrichment workers did not start")
        self.assertEqual(maximum_active, monitor.MAX_ENRICHMENT_WORKERS)
        self.assertTrue(all(state.payload["aircraft_id"] == tracker.snapshot()["last_plane"]["hex"].upper()
                            for state, tracker in zip(states, trackers)))
      finally:
        release_routes.set()
      futures = [state.last_enrichment_future for state in states if state.last_enrichment_future is not None]
      for future in futures:
        future.result(timeout=3)
    self.assertEqual(completed_routes, 64)
    self.assertEqual(maximum_active, 4)

  def test_status_does_not_wait_for_an_inflight_provider(self):
    request_started = threading.Event()
    release_request = threading.Event()
    status_ready = threading.Event()

    def upstream(request, timeout=8):
      request_started.set()
      release_request.wait(2)
      return Resp({'ac': []})

    def status():
      overhead.aircraft_provider_status()
      status_ready.set()

    with patch.object(overhead.urllib.request, 'urlopen', side_effect=upstream):
      worker = threading.Thread(target=overhead.fetch_aircraft, args=(overhead.Settings(),))
      worker.start()
      self.assertTrue(request_started.wait(1))
      reader = threading.Thread(target=status)
      reader.start()
      try:
        self.assertTrue(status_ready.wait(0.5), 'Local diagnostics blocked on upstream network I/O')
      finally:
        release_request.set()
        worker.join(2)
        reader.join(2)


if __name__ == '__main__':
  unittest.main(verbosity=2)
