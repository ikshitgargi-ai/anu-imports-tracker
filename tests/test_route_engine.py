"""Route engine: the math a rep literally drives. If this is wrong, someone
wastes a tank of fuel, so the guarantees are tested against known geometry."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import route_engine as re  # noqa: E402

# Real-ish Ontario points
TORONTO = (43.6532, -79.3832)
MISSISSAUGA = (43.5890, -79.6441)
BRAMPTON = (43.7315, -79.7624)
OTTAWA = (45.4215, -75.6972)
LONDON = (42.9849, -81.2453)


class TestDistance:
    def test_haversine_known_distance(self):
        # Toronto to Ottawa is about 350 km straight line
        d = re.haversine_km(TORONTO, OTTAWA)
        assert 340 < d < 360, d

    def test_zero_distance(self):
        assert re.haversine_km(TORONTO, TORONTO) < 0.001


class TestCorridor:
    def test_point_on_the_line_has_near_zero_detour(self):
        mid = ((TORONTO[0] + OTTAWA[0]) / 2, (TORONTO[1] + OTTAWA[1]) / 2)
        assert re.point_to_segment_km(mid, TORONTO, OTTAWA) < 5

    def test_point_off_the_line_has_a_real_detour(self):
        # London is nowhere near the Toronto->Ottawa line
        assert re.point_to_segment_km(LONDON, TORONTO, OTTAWA) > 100

    def test_corridor_keeps_on_the_way_and_destination_stores(self):
        stores = [
            {'store_number': 1, 'lat': MISSISSAUGA[0], 'lng': MISSISSAUGA[1]},  # near origin corridor
            {'store_number': 2, 'lat': 44.30, 'lng': -78.30},                    # roughly on the way
            {'store_number': 3, 'lat': OTTAWA[0], 'lng': OTTAWA[1]},             # destination
            {'store_number': 4, 'lat': LONDON[0], 'lng': LONDON[1]},             # wrong direction
        ]
        kept = re.corridor_select(stores, TORONTO, OTTAWA,
                                  corridor_km=25, dest_radius_km=30)
        nums = {s['store_number'] for s in kept}
        assert 3 in nums, 'the destination must be included'
        assert 4 not in nums, 'a store in the wrong direction must be excluded'
        assert all('why' in s and 'detour_km' in s for s in kept)


class TestOptimize:
    def test_round_trip_starts_and_returns_to_origin(self):
        pts = [TORONTO, OTTAWA, MISSISSAUGA, BRAMPTON]
        order, mat, total = re.optimize(pts, re.haversine_km, round_trip=True)
        assert order[0] == 0
        assert order[-1] == len(pts)  # the appended origin copy
        assert total > 0

    def test_two_opt_never_makes_it_worse(self):
        pts = [TORONTO, OTTAWA, MISSISSAUGA, BRAMPTON, LONDON, (44.0, -79.0)]
        mat = re._matrix(pts + [pts[0]], re.haversine_km)
        naive = re.nearest_neighbor(mat, 0, must_end=len(pts))
        opt = re.two_opt(naive, mat, fixed_start=True, fixed_end=True)
        assert re._route_len(opt, mat) <= re._route_len(naive, mat) + 1e-9

    def test_a_known_optimal_square_is_found(self):
        # Four corners of a square: the optimal loop is the perimeter, not the
        # diagonal crossing. Place origin at one corner.
        sq = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0)]
        order, mat, total = re.optimize(sq, re.haversine_km, round_trip=True)
        # The perimeter loop (~444 km) is optimal. A route that crosses the
        # diagonal is longer (~536 km): two diagonals plus two sides. The
        # optimizer must find the perimeter, not a crossing.
        side = re.haversine_km((0.0, 0.0), (0.0, 1.0))
        diag = re.haversine_km((0.0, 0.0), (1.0, 1.0))
        assert total < 4 * side + 1.0            # <= perimeter, within rounding
        assert total < 2 * diag + 2 * side - 1.0  # strictly better than crossing
        visited = [i for i in order if i not in (0, len(sq))]
        assert sorted(visited) == [1, 2, 3]


class TestFitToDay:
    def _stores(self, n):
        # a line of stores marching east from Toronto
        return [{'store_number': i, 'lat': 43.6532, 'lng': -79.4 + i * 0.05,
                 'detour_km': 0} for i in range(1, n + 1)]

    def test_day_cap_trims_to_what_fits(self):
        stores = self._stores(40)
        out = re.fit_to_day(stores, TORONTO, re.haversine_km,
                            max_stops=30, dwell_min=25, avg_speed_kmh=50,
                            day_minutes=8 * 60)
        # 30 stops at 25 min dwell alone is 750 min > 480, so it must trim
        assert len(out['stops']) < 30
        assert out['total_min'] <= 8 * 60 + 25  # within a stop of the budget
        assert out['dropped'], 'trimmed stores are reported, not hidden'

    def test_stops_are_sequenced_with_legs(self):
        stores = self._stores(5)
        out = re.fit_to_day(stores, TORONTO, re.haversine_km,
                            max_stops=10, dwell_min=20, avg_speed_kmh=50,
                            day_minutes=10 * 60)
        assert [s['seq'] for s in out['stops']] == [1, 2, 3, 4, 5]
        assert all(s['leg_km'] >= 0 for s in out['stops'])
        assert out['stops'][-1]['cumulative_km'] >= out['stops'][0]['cumulative_km']

    def test_empty_input_is_safe(self):
        out = re.fit_to_day([], TORONTO, re.haversine_km, max_stops=10,
                            dwell_min=20, avg_speed_kmh=50, day_minutes=480)
        assert out['stops'] == [] and out['total_km'] == 0.0


class TestFuel:
    def test_fuel_scales_with_distance(self):
        f = re.fuel_estimate(100)
        assert f['litres'] == 8.9
        assert f['cost'] == round(8.9 * 1.55, 2)
        assert re.fuel_estimate(200)['cost'] > f['cost']
