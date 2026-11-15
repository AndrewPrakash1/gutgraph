"""
Mixologist orchestrator — the only agent that talks to the user
(per the Notion plan). Pipeline:

  parse meal -> truth layer (gut_engine) -> convene parliament
  -> aggregate -> pairing tool -> drink card

Emits SSE-ready event dicts. Everything here is mock-safe: with no
DeepSeek key the agent lines come from persona templates, and the
numbers ALWAYS come from gut_engine, never from a model.
"""

import asyncio
from typing import Any, AsyncIterator, Dict, List, Optional

from demo_data import KEYWORD_MAP, MEAL_BY_ID, MEALS
from gut_engine import compute_gut_score

from . import foodo_tools, tool_loop
from .personas import AGENTS, MIXOLOGIST
from .room import GutParliament, normalize_metrics

# Spices and aromatics carry the flavour-molecule story, so a live pairing is asked
# about one of these when the plate has one (in this order), else the first ingredient.
HERO_PREFERENCE = ("turmeric", "ginger", "cumin", "garlic", "coriander", "chilli",
                   "red chile", "pepper", "mint", "lemon", "mango", "curd", "yogurt")
MAX_DRINK_CANDIDATES = 3


def _tool_event(result: Dict[str, Any]) -> Dict[str, Any]:
    """One SSE frame describing a real database read — name, args, source, timing."""
    return {
        "event": "tool",
        "tool": result.get("tool"),
        "args": result.get("args"),
        "source": result.get("source"),
        "ok": result.get("ok"),
        "ms": result.get("ms"),
        "error": result.get("error"),
        "output": result.get("data"),
    }


def _hero_ingredient(ingredients: List[str]) -> Optional[str]:
    lowered = [i.lower() for i in ingredients]
    for wanted in HERO_PREFERENCE:
        for name in lowered:
            if wanted in name:
                return name
    return lowered[0] if lowered else None


async def _resolve_meal_with_tools(message: str) -> Optional[Dict[str, Any]]:
    """The Mixologist reads RecipeDB for a meal the keyword map does not know.

    Before this, an unknown dish got "my recipe knowledge is still mocked" and nothing
    else — the mock list was the ceiling. Now the agent asks RecipeDB for the real
    ingredient list (through the tool loop, so the call is the model's decision) and
    gut_engine scores what comes back. Returns None when nothing usable was found.
    """
    if not tool_loop.enabled():
        return None
    messages = [
        {"role": "system", "content": MIXOLOGIST["system"] + " " + foodo_tools.TOOL_PROMPT +
         " The user has named a meal. Call recipe_lookup once with the dish name, then reply"
         " with only the dish name you found."},
        {"role": "user", "content": message},
    ]
    res = await tool_loop.run(messages, max_tokens=160, temperature=0.2, max_hops=2)
    calls = [c for c in res["calls"] if c.get("tool") == "recipe_lookup"]
    if not calls:
        return None
    call = calls[-1]
    if not call.get("ok"):
        return {"call": call, "failed": call.get("error")}
    data = call.get("data") or {}
    names = [i.get("name") for i in (data.get("ingredients") or []) if i.get("name")]
    if not names:
        return {"call": call, "failed": data.get("note") or "no ingredients returned"}
    return {"call": call, "dish": data.get("dish") or data.get("asked_for"),
            "ingredients": names, "calories": data.get("calories"),
            "region": data.get("region"), "minutes": data.get("minutes"),
            "diet": data.get("diet"), "alternatives": data.get("alternatives") or []}


async def _live_pairing(hero: Optional[str]) -> Optional[Dict[str, Any]]:
    """Pick the demo drink whose own hero ingredient shares the most flavour molecules
    with the meal's hero ingredient — a real FlavorDB ranking, not a curated table.

    Best Jaccard wins; the whole call is skipped (None) when FlavorDB cannot resolve
    either side, and the caller then uses the curated numbers instead — never faked.
    """
    if not tool_loop.enabled() or not hero:
        return None
    hero_resolved = foodo_tools.resolve_food(hero)
    if not hero_resolved:
        return None
    best: Optional[Dict[str, Any]] = None
    tried = 0
    for meal in MEALS:
        drink = meal.get("drink") or {}
        drink_hero = _hero_ingredient([str(i) for i in (drink.get("ingredients") or [])])
        if not drink_hero:
            continue
        tried += 1
        if tried > MAX_DRINK_CANDIDATES:
            break
        result = await asyncio.to_thread(foodo_tools.execute, "shared_molecules",
                                         {"food_a": hero_resolved, "food_b": drink_hero})
        data = result.get("data") or {}
        score = data.get("jaccard")
        if not result.get("ok") or score is None:
            continue
        if best is None or score > best["jaccard"]:
            best = {"drink": drink.get("name"), "explanation": drink.get("explanation"),
                    "prep": drink.get("prep"), "jaccard": score,
                    "shared": data.get("shared") or [], "shared_count": data.get("shared_count"),
                    "molecules_a": data.get("molecules_a"), "molecules_b": data.get("molecules_b"),
                    "pair_with": data.get("food_b"), "hero": data.get("food_a"),
                    "result": result}
    return best

STATUS_DELAY_S = 0.2


def _mixologist_event() -> Dict:
    return {"event": "agent_start", "agent": {
        "handle": MIXOLOGIST["handle"], "name": MIXOLOGIST["name"],
        "emoji": MIXOLOGIST["emoji"], "role": MIXOLOGIST["role"],
        "color": MIXOLOGIST["color"],
    }}


async def _speak(text: str, agent: str = MIXOLOGIST["handle"]) -> AsyncIterator[Dict]:
    for word in text.split(" "):
        yield {"event": "token", "agent": agent, "delta": word + " "}
        await asyncio.sleep(0.014)


