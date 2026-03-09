"""
MCP Tool implementations — Week 2+3+4 (12 tools).

Tools:
    1.  find_nearby               — spatial POI search by radius + optional category
    2.  get_poi_details           — full POI record by ID
    3.  get_administrative_area   — reverse-geocode a coordinate to district/province
    4.  validate_coordinates      — check if coordinates are within Sri Lanka bounds
    5.  get_coverage_stats        — pre-computed category counts by district
    6.  search_pois               — hybrid semantic + spatial search
    7.  list_categories           — all category/subcategory combinations with counts
    8.  get_business_density      — business breakdown by category for a radius
    9.  route_between             — straight-line distance + bearing between two POIs
    10. find_universities         — find universities and colleges near a point
    11. find_agricultural_zones   — find agricultural landuse zones near a point
    12. find_businesses_near      — find commercial businesses near a point

All tools:
    - Never raise — return {"error": "..."} instead
    - Log duration_ms + result_count on every call
    - Validate Sri Lanka bounds before any spatial query
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from mcp.server.fastmcp import FastMCP

from app.db import postgis
from app.cache import redis_cache
from app.embeddings import qdrant_client as qdrant_mod

log = structlog.get_logger()

# Sri Lanka bounding box
_LAT_MIN, _LAT_MAX = 5.85, 9.9
_LNG_MIN, _LNG_MAX = 79.5, 81.9
_RADIUS_MAX_KM = 100.0


def _validate_coords(lat: float, lng: float) -> str | None:
    """Return error string if invalid, None if valid."""
    if lat == 0.0 and lng == 0.0:
        return "Null island coordinates (0, 0) — provide valid Sri Lanka coordinates"
    if not (_LAT_MIN <= lat <= _LAT_MAX and _LNG_MIN <= lng <= _LNG_MAX):
        return (
            f"Coordinates ({lat}, {lng}) are outside Sri Lanka bounds "
            f"({_LAT_MIN}–{_LAT_MAX}N, {_LNG_MIN}–{_LNG_MAX}E)"
        )
    return None


def register_tools(mcp: FastMCP) -> None:
    """Register all 12 MCP tools onto the FastMCP instance."""

    # ── 1. find_nearby ───────────────────────────────────────────────────────

    @mcp.tool()
    async def find_nearby(
        lat: float,
        lng: float,
        radius_km: float = 5.0,
        category: str | None = None,
        subcategory: str | None = None,
        limit: int = 20,
    ) -> dict:
        """
        Find Points of Interest near a coordinate within Sri Lanka.

        Args:
            lat: Latitude (5.85 – 9.9)
            lng: Longitude (79.5 – 81.9)
            radius_km: Search radius in kilometres (default 5, max 100)
            category: Filter by OSM category (e.g. 'amenity', 'shop', 'tourism')
            subcategory: Filter by OSM subcategory (e.g. 'hospital', 'bank', 'restaurant').
                         Can be used with or without category.
            limit: Max results to return (default 20, max 100)

        Returns:
            {total, results: [{id, name, category, subcategory, lat, lng,
                               distance_m, address, quality_score}]}
        """
        t = time.time()
        try:
            err = _validate_coords(lat, lng)
            if err:
                return {"error": err, "valid": False}

            radius_km = min(float(radius_km), _RADIUS_MAX_KM)
            limit = min(int(limit), 100)
            radius_m = radius_km * 1000.0

            key = redis_cache.spatial_key(lat, lng, radius_km, category) + f":{subcategory or 'all'}:{limit}"

            async def fetch():
                rows = await postgis.find_pois_nearby(lat, lng, radius_m, category, subcategory, limit)
                return {
                    "total": len(rows),
                    "results": [
                        {
                            "id":            r["id"],
                            "name":          r["name"],
                            "name_si":       r["name_si"],
                            "category":      r["category"],
                            "subcategory":   r["subcategory"],
                            "lat":           r["lat"],
                            "lng":           r["lng"],
                            "distance_m":    round(r["distance_m"], 1),
                            "address":       r["address"],
                            "quality_score": r["quality_score"],
                        }
                        for r in rows
                    ],
                }

            result, cache_hit = await redis_cache.cached(key, redis_cache.TTL_SPATIAL, fetch)
            log.info("tool_called", tool="find_nearby",
                     duration_ms=round((time.time() - t) * 1000),
                     result_count=result.get("total", 0),
                     cache_hit=cache_hit,
                     subcategory=subcategory)
            return result

        except Exception as exc:
            log.error("tool_failed", tool="find_nearby", error=repr(exc))
            return {"error": "Internal error — try again"}

    # ── 2. get_poi_details ───────────────────────────────────────────────────

    @mcp.tool()
    async def get_poi_details(poi_id: str) -> dict:
        """
        Get full details for a single Point of Interest by its OSM-prefixed ID.

        Args:
            poi_id: OSM-prefixed ID string, e.g. 'n12345678', 'w67890', 'r111'

        Returns:
            Full POI record or {"error": "Not found"}
        """
        t = time.time()
        try:
            poi_id = poi_id.strip()
            if not poi_id:
                return {"error": "poi_id must not be empty"}

            key = redis_cache.poi_detail_key(poi_id)

            async def fetch():
                row = await postgis.get_poi_by_id(poi_id)
                if row is None:
                    return None
                return {
                    "id":           row["id"],
                    "osm_id":       row["osm_id"],
                    "osm_type":     row["osm_type"],
                    "name":         row["name"],
                    "name_si":      row["name_si"],
                    "name_ta":      row["name_ta"],
                    "category":     row["category"],
                    "subcategory":  row["subcategory"],
                    "lat":          row["lat"],
                    "lng":          row["lng"],
                    "address":      row["address"],
                    "tags":         row["tags"],
                    "wikidata_id":  row["wikidata_id"],
                    "geonames_id":  row["geonames_id"],
                    "enrichment":   row["enrichment"],
                    "data_source":  row["data_source"],
                    "quality_score": row["quality_score"],
                    "last_osm_sync": str(row["last_osm_sync"]) if row["last_osm_sync"] else None,
                }

            result, cache_hit = await redis_cache.cached(key, redis_cache.TTL_POI_DETAIL, fetch)

            if result is None:
                log.info("tool_called", tool="get_poi_details",
                         duration_ms=round((time.time() - t) * 1000),
                         found=False, cache_hit=False)
                return {"error": f"POI not found: {poi_id}"}

            log.info("tool_called", tool="get_poi_details",
                     duration_ms=round((time.time() - t) * 1000),
                     found=True, cache_hit=cache_hit)
            return result

        except Exception:
            log.error("tool_failed", tool="get_poi_details")
            return {"error": "Internal error — try again"}

    # ── 3. get_administrative_area ───────────────────────────────────────────

    @mcp.tool()
    async def get_administrative_area(lat: float, lng: float) -> dict:
        """
        Reverse-geocode coordinates to Sri Lanka administrative area.

        Args:
            lat: Latitude (5.85 – 9.9)
            lng: Longitude (79.5 – 81.9)

        Returns:
            {district, province, ds_division} — ds_division may be null
            if DS Division data not loaded.
        """
        t = time.time()
        try:
            err = _validate_coords(lat, lng)
            if err:
                return {"error": err, "valid": False}

            key = redis_cache.admin_key(lat, lng)

            async def fetch():
                return await postgis.get_admin_area_for_point(lat, lng)

            result, cache_hit = await redis_cache.cached(key, redis_cache.TTL_ADMIN, fetch)
            log.info("tool_called", tool="get_administrative_area",
                     duration_ms=round((time.time() - t) * 1000),
                     district=result.get("district"), cache_hit=cache_hit)
            return result

        except Exception:
            log.error("tool_failed", tool="get_administrative_area")
            return {"error": "Internal error — try again"}

    # ── 4. validate_coordinates ──────────────────────────────────────────────

    @mcp.tool()
    async def validate_coordinates(lat: float, lng: float) -> dict:
        """
        Check if coordinates fall within Sri Lanka's bounding box.
        Use this before any spatial query to avoid out-of-bounds errors.

        Args:
            lat: Latitude to validate
            lng: Longitude to validate

        Returns:
            {valid: bool, lat, lng, message}
        """
        try:
            err = _validate_coords(lat, lng)
            if err:
                return {
                    "valid":   False,
                    "lat":     lat,
                    "lng":     lng,
                    "message": err,
                    "bounds":  {
                        "lat_min": _LAT_MIN, "lat_max": _LAT_MAX,
                        "lng_min": _LNG_MIN, "lng_max": _LNG_MAX,
                    },
                }
            return {
                "valid":   True,
                "lat":     lat,
                "lng":     lng,
                "message": "Coordinates are within Sri Lanka bounds",
            }
        except Exception:
            log.error("tool_failed", tool="validate_coordinates")
            return {"error": "Internal error"}

    # ── 5. get_coverage_stats ────────────────────────────────────────────────

    @mcp.tool()
    async def get_coverage_stats(district: str | None = None) -> dict:
        """
        Get pre-computed POI category counts for Sri Lanka or a specific district.

        Args:
            district: District name (e.g. 'Colombo', 'Kandy') or None for national stats

        Returns:
            {district_filter, total_pois, categories: [{category, subcategory, poi_count}]}
        """
        t = time.time()
        try:
            key = redis_cache.categories_key(district)

            async def fetch():
                rows = await postgis.get_coverage_stats(district)
                total = sum(r.get("poi_count", 0) for r in rows)
                return {
                    "district_filter": district,
                    "total_pois":      total,
                    "categories":      rows,
                }

            result, cache_hit = await redis_cache.cached(key, redis_cache.TTL_CATEGORIES, fetch)
            log.info("tool_called", tool="get_coverage_stats",
                     duration_ms=round((time.time() - t) * 1000),
                     district=district, cache_hit=cache_hit)
            return result

        except Exception:
            log.error("tool_failed", tool="get_coverage_stats")
            return {"error": "Internal error — try again"}

    # ── 6. search_pois ───────────────────────────────────────────────────────

    @mcp.tool()
    async def search_pois(
        query: str,
        lat: float | None = None,
        lng: float | None = None,
        radius_km: float = 10.0,
        category: str | None = None,
        limit: int = 10,
    ) -> dict:
        """
        Hybrid semantic + spatial search for Points of Interest in Sri Lanka.

        Combines PostGIS spatial pre-filtering with Gemini vector embeddings
        and Qdrant cosine similarity ranking.

        Args:
            query: Natural language search (e.g. 'hospital near me', 'Buddhist temple')
            lat: Optional latitude for spatial constraint
            lng: Optional longitude for spatial constraint
            radius_km: Search radius in km when coordinates given (default 10)
            category: Optional category filter (e.g. 'amenity', 'tourism')
            limit: Max results (default 10, max 50)

        Returns:
            {query, total, results: [{poi_id, name, category, district,
                                      semantic_score, distance_m}]}
        """
        t = time.time()
        try:
            query = query.strip()
            if not query:
                return {"error": "query must not be empty"}

            limit = min(int(limit), 50)
            radius_km = min(float(radius_km), _RADIUS_MAX_KM)
            radius_m  = radius_km * 1000.0

            # Validate coords if given
            if lat is not None and lng is not None:
                err = _validate_coords(lat, lng)
                if err:
                    return {"error": err, "valid": False}

            # ── Stage 1: Spatial pre-filter (PostGIS) ────────────────────────
            spatial_ids = None
            spatial_distances: dict[str, float] = {}

            if lat is not None and lng is not None:
                candidates = await postgis.get_spatial_candidates(
                    lat, lng, radius_m, category, max_candidates=500
                )
                # CRITICAL: 0 results in radius → return empty immediately
                # Never fall through to unconstrained Qdrant search
                if not candidates:
                    log.info("tool_called", tool="search_pois",
                             duration_ms=round((time.time() - t) * 1000),
                             result_count=0, spatial_candidates=0)
                    return {"query": query, "total": 0, "results": []}

                spatial_ids = [c["id"] for c in candidates]
                spatial_distances = {c["id"]: c["distance_m"] for c in candidates}

            # ── Stage 2: Embed query (with Redis cache) ───────────────────────
            cache_key = redis_cache.semantic_key(query, lat, lng, radius_km, category)
            cached_result, cache_hit = await redis_cache.cached(
                cache_key,
                redis_cache.TTL_SEMANTIC,
                _make_semantic_fetch(query, spatial_ids, spatial_distances, category, limit),
            )

            log.info("tool_called", tool="search_pois",
                     duration_ms=round((time.time() - t) * 1000),
                     result_count=cached_result.get("total", 0),
                     cache_hit=cache_hit,
                     spatial_candidates=len(spatial_ids) if spatial_ids else None)
            return cached_result

        except Exception as exc:
            log.error("tool_failed", tool="search_pois", error=repr(exc))
            return {"error": "Internal error — try again"}

    # Register Week 4 domain tools (7–12)
    _register_week4_tools(mcp)


def _make_semantic_fetch(
    query: str,
    spatial_ids: list[str] | None,
    spatial_distances: dict[str, float],
    category: str | None,
    limit: int,
):
    """Return an async callable for the cache-aside fetch_fn."""
    async def fetch():
        vector = await qdrant_mod.embed_query_cached(query)
        qdrant_results = await qdrant_mod.search_collection(
            vector=vector,
            filter_ids=spatial_ids,
            category=category,
            limit=limit,
        )
        results = []
        for r in qdrant_results:
            poi_id = r.get("poi_id")
            results.append({
                "poi_id":         poi_id,
                "name":           r.get("name"),
                "name_si":        r.get("name_si"),
                "category":       r.get("category"),
                "subcategory":    r.get("subcategory"),
                "district":       r.get("district"),
                "province":       r.get("province"),
                "lat":            r.get("lat"),
                "lng":            r.get("lng"),
                "semantic_score": round(r.get("score", 0), 4),
                "distance_m":     round(spatial_distances[poi_id], 1)
                                  if poi_id in spatial_distances else None,
            })
        return {"query": query, "total": len(results), "results": results}
    return fetch


def _register_week4_tools(mcp: FastMCP) -> None:
    """Register tools 7–12 (Week 4 domain tools)."""

    # ── 7. list_categories ───────────────────────────────────────────────────

    @mcp.tool()
    async def list_categories(district: str | None = None) -> dict:
        """
        List all POI category/subcategory combinations with counts for Sri Lanka
        or a specific district.

        Args:
            district: District name (e.g. 'Colombo', 'Kandy') or None for national

        Returns:
            {district_filter, total_categories, categories: [{category, subcategory, poi_count}]}
        """
        t = time.time()
        try:
            # Use "list_cats:" prefix — distinct from get_coverage_stats "categories:" key
            key = f"list_cats:{district or 'all'}"

            async def fetch():
                rows = await postgis.get_coverage_stats(district)
                return {
                    "district_filter":  district,
                    "total_categories": len(rows),
                    "categories":       rows,
                }

            result, cache_hit = await redis_cache.cached(key, redis_cache.TTL_CATEGORIES, fetch)
            log.info("tool_called", tool="list_categories",
                     duration_ms=round((time.time() - t) * 1000),
                     district=district, cache_hit=cache_hit)
            return result

        except Exception:
            log.error("tool_failed", tool="list_categories")
            return {"error": "Internal error — try again"}

    # ── 8. get_business_density ──────────────────────────────────────────────

    @mcp.tool()
    async def get_business_density(
        lat: float,
        lng: float,
        radius_km: float = 2.0,
    ) -> dict:
        """
        Get business density breakdown by category for a given radius.
        Aggregates only POIs within the radius — never the full table.

        Args:
            lat: Latitude (5.85 – 9.9)
            lng: Longitude (79.5 – 81.9)
            radius_km: Radius in kilometres (default 2, max 50)

        Returns:
            {lat, lng, radius_km, total_pois, breakdown: [{category, subcategory, poi_count}]}
        """
        t = time.time()
        try:
            err = _validate_coords(lat, lng)
            if err:
                return {"error": err, "valid": False}

            radius_km = min(float(radius_km), 50.0)
            radius_m  = radius_km * 1000.0

            key = redis_cache.density_key(lat, lng, radius_km)

            async def fetch():
                rows = await postgis.get_density_breakdown(lat, lng, radius_m)
                total = sum(r.get("poi_count", 0) for r in rows)
                return {
                    "lat":        lat,
                    "lng":        lng,
                    "radius_km":  radius_km,
                    "total_pois": total,
                    "breakdown":  rows,
                }

            result, cache_hit = await redis_cache.cached(key, redis_cache.TTL_DENSITY, fetch)
            log.info("tool_called", tool="get_business_density",
                     duration_ms=round((time.time() - t) * 1000),
                     result_count=result.get("total_pois", 0),
                     cache_hit=cache_hit)
            return result

        except Exception:
            log.error("tool_failed", tool="get_business_density")
            return {"error": "Internal error — try again"}

    # ── 9. route_between ────────────────────────────────────────────────────

    @mcp.tool()
    async def route_between(
        origin_poi_id: str,
        dest_poi_id: str,
    ) -> dict:
        """
        Calculate straight-line distance and bearing between two POIs.
        Note: v1 provides straight-line distance only — no road routing.

        Args:
            origin_poi_id: OSM-prefixed ID of the origin POI (e.g. 'n12345678')
            dest_poi_id:   OSM-prefixed ID of the destination POI

        Returns:
            {origin, destination, distance_m, distance_km, bearing_deg, note}
        """
        t = time.time()
        try:
            origin_poi_id = origin_poi_id.strip()
            dest_poi_id   = dest_poi_id.strip()

            if not origin_poi_id or not dest_poi_id:
                return {"error": "origin_poi_id and dest_poi_id must not be empty"}
            if origin_poi_id == dest_poi_id:
                return {"error": "origin and destination must be different POIs"}

            data = await postgis.get_route_data(origin_poi_id, dest_poi_id)

            if data is None:
                # Single query to identify which POI(s) are missing
                existing = await postgis.check_pois_exist(origin_poi_id, dest_poi_id)
                if origin_poi_id not in existing:
                    return {"error": f"POI not found or deleted: {origin_poi_id}"}
                if dest_poi_id not in existing:
                    return {"error": f"POI not found or deleted: {dest_poi_id}"}
                return {"error": "Could not compute route data"}

            result = {
                "origin": {
                    "poi_id": data["origin_id"],
                    "name":   data["origin_name"],
                    "lat":    data["origin_lat"],
                    "lng":    data["origin_lng"],
                },
                "destination": {
                    "poi_id": data["dest_id"],
                    "name":   data["dest_name"],
                    "lat":    data["dest_lat"],
                    "lng":    data["dest_lng"],
                },
                "distance_m":   float(data["distance_m"]),
                "distance_km":  round(float(data["distance_m"]) / 1000, 3),
                "bearing_deg":  float(data["bearing_deg"]),
                "note":         "Straight-line distance only — road routing not available in v1",
            }
            log.info("tool_called", tool="route_between",
                     duration_ms=round((time.time() - t) * 1000),
                     distance_km=result["distance_km"])
            return result

        except Exception:
            log.error("tool_failed", tool="route_between")
            return {"error": "Internal error — try again"}

    # ── 10. find_universities ───────────────────────────────────────────────

    @mcp.tool()
    async def find_universities(
        lat: float,
        lng: float,
        radius_km: float = 20.0,
        limit: int = 20,
    ) -> dict:
        """
        Find universities and colleges near a coordinate.
        Covers amenity=university, amenity=college, office=educational_institution.

        Args:
            lat: Latitude (5.85 – 9.9)
            lng: Longitude (79.5 – 81.9)
            radius_km: Search radius in km (default 20)
            limit: Max results (default 20, max 100)

        Returns:
            {total, results: [{id, name, subcategory, lat, lng, distance_m, address}]}
        """
        t = time.time()
        try:
            err = _validate_coords(lat, lng)
            if err:
                return {"error": err, "valid": False}

            radius_km = min(float(radius_km), _RADIUS_MAX_KM)
            limit     = min(int(limit), 100)
            radius_m  = radius_km * 1000.0

            key = redis_cache.spatial_key(lat, lng, radius_km, "universities") + f":{limit}"

            async def fetch():
                rows = await postgis.find_universities_nearby(lat, lng, radius_m, limit)
                return {
                    "total":   len(rows),
                    "results": [
                        {
                            "id":          r["id"],
                            "name":        r["name"],
                            "name_si":     r["name_si"],
                            "category":    r["category"],
                            "subcategory": r["subcategory"],
                            "lat":         r["lat"],
                            "lng":         r["lng"],
                            "distance_m":  round(r["distance_m"], 1),
                            "address":     r["address"],
                            "quality_score": r["quality_score"],
                        }
                        for r in rows
                    ],
                }

            result, cache_hit = await redis_cache.cached(key, redis_cache.TTL_SPATIAL, fetch)
            log.info("tool_called", tool="find_universities",
                     duration_ms=round((time.time() - t) * 1000),
                     result_count=result.get("total", 0), cache_hit=cache_hit)
            return result

        except Exception:
            log.error("tool_failed", tool="find_universities")
            return {"error": "Internal error — try again"}

    # ── 11. find_agricultural_zones ─────────────────────────────────────────

    @mcp.tool()
    async def find_agricultural_zones(
        lat: float,
        lng: float,
        radius_km: float = 10.0,
        limit: int = 20,
    ) -> dict:
        """
        Find agricultural landuse zones near a coordinate.
        Covers farmland, orchard, greenhouse, aquaculture, vineyard, reservoir.

        Args:
            lat: Latitude (5.85 – 9.9)
            lng: Longitude (79.5 – 81.9)
            radius_km: Search radius in km (default 10)
            limit: Max results (default 20, max 100)

        Returns:
            {total, results: [{id, name, subcategory, lat, lng, distance_m, address}]}
        """
        t = time.time()
        try:
            err = _validate_coords(lat, lng)
            if err:
                return {"error": err, "valid": False}

            radius_km = min(float(radius_km), _RADIUS_MAX_KM)
            limit     = min(int(limit), 100)
            radius_m  = radius_km * 1000.0

            key = redis_cache.spatial_key(lat, lng, radius_km, "agri") + f":{limit}"

            async def fetch():
                rows = await postgis.find_agricultural_zones_nearby(lat, lng, radius_m, limit)
                return {
                    "total":   len(rows),
                    "results": [
                        {
                            "id":          r["id"],
                            "name":        r["name"],
                            "name_si":     r.get("name_si"),
                            "subcategory": r["subcategory"],
                            "lat":         r["lat"],
                            "lng":         r["lng"],
                            "distance_m":  round(r["distance_m"], 1),
                            "address":     r["address"],
                            "tags":        r["tags"],
                        }
                        for r in rows
                    ],
                }

            result, cache_hit = await redis_cache.cached(key, redis_cache.TTL_SPATIAL, fetch)
            log.info("tool_called", tool="find_agricultural_zones",
                     duration_ms=round((time.time() - t) * 1000),
                     result_count=result.get("total", 0), cache_hit=cache_hit)
            return result

        except Exception:
            log.error("tool_failed", tool="find_agricultural_zones")
            return {"error": "Internal error — try again"}

    # ── 12. find_businesses_near ────────────────────────────────────────────

    @mcp.tool()
    async def find_businesses_near(
        lat: float,
        lng: float,
        radius_km: float = 5.0,
        business_type: str | None = None,
        limit: int = 20,
    ) -> dict:
        """
        Find commercial businesses near a coordinate.
        Covers shops, offices, and commercial amenities (restaurants, banks, pharmacies, etc.)

        Args:
            lat: Latitude (5.85 – 9.9)
            lng: Longitude (79.5 – 81.9)
            radius_km: Search radius in km (default 5, max 100)
            business_type: Filter by subcategory (e.g. 'restaurant', 'bank', 'pharmacy',
                           'supermarket', 'fuel', 'cafe', 'atm') or None for all businesses
            limit: Max results (default 20, max 100)

        Returns:
            {total, business_type_filter, results: [{id, name, category, subcategory,
                                                     lat, lng, distance_m, address}]}
        """
        t = time.time()
        try:
            err = _validate_coords(lat, lng)
            if err:
                return {"error": err, "valid": False}

            radius_km = min(float(radius_km), _RADIUS_MAX_KM)
            limit     = min(int(limit), 100)
            radius_m  = radius_km * 1000.0

            cache_cat = f"biz_{business_type}" if business_type else "biz"
            key = redis_cache.spatial_key(lat, lng, radius_km, cache_cat) + f":{limit}"

            async def fetch():
                rows = await postgis.find_businesses_nearby(
                    lat, lng, radius_m, business_type, limit
                )
                return {
                    "total":               len(rows),
                    "business_type_filter": business_type,
                    "results": [
                        {
                            "id":          r["id"],
                            "name":        r["name"],
                            "name_si":     r.get("name_si"),
                            "category":    r["category"],
                            "subcategory": r["subcategory"],
                            "lat":         r["lat"],
                            "lng":         r["lng"],
                            "distance_m":  round(r["distance_m"], 1),
                            "address":     r["address"],
                        }
                        for r in rows
                    ],
                }

            result, cache_hit = await redis_cache.cached(key, redis_cache.TTL_SPATIAL, fetch)
            log.info("tool_called", tool="find_businesses_near",
                     duration_ms=round((time.time() - t) * 1000),
                     result_count=result.get("total", 0),
                     business_type=business_type, cache_hit=cache_hit)
            return result

        except Exception:
            log.error("tool_failed", tool="find_businesses_near")
            return {"error": "Internal error — try again"}
