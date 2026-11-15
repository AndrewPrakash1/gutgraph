"""
The tools a council agent may call — the ONLY door from an LLM decision to
RecipeDB and FlavorDB.

What the model gets is a tool *name* and a typed argument, never a URL, a key or a
free-form HTTP call: this module validates the arguments, runs the request through
`foodoscope` (allow-list, disk cache, 6 s timeout) and returns JSON. CocktailDB is
absent from the surface entirely, so no prompt and no injection can reach it.

Every call answers {ok, tool, args, source, data|error, ms} and never raises.
"The gateway is unreachable" is a normal answer (ok=false, source="offline"): the
agent is told to say so, not to invent a recipe.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

import foodoscope

MAX_ARG_CHARS = 60
MAX_PAIRS = 8
MAX_INGREDIENTS = 25

TOOL_SOURCES = {
    "recipe_lookup": "RecipeDB",
    "flavor_pairs": "FlavorDB",
    "shared_molecules": "FlavorDB",
}

TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "recipe_lookup",
            "description": (
                "Look up a dish in RecipeDB (a real food-composition database) and get its "
                "actual ingredient list, region, calories and diet flags. Use this whenever "
                "the user names a specific dish, instead of guessing what is in it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dish": {
                        "type": "string",
                        "description": "The dish name only, e.g. 'rajma chawal', 'masala dosa'.",
                    }
                },
                "required": ["dish"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "flavor_pairs",
            "description": (
                "Ask FlavorDB which foods share flavour molecules with an ingredient. Returns "
                "each partner with its shared-molecule count and Jaccard similarity. Use this "
                "to justify why a GutGraph drink pairs with a meal."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ingredient": {"type": "string", "description": "One ingredient, e.g. 'ginger'."},
                    "limit": {"type": "integer", "description": "How many partners, 1-8. Default 5."},
                },
                "required": ["ingredient"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shared_molecules",
            "description": (
                "The named flavour molecules two foods have in common, with a Jaccard score. "
                "Use this when you want to name the actual molecules behind a pairing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "food_a": {"type": "string", "description": "First food, e.g. 'mango'."},
                    "food_b": {"type": "string", "description": "Second food, e.g. 'turmeric'."},
                },
                "required": ["food_a", "food_b"],
            },
        },
    },
]

TOOL_NAMES: Tuple[str, ...] = tuple(t["function"]["name"] for t in TOOL_SPECS)

TOOL_PROMPT = (
    " You can call three tools: recipe_lookup(dish) reads RecipeDB; flavor_pairs(ingredient) "
    "and shared_molecules(food_a, food_b) read FlavorDB. Call them whenever the user asks about "
    "a specific dish, ingredient or pairing, so your answer carries real data. Never invent an "
    "ingredient list, a molecule name or a similarity number: if a tool returns ok=false, say the "
    "database was unreachable and answer from general knowledge instead. GutGraph's own "
    "drink names (Desi Cool, Pink Paradise, Sunkissed…) are yours, not database entries — "
    "do not look those up."
)


# FlavorDB indexes *foods*, not Indian dish words: measured live, "rajma" and "curd"
# are 404 ("entity not found with entity_alias_readable: rajma") while "kidney bean"
# and "yogurt" are real entities. Every value below was verified against the live
# alias endpoint, so this is a mapping of things that exist, not a guess.
COMMON_ALIASES = {
    "rajma": "kidney bean", "chawal": "rice", "curd": "yogurt", "dahi": "yogurt",
    "chana": "chickpea", "moong": "mung bean", "atta": "wheat", "ghee": "butter",
    "paneer": "cottage cheese", "jeera": "cumin", "haldi": "turmeric", "adrak": "ginger",
    "dhaniya": "coriander", "pudina": "mint", "imli": "tamarind", "bhindi": "okra",
    "baingan": "eggplant", "methi": "fenugreek", "saunf": "fennel", "elaichi": "cardamom",
    "dal": "lentil", "chai": "tea", "nimbu": "lemon", "kela": "banana", "aam": "mango",
    "nariyal": "coconut", "palak": "spinach", "tamatar": "tomato",
}


class ToolError(Exception):
    """Bad arguments from the model — reported back to it, never fatal."""


def _clean(value: Any, limit: int = MAX_ARG_CHARS) -> str:
    """One printable line, length-capped: nothing a model sends may bloat a URL."""
    if not isinstance(value, str):
        return ""
    text = "".join(ch for ch in value if ch.isprintable())
    return " ".join(text.split())[:limit].strip()


def _count(value: Any, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def _search_any(dish: str, limit: int = 5) -> Optional[List[Dict[str, Any]]]:
    """RecipeDB's by-title is a literal title search, measured live: "rajma chawal"
    answers 404 "No recipes found" while "rajma" returns Rajma (Kidney Bean Curry)
    and "masala dosa" returns a real dish. So: try the phrase, and only if it misses,
    try its words and merge. `None` means the gateway itself was unreachable.
    """
    words = [w for w in dish.lower().split() if len(w) > 2]
    hits = foodoscope.recipe_search(dish, limit=limit)
    if hits is None:
        return None
    if hits:
        return hits
    if not words:
        return []
    seen, merged = set(), []
    for word in words:
        for row in (foodoscope.recipe_search(word, limit=limit) or []):
            rid = row.get("id")
            if rid and rid not in seen:
                seen.add(rid)
                merged.append(row)
    return merged


def _recipe_lookup(args: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    dish = _clean(args.get("dish"))
    if not dish:
        raise ToolError("`dish` must be a non-empty dish name")
    hits = _search_any(dish)
    if hits is None:
        return None, "RecipeDB"
    if not hits:
        return {"dish": dish, "matched": None, "ingredients": [],
                "note": "RecipeDB has no dish by that title — try another name."}, "RecipeDB"
    words = [w for w in dish.lower().split() if len(w) > 2]

    def rank(row: Dict[str, Any]) -> Tuple[int, int]:
        title = (row.get("name") or "").lower()
        return (sum(1 for w in words if w in title), -abs(len(title) - len(dish)))

    best = max(hits, key=rank)
    detail = foodoscope.recipe_detail(best["id"]) if best.get("id") else None
    src = detail or best
    seen, ingredients = set(), []
    for row in (src.get("ingredients") or []):
        if not isinstance(row, dict):
            continue
        name = _clean(row.get("name"))
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        ingredients.append({"name": name,
                            "quantity": row.get("quantity"),
                            "unit": row.get("unit")})
    return {
        "dish": src.get("name") or dish,
        "asked_for": dish,
        "matched": bool(detail),
        "region": src.get("region"),
        "sub_region": src.get("sub_region"),
        "calories": src.get("calories"),
        "minutes": src.get("minutes"),
        "servings": src.get("servings"),
        "diet": src.get("diet"),
        "ingredients": ingredients[:MAX_INGREDIENTS],
        "ingredient_count": len(ingredients),
        "alternatives": [h.get("name") for h in hits if h.get("name") != src.get("name")][:4],
    }, "RecipeDB"


def _candidates(name: str) -> List[str]:
    """Every spelling worth trying, most specific first: the name itself, its alias-map
    translation, the same phrase with plural/derived words swapped, then each word."""
    low = " ".join(name.lower().split())
    words = [w for w in low.split() if len(w) > 2]
    out: List[str] = [low]
    for word in words:
        alias = COMMON_ALIASES.get(word)
        if alias:
            out.append(" ".join(alias if w == word else w for w in words))
    for word in words:
        for variant in (word.rstrip("s"), word + "s"):
            if variant != word and len(variant) > 2:
                out.append(" ".join(variant if w == word else w for w in words))
    out.extend(words)
    seen, ordered = set(), []
    for cand in out:
        if cand and cand not in seen:
            seen.add(cand)
            ordered.append(cand)
    return ordered


def _resolve_entity(name: str) -> Tuple[Optional[str], Optional[List[Dict[str, Any]]]]:
    """FlavorDB entities are foods, not dishes or Hindi words — measured live, the model
    asked for "rajma chawal" and got a 404 entity-not-found, which turned a good question
    into a dead tool call. So try the name, its alias, singular/plural, then its words,
    and report which spelling actually resolved so the agent can be precise about it.
    """
    for candidate in _candidates(name):
        rows = foodoscope.entity_molecules(candidate)
        if rows:
            return candidate, rows
    return None, None


def resolve_food(name: str) -> Optional[str]:
    """Public helper for callers that need a FlavorDB-known spelling of a food name."""
    resolved, rows = _resolve_entity(name)
    return resolved if rows else None


def _flavor_pairs(args: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    ingredient = _clean(args.get("ingredient"))
    if not ingredient:
        raise ToolError("`ingredient` must be a non-empty food name")
    limit = _count(args.get("limit"), 1, MAX_PAIRS, 5)
    rows = foodoscope.pairings(ingredient, with_similarity=min(3, limit))
    resolved = ingredient
    if not rows:
        for candidate in _candidates(ingredient):
            rows = foodoscope.pairings(candidate, with_similarity=min(3, limit))
            if rows:
                resolved = candidate
                break
    if not rows:
        raise ToolError(
            f"FlavorDB has no pairing data for '{ingredient}' — it indexes foods, not dishes "
            f"or local words. Retry with a plain ingredient name (e.g. ginger, mango)."
        )
    return {
        "ingredient": resolved,
        "asked_for": ingredient,
        "pairs": [
            {
                "food": p.get("name"),
                "category": p.get("category"),
                "shared_molecules": p.get("shared_molecules"),
                "reported_similar": p.get("reported_similar"),
                "similarity": p.get("similarity"),
            }
            for p in rows[:limit]
        ],
    }, "FlavorDB"


def _shared_molecules(args: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    a, b = _clean(args.get("food_a")), _clean(args.get("food_b"))
    if not a or not b:
        raise ToolError("`food_a` and `food_b` must both be non-empty food names")
    a_res, rows_a = _resolve_entity(a)
    b_res, rows_b = _resolve_entity(b)
    if not rows_a or not rows_b:
        missing = a if not rows_a else b
        raise ToolError(
            f"FlavorDB has no entity called '{missing}' — it indexes foods, not dishes or "
            f"local words. Retry with a plain ingredient name."
        )
    key_a = {str(r.get("pubchem_id") or r.get("name")).lower(): r.get("name") for r in rows_a}
    key_b = {str(r.get("pubchem_id") or r.get("name")).lower(): r.get("name") for r in rows_b}
    shared_keys = set(key_a) & set(key_b)
    union = set(key_a) | set(key_b)
    return {
        "food_a": a_res, "food_b": b_res,
        "asked_for": [a, b],
        "molecules_a": len(key_a), "molecules_b": len(key_b),
        "shared": sorted({key_a[k] or k for k in shared_keys})[:12],
        "shared_count": len(shared_keys),
        "jaccard": round(len(shared_keys) / len(union), 4) if union else None,
    }, "FlavorDB"


_HANDLERS = {
    "recipe_lookup": _recipe_lookup,
    "flavor_pairs": _flavor_pairs,
    "shared_molecules": _shared_molecules,
}


def execute(name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run one tool call. Always returns a dict; never raises."""
    started = time.time()
    args = args if isinstance(args, dict) else {}
    out: Dict[str, Any] = {"tool": name, "args": args}

    def done(**extra: Any) -> Dict[str, Any]:
        out.update(extra)
        out["ms"] = int((time.time() - started) * 1000)
        return out

    handler = _HANDLERS.get(str(name))
    if handler is None:
        return done(ok=False, source="tools", offline=True, error=f"unknown tool '{name}'")
    # The handler validates its arguments before it touches the network, so a model that
    # mis-addresses a tool hears about its arguments even when the gateway is off — and
    # the reason for a *lookup* failure is then stated exactly (no key vs no network).
    try:
        data, source = handler(args)
    except ToolError as exc:
        return done(ok=False, source=TOOL_SOURCES.get(str(name), "offline"), offline=False,
                    error=str(exc))
    except Exception as exc:                                   # noqa: BLE001 - reported, not raised
        return done(ok=False, source=TOOL_SOURCES.get(str(name), "offline"),
                    error=f"{type(exc).__name__}: {exc}")
    if data is None:
        if not foodoscope.enabled():
            return done(ok=False, source=TOOL_SOURCES.get(str(name), "offline"), offline=True,
                        error="FOODOSCOPE_API_KEY is not set on this server")
        return done(ok=False, source=TOOL_SOURCES.get(str(name), "offline"), offline=True,
                    error=f"{TOOL_SOURCES.get(str(name), 'gateway')} is unreachable "
                          "(FoodoScope is a campus-network host)")
    return done(ok=True, source=source, data=data)
