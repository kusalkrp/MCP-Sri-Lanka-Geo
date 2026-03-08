"""
MCP Tool implementations — Week 2 (5 core tools).

Week 2 tools:
    1. find_nearby             — spatial POI search by radius + optional category
    2. get_poi_details         — full POI record by ID
    3. get_administrative_area — reverse-geocode a coordinate to district/province
    4. validate_coordinates    — check if coordinates are within Sri Lanka bounds
    5. get_coverage_stats      — pre-computed category counts by district

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
    """Register all Week 2 tools onto the FastMCP instance."""

    # ── 1. find_nearby ───────────────────────────────────────────────────────

    @mcp.tool()
    async def find_nearby(
        lat: float,
        lng: float,
        radius_km: float = 5.0,
        category: str | None = None,
        limit: int = 20,
    ) -> dict:
        """
        Find Points of Interest near a coordinate within Sri Lanka.

        Args:
            lat: Latitude (5.85 – 9.9)
            lng: Longitude (79.5 – 81.9)
            radius_km: Search radius in kilometres (default 5, max 100)
            category: Filter by OSM category (e.g. 'amenity', 'shop', 'tourism')
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

            key = redis_cache.spatial_key(lat, lng, radius_km, category)

            async def fetch():
                rows = await postgis.find_pois_nearby(lat, lng, radius_m, category, limit)
                return {
                    "total": len(rows),
                    "results": [
                        {
                            "id":           r["id"],
                            "name":         r["name"],
                            "name_si":      r["name_si"],
                            "category":     r["category"],
                            "subcategory":  r["subcategory"],
                            "lat":          r["lat"],
                            "lng":          r["lng"],
                            "distance_m":   round(r["distance_m"], 1),
                            "address":      r["address"],
                            "quality_score": r["quality_score"],
                        }
                        for r in rows
                    ],
                }

            result, cache_hit = await redis_cache.cached(key, redis_cache.TTL_SPATIAL, fetch)
            log.info("tool_called", tool="find_nearby",
                     duration_ms=round((time.time() - t) * 1000),
                     result_count=result.get("total", 0),
                     cache_hit=cache_hit)
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
