"""Day-route optimization for the field team.

Pure geometry and heuristics, no network and no database, so the whole thing is
deterministic and unit-testable. The app layer supplies the coordinates (real
address geocoding) and, when available, a real driving-distance matrix from
OSRM; everything in here is math over points.

The job: a rep names where they are starting and a city they need to reach. We
pick the LCBO stores worth hitting on the way there, in and around the
destination, and on the way back, then order them so the day is the fewest
kilometres of driving, capped by how many stops actually fit in a working day.
An optional layer weaves in on-trade accounts near the same corridor.
"""

import math

EARTH_KM = 6371.0


def haversine_km(a, b):
    lat1, lng1 = a
    lat2, lng2 = b
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    h = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return 2 * EARTH_KM * math.asin(math.sqrt(h))


def _project(p, ref_lat):
    """Local equirectangular metres from a reference latitude. Good enough for
    corridor geometry at city and provincial scale, and cheap."""
    lat, lng = p
    x = math.radians(lng) * math.cos(math.radians(ref_lat)) * EARTH_KM * 1000
    y = math.radians(lat) * EARTH_KM * 1000
    return x, y


def point_to_segment_km(p, a, b):
    """Shortest distance from point p to the segment a->b, in km.

    This is what makes a store count as 'on the way' rather than merely near one
    end. A store a few km off the straight line between home and the city is a
    cheap detour; one far off it is not.
    """
    ref = a[0]
    px, py = _project(p, ref)
    ax, ay = _project(a, ref)
    bx, by = _project(b, ref)
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 == 0:
        return haversine_km(p, a)
    t = ((px - ax) * dx + (py - ay) * dy) / seg2
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy) / 1000.0


def corridor_select(stores, origin, dest, corridor_km, dest_radius_km):
    """Keep the stores that lie along the origin->dest corridor OR inside the
    destination catchment. Each kept store is tagged with why and with its
    detour distance from the line, so the caller can rank cheap detours first.

    stores: list of dicts with 'lat','lng'. Returns a filtered, annotated copy.
    """
    kept = []
    for s in stores:
        p = (s['lat'], s['lng'])
        d_line = point_to_segment_km(p, origin, dest)
        d_dest = haversine_km(p, dest)
        on_corridor = d_line <= corridor_km
        near_dest = d_dest <= dest_radius_km
        if on_corridor or near_dest:
            kept.append({
                **s,
                'detour_km': round(d_line, 2),
                'dest_dist_km': round(d_dest, 2),
                'why': 'destination_area' if near_dest else 'on_corridor',
            })
    return kept


def _matrix(points, dist_fn):
    n = len(points)
    m = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = dist_fn(points[i], points[j])
            m[i][j] = m[j][i] = d
    return m


def nearest_neighbor(mat, start, must_end=None):
    n = len(mat)
    unvisited = set(range(n))
    unvisited.discard(start)
    if must_end is not None:
        unvisited.discard(must_end)
    order = [start]
    cur = start
    while unvisited:
        nxt = min(unvisited, key=lambda j: mat[cur][j])
        order.append(nxt)
        unvisited.remove(nxt)
        cur = nxt
    if must_end is not None:
        order.append(must_end)
    return order


def _route_len(order, mat):
    return sum(mat[order[i]][order[i + 1]] for i in range(len(order) - 1))


def two_opt(order, mat, fixed_start=True, fixed_end=False, max_passes=40):
    """Classic 2-opt. Reverses segments while it shortens the route. The first
    node (origin) is held fixed; the last is held too on a round trip."""
    best = order[:]
    lo = 1 if fixed_start else 0
    improved = True
    passes = 0
    while improved and passes < max_passes:
        improved = False
        passes += 1
        hi = len(best) - (1 if fixed_end else 0)
        for i in range(lo, hi - 1):
            for k in range(i + 1, hi):
                a, b = best[i - 1], best[i]
                c = best[k]
                d = best[k + 1] if k + 1 < len(best) else None
                before = mat[a][b] + (mat[c][d] if d is not None else 0)
                after = mat[a][c] + (mat[b][d] if d is not None else 0)
                if after + 1e-9 < before:
                    best[i:k + 1] = best[i:k + 1][::-1]
                    improved = True
    return best


def optimize(points, dist_fn, round_trip=True):
    """Order points[0..] starting at points[0] (the origin). round_trip returns
    to the origin at the end. Returns (order_indices, matrix, total_km)."""
    n = len(points)
    if n <= 1:
        return list(range(n)), [[0.0]], 0.0
    if round_trip:
        pts = points + [points[0]]
        mat = _matrix(pts, dist_fn)
        end = len(pts) - 1
        order = nearest_neighbor(mat, 0, must_end=end)
        order = two_opt(order, mat, fixed_start=True, fixed_end=True)
    else:
        mat = _matrix(points, dist_fn)
        order = nearest_neighbor(mat, 0)
        order = two_opt(order, mat, fixed_start=True, fixed_end=False)
    return order, mat, _route_len(order, mat)


def fit_to_day(candidates, origin, dist_fn, *, max_stops, dwell_min,
               avg_speed_kmh, day_minutes, round_trip=True):
    """Choose which candidates to actually visit and in what order.

    Greedy on the caller's ordering (candidates should already be sorted best
    first), then optimise the chosen set, then trim from the least valuable end
    until the day fits: driving time at avg_speed_kmh plus dwell_min per stop
    must sit inside day_minutes. Honest about the cap instead of returning a
    route no one can finish.
    """
    chosen = candidates[:max_stops] if max_stops else candidates[:]
    dropped_for_cap = candidates[len(chosen):]

    def plan(sel):
        pts = [origin] + [(s['lat'], s['lng']) for s in sel]
        order, mat, total_km = optimize(pts, dist_fn, round_trip=round_trip)
        drive_min = total_km / max(avg_speed_kmh, 1e-6) * 60.0
        total_min = drive_min + dwell_min * len(sel)
        return order, mat, total_km, drive_min, total_min

    while chosen:
        order, mat, total_km, drive_min, total_min = plan(chosen)
        if total_min <= day_minutes or len(chosen) == 1:
            break
        dropped_for_cap.insert(0, chosen.pop())

    if not chosen:
        return {'stops': [], 'total_km': 0.0, 'drive_min': 0.0,
                'total_min': 0.0, 'dropped': dropped_for_cap}

    # order indexes into [origin] + chosen; map back to store dicts in sequence
    seq = []
    prev_km = 0.0
    cum_km = 0.0
    for pos, idx in enumerate(order):
        if idx == 0 or idx == len(chosen) + 1:
            continue  # origin endpoints
        store = chosen[idx - 1]
        leg_km = mat[order[pos - 1]][idx]
        cum_km += leg_km
        seq.append({
            **store,
            'seq': len(seq) + 1,
            'leg_km': round(leg_km, 2),
            'cumulative_km': round(cum_km, 2),
            'cumulative_drive_min': round(cum_km / max(avg_speed_kmh, 1e-6) * 60.0, 1),
        })
    return {
        'stops': seq,
        'total_km': round(total_km, 2),
        'drive_min': round(drive_min, 1),
        'total_min': round(total_min, 1),
        'dropped': dropped_for_cap,
    }


def fuel_estimate(km, l_per_100km=8.9, price_per_l=1.55):
    """A plain fuel-cost figure for the day. Defaults: a mid-size car and a
    round Ontario pump price. Both are arguments so they are easy to update.
    """
    litres = km * l_per_100km / 100.0
    return {'litres': round(litres, 1), 'cost': round(litres * price_per_l, 2),
            'l_per_100km': l_per_100km, 'price_per_l': price_per_l}
