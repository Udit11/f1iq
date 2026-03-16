"""
F1IQ — LLM Service (Claude API)

Uses Claude claude-sonnet-4-20250514 for:
  1. Live race commentary       — real-time narrative from timing data
  2. What-if scenario analysis  — deep strategic reasoning
  3. Post-race debrief writer   — team-specific analysis reports
  4. Strategy explainer         — plain-English pit window reasoning
  5. Race preview writer        — pre-race narrative from weekend data

The API key is injected from the frontend (user provides their own
Anthropic API key via the settings panel) OR set as ANTHROPIC_API_KEY
environment variable.
"""
import httpx
import logging
import json
import os
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)

CLAUDE_MODEL   = "claude-sonnet-4-20250514"
ANTHROPIC_URL  = "https://api.anthropic.com/v1/messages"
ANTHROPIC_BETA = "2023-06-01"


def _get_api_key() -> Optional[str]:
    return os.environ.get("ANTHROPIC_API_KEY")


async def _call_claude(
    system: str,
    user: str,
    max_tokens: int = 800,
    api_key: Optional[str] = None,
) -> str:
    """Single Claude API call, returns text."""
    key = api_key or _get_api_key()
    if not key:
        raise ValueError("No ANTHROPIC_API_KEY set")

    headers = {
        "x-api-key":         key,
        "anthropic-version": ANTHROPIC_BETA,
        "content-type":      "application/json",
    }
    body = {
        "model":      CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "system":     system,
        "messages":   [{"role": "user", "content": user}],
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(ANTHROPIC_URL, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"].strip()


async def _call_claude_streaming(
    system: str,
    user: str,
    max_tokens: int = 1200,
    api_key: Optional[str] = None,
) -> AsyncIterator[str]:
    """Streaming Claude call — yields text chunks."""
    key = api_key or _get_api_key()
    if not key:
        raise ValueError("No ANTHROPIC_API_KEY set")

    headers = {
        "x-api-key":         key,
        "anthropic-version": ANTHROPIC_BETA,
        "content-type":      "application/json",
    }
    body = {
        "model":      CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "stream":     True,
        "system":     system,
        "messages":   [{"role": "user", "content": user}],
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        async with client.stream("POST", ANTHROPIC_URL, headers=headers, json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    try:
                        chunk = json.loads(line[6:])
                        if chunk.get("type") == "content_block_delta":
                            text = chunk.get("delta", {}).get("text", "")
                            if text:
                                yield text
                    except json.JSONDecodeError:
                        continue


# ── Live Race Commentary ──────────────────────────────────────────────────────

COMMENTARY_SYSTEM = """You are an expert Formula 1 race commentator with deep technical knowledge.
You write sharp, exciting, technically accurate commentary in the style of top F1 broadcasters.
Keep commentary concise (2-3 sentences max per call), punchy, and specific to the data provided.
Never make up lap times or statistics not given to you. Focus on the most interesting story of the moment."""


async def generate_commentary(timing_data: dict, api_key: str = None) -> str:
    """
    Generate 2-3 sentences of live race commentary from current timing state.
    Called every 10-15 seconds during a live session.
    """
    lap = timing_data.get("lap", 0)
    total = timing_data.get("total_laps", 60)
    sc = timing_data.get("safety_car", False)
    drivers = timing_data.get("drivers", [])[:5]

    top3 = "\n".join(
        f"P{d['position']} {d['name_acronym']} ({d['team_name']}) — gap: {d['gap_to_leader']} — "
        f"{d['tyre_compound']} tyre age {d['tyre_age']} laps — last lap {d['last_lap_time']}"
        for d in drivers
    )

    prompt = f"""Current race state:
Lap {lap} of {total} ({round(lap/total*100)}% complete)
Track status: {"SAFETY CAR DEPLOYED" if sc else "Green Flag — Racing"}

Top 5:
{top3}

Write 2-3 sentences of live race commentary right now. Focus on the most compelling story."""

    return await _call_claude(COMMENTARY_SYSTEM, prompt, max_tokens=200, api_key=api_key)


# ── Strategy Explainer ────────────────────────────────────────────────────────

STRATEGY_SYSTEM = """You are an F1 strategist at a top team. You explain pit stop decisions in plain English
that a casual fan would understand. Be specific about the numbers but translate them into clear tactical language.
Write in present tense, 3-4 sentences maximum."""


async def explain_strategy(strategy_data: dict, api_key: str = None) -> str:
    """
    Generate a plain-English explanation of a driver's strategic situation.
    """
    prompt = f"""Driver: {strategy_data.get('name_acronym')} ({strategy_data.get('team_name')})
Current position: P{strategy_data.get('current_position')}
Tyre: {strategy_data.get('compound')} compound, {strategy_data.get('tyre_age')} laps old
Tyre health: {strategy_data.get('tyre_health', 75):.0f}%
Current lap: {strategy_data.get('current_lap')} of {strategy_data.get('total_laps')}
Pit window: laps {strategy_data.get('optimal_pit_window_start')}–{strategy_data.get('optimal_pit_window_end')}
Recommendation type: {strategy_data.get('recommendation_type')}

Explain this driver's strategic situation in 3-4 plain-English sentences. What should happen and why?"""

    return await _call_claude(STRATEGY_SYSTEM, prompt, max_tokens=250, api_key=api_key)


# ── What-If Scenario Deep Dive ────────────────────────────────────────────────

WHATIF_SYSTEM = """You are an F1 analyst writing for a specialist motorsport audience.
You have deep knowledge of F1 strategy, regulations, car performance, and race history.
Write sharp, specific analysis — not generic statements. Reference real historical parallels when relevant.
Max 150 words per scenario."""


async def analyze_whatif(scenario: dict, race_context: dict, api_key: str = None) -> str:
    """
    Generate deeper analysis of a what-if scenario using Claude.
    """
    standings_summary = ", ".join(
        f"{d['full_name']} {d['points']}pts"
        for d in race_context.get("top_drivers", [])[:4]
    )

    prompt = f"""2026 F1 Race: {race_context.get('race_name')}
Championship: {standings_summary}

What-if scenario: {scenario.get('title')}
Probability: {scenario.get('probability')}
Impact level: {scenario.get('impact')}

Current summary: {scenario.get('description')}

Write a deeper 100-150 word analysis of this scenario. Include:
- Why this probability is right/wrong
- Historical parallel if one exists
- Specific lap numbers or race phases where it could play out
- The precise championship mathematics if it matters
Be specific and technical."""

    return await _call_claude(WHATIF_SYSTEM, prompt, max_tokens=300, api_key=api_key)


# ── Post-Race Debrief Writer ──────────────────────────────────────────────────

DEBRIEF_SYSTEM = """You are writing the official post-race engineering debrief for an F1 team.
Tone: technical, direct, no corporate fluff. Like a race engineer writing internal notes.
Focus on: what happened, why it happened, what it means for the championship, what to fix."""


async def generate_team_debrief(
    team_name: str,
    race_results: list,
    lap_data: dict,
    championship_context: dict,
    api_key: str = None,
) -> dict:
    """
    Generate a full team debrief using Claude.
    Returns structured dict with summary, issues, actions.
    """
    drivers_info = "\n".join(
        f"  {r.get('abbreviation','?')}: P{r.get('position','?')} "
        f"(started P{r.get('grid','?')}) — {r.get('status','finished')} — "
        f"{r.get('points',0)} points — FL: {r.get('fastest_lap_time','N/A')}"
        for r in race_results if r.get("team", "").lower() in team_name.lower()
                               or team_name.lower() in r.get("team", "").lower()
    )
    if not drivers_info:
        drivers_info = "No result data available for this team"

    champ = championship_context
    prompt = f"""Team: {team_name}
Race: {champ.get('race_name', 'Unknown')}

Driver results:
{drivers_info}

Championship standing after this race:
- Constructor P{champ.get('constructor_position','?')}: {champ.get('constructor_points','?')} pts
- Gap to leader: {champ.get('constructor_gap','?')} pts
- Next race: {champ.get('next_race','?')}

Write a team debrief as JSON with these exact keys:
{{
  "summary": "2-3 sentence race summary",
  "issues": [
    {{"priority": "P1/P2/P3", "description": "specific technical or strategic issue"}}
  ],
  "action_items": ["specific action 1", "specific action 2", "specific action 3", "specific action 4"]
}}

Be specific and technical. P1 = critical/race-losing, P2 = significant, P3 = minor.
Return ONLY the JSON, no other text."""

    raw = await _call_claude(DEBRIEF_SYSTEM, prompt, max_tokens=600, api_key=api_key)

    # Parse JSON response
    try:
        # Strip markdown code fences if present
        clean = raw.strip()
        if clean.startswith("```"):
            clean = "\n".join(clean.split("\n")[1:-1])
        return json.loads(clean)
    except json.JSONDecodeError:
        logger.warning(f"Debrief JSON parse failed, returning raw: {raw[:100]}")
        return {
            "summary": raw[:300],
            "issues": [],
            "action_items": [],
        }


# ── Race Preview Writer ───────────────────────────────────────────────────────

PREVIEW_SYSTEM = """You are an F1 journalist writing a pre-race preview for a specialist F1 audience.
You know the 2026 season in detail: Mercedes dominance, RUS/ANT battle, Ferrari resurgent,
McLaren DNF troubles, Verstappen winless. Be specific, opinionated, technically grounded.
No fluff — every sentence should contain an insight."""


async def generate_race_preview(race: dict, standings: list, api_key: str = None) -> str:
    """
    Generate a 200-word race preview narrative for the next race.
    Used in the Race Weekend panel header.
    """
    top5 = "\n".join(
        f"P{d['position']} {d['full_name']} ({d['team']}) — {d['points']} pts"
        for d in standings[:5]
    )
    sprint = "Sprint weekend (FP1 → SQ → Sprint → Q → Race)" if race.get("sprint") else "Standard weekend"
    circ = race.get("circuit_info", {})

    prompt = f"""Next race: {race.get('race_name')} at {race.get('circuit')}
Format: {sprint}
Circuit: {circ.get('characteristics','N/A')}
Overtaking: {circ.get('overtaking','N/A')}, DRS zones: {circ.get('drs_zones','?')}

Championship top 5:
{top5}

Write a 180-200 word race preview. Include:
1. The key championship storyline for this race
2. One circuit-specific tactical angle
3. One underdog or upset pick with reasoning
4. One sentence on what would make this race historic
Be punchy and specific. No generic statements."""

    return await _call_claude(PREVIEW_SYSTEM, prompt, max_tokens=400, api_key=api_key)


# ── Check if LLM is available ─────────────────────────────────────────────────

def llm_available() -> bool:
    return bool(_get_api_key())
