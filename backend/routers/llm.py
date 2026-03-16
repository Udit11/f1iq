"""
/api/llm/* — LLM-powered analysis endpoints (Claude claude-sonnet-4-20250514)

All endpoints require ANTHROPIC_API_KEY env var OR accept
?api_key= query param (for per-user key injection from frontend).

Endpoints:
  POST /api/llm/commentary          — live race commentary snippet
  POST /api/llm/strategy-explain    — plain-English strategy reasoning
  POST /api/llm/whatif-deep         — deep-dive what-if scenario analysis
  POST /api/llm/debrief             — full team post-race debrief
  GET  /api/llm/preview             — next race preview narrative
  GET  /api/llm/status              — whether LLM is available
  GET  /api/llm/stream/commentary   — SSE streaming commentary
"""
from fastapi import APIRouter, Query, Body
from fastapi.responses import StreamingResponse
from typing import Optional
import json

from ..services.llm_service import (
    generate_commentary, explain_strategy, analyze_whatif,
    generate_team_debrief, generate_race_preview, llm_available,
    _call_claude_streaming,
)
from ..services import ergast
from datetime import datetime, timezone

router = APIRouter(prefix="/api/llm", tags=["llm"])


def _get_key(api_key: Optional[str]) -> Optional[str]:
    """Prefer query param key, fall back to env var."""
    import os
    return api_key or os.environ.get("ANTHROPIC_API_KEY")


async def _get_next_race() -> dict | None:
    """Fetch the next upcoming race from Ergast schedule."""
    try:
        schedule = await ergast.get_race_schedule()
        if not schedule:
            return None
        today = datetime.now(timezone.utc).date()
        upcoming = [r for r in schedule if r.get("date") and datetime.fromisoformat(r["date"]).date() >= today]
        if not upcoming:
            return None
        upcoming.sort(key=lambda r: r.get("date"))
        return upcoming[0]
    except Exception:
        return None


async def _get_top_drivers(limit: int = 4) -> list[dict]:
    """Fetch current top driver standings."""
    try:
        standings = await ergast.get_driver_standings()
        return standings[:limit] if standings else []
    except Exception:
        return []


@router.get("/status")
async def llm_status():
    """Check whether Claude LLM is configured and available."""
    available = llm_available()
    return {
        "available": available,
        "model": "claude-sonnet-4-20250514" if available else None,
        "message": (
            "LLM active — live commentary, strategy analysis and debrief generation enabled"
            if available else
            "Set ANTHROPIC_API_KEY environment variable or pass ?api_key= to enable LLM features"
        ),
    }


@router.post("/commentary")
async def live_commentary(
    timing: dict = Body(default=None),
    api_key: Optional[str] = Query(None),
):
    """
    Generate 2-3 sentences of live race commentary.
    POST timing data as JSON body.
    """
    key = _get_key(api_key)
    if not key:
        return {"commentary": None, "error": "No API key — set ANTHROPIC_API_KEY or pass ?api_key="}

    if not timing:
        return {"commentary": None, "error": "Timing data required"}
    data = timing
    try:
        text = await generate_commentary(data, api_key=key)
        return {"commentary": text, "lap": data.get("lap"), "model": "claude-sonnet-4-20250514"}
    except Exception as e:
        return {"commentary": None, "error": str(e)}


@router.post("/strategy-explain")
async def strategy_explain(
    strategy: dict = Body(...),
    api_key: Optional[str] = Query(None),
):
    """
    Generate plain-English explanation of a driver's strategy situation.
    POST a strategy object from /api/live/strategy.
    """
    key = _get_key(api_key)
    if not key:
        return {"explanation": None, "error": "No API key"}
    try:
        text = await explain_strategy(strategy, api_key=key)
        return {"explanation": text, "driver": strategy.get("name_acronym"), "model": "claude-sonnet-4-20250514"}
    except Exception as e:
        return {"explanation": None, "error": str(e)}


@router.post("/whatif-deep")
async def whatif_deep_dive(
    scenario: dict = Body(...),
    race_name: str = Query("Japanese Grand Prix"),
    api_key: Optional[str] = Query(None),
):
    """
    Generate a deeper 150-word analysis of a what-if scenario.
    POST a scenario object from /api/weekend/next.
    """
    key = _get_key(api_key)
    if not key:
        return {"analysis": None, "error": "No API key"}

    race = await _get_next_race()
    race_context = {
        "race_name": race.get("race_name", race_name) if race else race_name,
        "top_drivers": await _get_top_drivers(),
    }
    try:
        text = await analyze_whatif(scenario, race_context, api_key=key)
        return {"analysis": text, "scenario": scenario.get("title"), "model": "claude-sonnet-4-20250514"}
    except Exception as e:
        return {"analysis": None, "error": str(e)}


@router.post("/debrief")
async def team_debrief(
    team_name: str = Query(...),
    year: int = Query(2026),
    round_num: int = Query(2),
    api_key: Optional[str] = Query(None),
):
    """
    Generate a full team debrief using Claude.
    Pulls race results from Ergast/FastF1 when available.
    """
    key = _get_key(api_key)
    if not key:
        return {"debrief": None, "error": "No API key"}

    from ..services import ergast
    try:
        results = await ergast.get_race_results(year, round_num)
    except Exception:
        results = []

    constructors = []
    try:
        constructors = await ergast.get_constructor_standings(year)
    except Exception:
        constructors = []

    constructor = next(
        (c for c in constructors if team_name.lower() in c.get("name", "").lower()),
        {}
    )
    top_points = constructors[0].get("points", 0) if constructors else 0
    race = await _get_next_race()
    champ_ctx = {
        "race_name": f"Round {round_num}, {year}",
        "constructor_position": constructor.get("position", "?"),
        "constructor_points": constructor.get("points", "?"),
        "constructor_gap": max(0, top_points - constructor.get("points", 0)),
        "next_race": race.get("race_name", "TBD") if race else "TBD",
    }

    try:
        debrief = await generate_team_debrief(team_name, results, {}, champ_ctx, api_key=key)
        return {
            "team": team_name,
            "debrief": debrief,
            "model": "claude-sonnet-4-20250514",
        }
    except Exception as e:
        return {"debrief": None, "error": str(e)}


@router.get("/preview")
async def race_preview(api_key: Optional[str] = Query(None)):
    """
    Generate a 200-word pre-race preview narrative for the next race.
    """
    key = _get_key(api_key)
    if not key:
        return {"preview": None, "error": "No API key — set ANTHROPIC_API_KEY or pass ?api_key="}

    race = await _get_next_race()
    if not race:
        return {"preview": "Season complete.", "error": None}
    standings = await _get_top_drivers(limit=20)
    try:
        text = await generate_race_preview(race, standings, api_key=key)
        return {
            "preview": text,
            "race": race.get("race_name"),
            "model": "claude-sonnet-4-20250514",
        }
    except Exception as e:
        return {"preview": None, "error": str(e)}


@router.get("/stream/commentary")
async def stream_commentary(api_key: Optional[str] = Query(None)):
    """
    Server-Sent Events stream of live commentary.
    Frontend connects with EventSource('/api/llm/stream/commentary?api_key=...')
    """
    key = _get_key(api_key)
    if not key:
        async def no_key():
            yield "data: {\"error\": \"No API key\"}\n\n"
        return StreamingResponse(no_key(), media_type="text/event-stream")

    async def no_timing():
        yield "data: {\"error\": \"Live timing data required\"}\n\n"

    return StreamingResponse(no_timing(), media_type="text/event-stream")