def _match_meal(message: str):
    """Keyword match, but honest: returns None when nothing matches
    (find_meal silently falls back to noodles — fine for the old UI,
    wrong for a chat agent)."""
    text = message.lower()
    for keyword, meal_id in KEYWORD_MAP.items():
        if keyword in text:
            return MEAL_BY_ID[meal_id]
    return None


async def run_pipeline(message: str) -> AsyncIterator[Dict]:
    m = MIXOLOGIST

    yield _mixologist_event()
    async for ev in _speak("On it. Let me parse that meal and pull the facts."):
        yield ev
    await asyncio.sleep(STATUS_DELAY_S)

    meal = _match_meal(message)
    live: Optional[Dict[str, Any]] = None

    if meal is None:
        # Unknown to the keyword map: the Mixologist reads RecipeDB instead of giving up.
        async for ev in _speak("I don't have that one in my shortlist — let me look it up."):
            yield ev
        await asyncio.sleep(STATUS_DELAY_S)
        live = await _resolve_meal_with_tools(message)
        if live is None:
            async for ev in _speak(
                "The recipe tool is switched off on this deployment, so I can only score "
                f"meals I know: {', '.join(x['name'] for x in MEALS)}."
            ):
                yield ev
            yield {"event": "unknown_meal", "reason": "tools_off"}
            yield {"event": "done"}
            return
        if live.get("call") is not None:
            yield _tool_event(live["call"])
        if live.get("failed") or not live.get("ingredients"):
            reason = live.get("failed") or "no ingredients returned"
            async for ev in _speak(f"RecipeDB could not help me with that dish — {reason}."):
                yield ev
            yield {"event": "unknown_meal", "reason": reason}
            yield {"event": "done"}
            return

    ingredients = list(live["ingredients"]) if live else [i["name"] for i in (meal or {}).get("ingredients", [])]
    meal_name = (live["dish"] if live else (meal or {}).get("name")) or "that meal"
    meal_emoji = "🔎" if live else ((meal or {}).get("emoji") or "🍽")

    yield {"event": "meal", "meal": {
        "id": (live or meal or {}).get("id"), "name": meal_name, "emoji": meal_emoji,
        "ingredients": ingredients,
        "source": "RecipeDB" if live else "demo",
        "calories": (live or {}).get("calories"),
        "region": (live or {}).get("region"),
    }}

    # --- truth layer: Python computes, agents interpret ---------------------
    # RecipeDB gives names, not nutrient columns, so a looked-up dish is scored by the
    # same gut_engine rules over its real ingredient list.
    engine_input = [{"name": n} for n in ingredients] if live else (meal or {}).get("ingredients", [])
    score, gaps, raw_metrics = compute_gut_score(engine_input)
    metrics = normalize_metrics(raw_metrics)
    yield {"event": "tool", "tool": "gut_engine.compute_score",
           "output": {"gut_score": score, "gaps": gaps, **metrics}}
    await asyncio.sleep(STATUS_DELAY_S)

    async for ev in _speak(
        f"Facts are in — gut score {score}/10. Convening the gut parliament in private…"
    ):
        yield ev
    await asyncio.sleep(STATUS_DELAY_S)

    # --- parliament (private: agents reason + vote off-stage, no per-agent stream) ---
    room = GutParliament(AGENTS)
    private = await room.convene_private(meal_name, metrics)
    yes, total = private["yes"], private["total"]

    # --- chief agent (Mixologist) delivers the single final verdict ----------
    verdict_line = (
        f"Parliament has spoken: {yes} of {total} approve. "
        + ("Your microbes are broadly happy — minor tune-ups only."
           if yes >= 6 else
           "Your microbes are revolting. Prescribing a rescue drink.")
    )
    async for ev in _speak(verdict_line):
        yield ev
    await asyncio.sleep(STATUS_DELAY_S)

    # --- the pairing: FlavorDB ranks the candidates when it is reachable ----
    pair = await _live_pairing(_hero_ingredient(ingredients))
    if pair:
        yield _tool_event(pair["result"])
        await asyncio.sleep(STATUS_DELAY_S)
        drink = {
            "name": pair["drink"], "prep": pair["prep"], "explanation": pair["explanation"],
            "shared_molecules": pair["shared_count"], "similarity_score": pair["jaccard"],
            "hero": pair["hero"], "pair_with": pair["pair_with"], "live": True,
            "shared": pair["shared"],
        }
        prescription = (
            f"Prescription: {drink['name']} — {pair['prep']} FlavorDB says {pair['hero']} and "
            f"{pair['pair_with']} share {pair['shared_count']} molecules (Jaccard "
            f"{pair['jaccard']}), the best pairing for what you ate."
        )
    else:
        drink = {**(meal or {}).get("drink", MEALS[0]["drink"]), "live": False}
        yield {"event": "tool", "tool": "pairing_engine.flavordb_lookup",
               "source": "curated", "ok": True,
               "output": {"drink": drink["name"],
                          "shared_molecules": drink["shared_molecules"],
                          "similarity_score": drink["similarity_score"],
                          "note": "FlavorDB unreachable — curated numbers"}}
        await asyncio.sleep(STATUS_DELAY_S)
        prescription = f"Prescription: {drink['name']} — {drink['prep']} {drink['explanation']}"

    async for ev in _speak(prescription):
        yield ev

    yield {"event": "result", "result": {
        "gut_score": score,
        "gaps": gaps,
        "drink": drink,
        "tally": {"yes": yes, "total": total},
        "meal": {"name": meal_name, "ingredients": ingredients,
                 "source": "RecipeDB" if live else "demo"},
    }}
    yield {"event": "done"}
