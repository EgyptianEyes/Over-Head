"""Offline sustained acquisition checks: scheduler and provider clocks advance together."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor
from overhead import Settings, AircraftFeedUnavailable, AircraftFeedRateLimited


class ContinuousAcquisitionTests(unittest.TestCase):
    def test_ten_minutes_keep_automatic_observations_fresh(self):
        with tempfile.TemporaryDirectory() as directory, patch('monitor.time.monotonic') as clock, \
             patch('monitor._enrichment_slots') as enrichment:
            enrichment.acquire.return_value = False
            clock.return_value = 1000.0
            store = monitor.VisitorSessionStore(Settings(latitude=51.5, longitude=-.1), Path(directory))
            session, _ = store.get_or_create()
            counts = {'normal': 0, 'automatic_tracking': 0}

            def aircraft():
                return {'hex': 'abc123', 'flight': 'TST123', 'lat': 51.51,
                        'lon': -.1, 'seen_pos': .1, 'gs': 250, 'track': 90}

            with patch('monitor.fetch_aircraft', side_effect=lambda *_a, **_k: [aircraft()]), \
                 patch('monitor.fetch_tracked_aircraft', side_effect=lambda *_a, **_k: (aircraft(), 'TST123')):
                for elapsed in range(601):
                    clock.return_value = 1000.0 + elapsed
                    store.touch(session, active=True)
                    for _ in range(3):
                        chosen, _ = store.claim_due()
                        if chosen is None:
                            break
                        kind = chosen.acquisition_kind
                        counts[kind] = counts.get(kind, 0) + 1
                        target = store.automatic_tracking_target(chosen)
                        monitor.update_state(chosen.state, chosen.settings, False, None, None,
                            tracker=chosen.tracker, persist_frame=False,
                            tracking_only=kind.startswith('automatic'),
                            automatic_tracking=target,
                            perform_automatic_tracking=kind == 'automatic_tracking')
                        if kind == 'normal':
                            store.remember_shared_result(chosen)
                        else:
                            store.remember_automatic_tracking_result(chosen)
                    self.assertEqual(session.state.payload['aircraft_id'], 'ABC123', elapsed)
                    self.assertLessEqual(clock.return_value - session.state.position_observed_at_monotonic,
                                         monitor.POSITION_FRESHNESS_SECONDS, elapsed)
                    self.assertTrue(session.state.payload['radar_contacts'], elapsed)
            self.assertEqual(counts['normal'], 11)
            self.assertGreaterEqual(counts['automatic_tracking'], 30)
            self.assertLessEqual(counts['automatic_tracking'], 40)

    def test_lookup_retention_does_not_override_radius_or_expiry(self):
        with tempfile.TemporaryDirectory() as directory, patch('monitor.time.monotonic', return_value=1000):
            store = monitor.VisitorSessionStore(Settings(latitude=51.5, longitude=-.1), Path(directory))
            session, _ = store.get_or_create()
            session.state.payload['aircraft_id'] = 'ABC123'
            session.state.last_live_aircraft = [dict(hex='abc123', lat=51.6, lon=-.1, seen_pos=.1)]
            session.state.last_live_aircraft_received_at = 970
            self.assertIsNotNone(store.automatic_tracking_target(session))
            session.settings.radius_nm = 1
            self.assertIsNone(store.automatic_tracking_target(session))
            session.settings.radius_nm = 25
            session.state.last_live_aircraft_received_at = 939
            self.assertIsNone(store.automatic_tracking_target(session))

    def test_ten_minutes_contention_motion_failures_and_recovery(self):
        with tempfile.TemporaryDirectory() as directory, patch('monitor.time.monotonic') as clock, \
             patch('monitor._enrichment_slots') as enrichment:
            enrichment.acquire.return_value = False
            clock.return_value = 1000.0
            store = monitor.VisitorSessionStore(Settings(latitude=51.5, longitude=-.1), Path(directory))
            first, _ = store.get_or_create()
            second, _ = store.get_or_create()
            second.settings.radius_nm = 5
            elapsed = 0
            clock_offset = 0
            acquisitions = []
            recovered = set()
            failure_modes = set()
            explicit_updates = []

            def records():
                # The nearest plane leaves; another remains inside the larger radius.
                offset = .02 + elapsed / 10000 if elapsed < 90 else .6
                return [dict(hex='abc123', flight='TST123', lat=51.5 + offset, lon=-.1, seen_pos=.1),
                        dict(hex='def456', flight='TST456', lat=51.65, lon=-.1, seen_pos=.1)]

            def provider(*_args, **_kwargs):
                if 150 <= elapsed < 180:
                    raise AircraftFeedUnavailable(10)
                if 210 <= elapsed < 240:
                    raise AircraftFeedRateLimited(20)
                return [] if 240 <= elapsed < 300 else records()

            def targeted(candidates, **_kwargs):
                available = provider()
                key = candidates[0].split(':')[-1].upper()
                plane = next((item for item in available if item['hex'].upper() == key), None)
                return plane, plane['flight'] if plane else ''

            with patch('monitor.fetch_aircraft', side_effect=provider), \
                 patch('monitor.fetch_tracked_aircraft', side_effect=targeted):
                for elapsed in range(601):
                    clock.return_value = 1000.0 + elapsed + clock_offset
                    if elapsed == 120:
                        second.settings.radius_nm = 25
                        second.state.payload['radar_radius_nm'] = 25
                        store.request_refresh(second, 'range')
                    if elapsed == 330:
                        monitor.apply_visitor_location_change(second, 51.55, -.1)
                        store.request_refresh(second, 'location')
                    if elapsed == 360:
                        first.tracker.start('TST123', ('hex:ABC123',))
                        store.request_tracking_refresh(first)
                    if elapsed == 425:
                        first.tracker.stop()
                        store.cancel_refresh(first, 'tracking')
                        store.request_refresh(first, 'cancel')
                    for visitor in (first, second):
                        store.touch(visitor, active=True)
                    for _ in range(6):
                        chosen, _ = store.claim_due()
                        if chosen is None:
                            break
                        kind = chosen.acquisition_kind
                        acquisitions.append((elapsed, chosen.session_id, kind))
                        target = store.automatic_tracking_target(chosen)
                        # A delayed result arrives with actual acquisition time, not queue time.
                        if elapsed == 480 and clock_offset == 0:
                            clock_offset = 4
                            clock.return_value += clock_offset
                        monitor.update_state(chosen.state, chosen.settings, False, None, None,
                            tracker=chosen.tracker, persist_frame=False,
                            cached_acquisition=chosen.cached_acquisition,
                            cached_tracking_result=chosen.cached_tracking_result,
                            tracking_only=kind in {'tracking', 'tracking_cache', 'automatic_tracking', 'automatic_tracking_cache'},
                            perform_tracking_lookup=kind == 'tracking',
                            automatic_tracking=target,
                            cached_automatic_tracking_result=chosen.cached_auto_tracking_result,
                            perform_automatic_tracking=kind == 'automatic_tracking')
                        if kind == 'normal':
                            store.remember_shared_result(chosen)
                        elif kind.startswith('automatic'):
                            store.remember_automatic_tracking_result(chosen)
                        elif kind == 'tracking':
                            explicit_updates.append(elapsed)
                            store.remember_tracking_result(chosen)
                        payload = chosen.state.payload
                        failure_modes.add(payload['mode'])
                        if payload.get('aircraft_id') and not chosen.tracker.snapshot()['active']:
                            self.assertLess(payload['distance_nm'], chosen.settings.radius_nm)
                            self.assertLessEqual(clock.return_value - chosen.state.position_observed_at_monotonic, 20)
                            if elapsed >= 500:
                                recovered.add(chosen.session_id)
                    if elapsed == 75:
                        self.assertEqual(first.state.payload['aircraft_id'], 'ABC123')
                        self.assertEqual(second.state.payload['aircraft_id'], 'ABC123')
                    if elapsed == 390:
                        self.assertTrue(first.tracker.snapshot()['active'])
                        self.assertEqual(first.state.payload['aircraft_id'], 'ABC123')
                        self.assertGreater(first.state.payload['distance_nm'], first.settings.radius_nm)
            self.assertEqual(recovered, {first.session_id, second.session_id})
            self.assertIn('provider_unavailable', failure_modes)
            self.assertIn('rate_limited', failure_modes)
            self.assertIn('scanning', failure_modes)
            self.assertGreaterEqual(len(explicit_updates), 2)
            self.assertTrue(all(b - a >= 30 for a, b in zip(explicit_updates, explicit_updates[1:])))
            self.assertTrue(any(kind.endswith('_cache') for _, _, kind in acquisitions))
            self.assertEqual(first.settings.latitude, 51.5)
            self.assertEqual(second.settings.latitude, 51.55)
