"""
FoodoScope (RecipeDB / FlavorDB) client for the campus gateway.

Reads FOODOSCOPE_API_KEY + FOODOSCOPE_BASE_URL from the environment (loaded from
repo-root .env by env_loader at app start). The gateway at
http://cosylab.iiitd.edu.in:6969 is only reachable on the IIIT-D event Wi-Fi, so
every call here fails fast (short timeout) and returns None instead of hanging;
callers fall back to the local keyword/demo data.

Endpoints (per docs/STARTER_GUIDE.md):
    GET /recipe2-api/recipe-bytitle/recipeByTitle?title=...&page=1&limit=N
    GET /flavordb/food/by-alias?food_pair=<ingredient>
    GET /recipe2-api/connections/links?ingredient=...&page=1&limit=N
Auth: `Authorization: Bearer <key>` (Bearer, not Token/Basic).
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import time
from typing import Any, Dict, List, Optional

import httpx

DEFAULT_BASE_URL = "http://cosylab.iiitd.edu.in:6969"
_CACHE_DIR = pathlib.Path(__file__).resolve().parent / ".foodoscope_cache"
_TIMEOUT = httpx.Timeout(6.0, connect=4.0)

# Paths the browser-facing proxy may forward (the key never leaves the server).
PROXY_ALLOWED = ("/recipe2-api/", "/flavordb/")
COCKTAIL_PREFIX = "/cocktaildb/"


def _key() -> str:
    return os.environ.get("FOODOSCOPE_API_KEY", "").strip()


def _base() -> str:
    return (os.environ.get("FOODOSCOPE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def enabled() -> bool:
    return bool(_key())


def cocktail_enabled() -> bool:
    """CocktailDB is alcohol data and 18+ gated by the event policy.

    It stays OFF unless the team's shortlist email says CLEARED *and*
    FOODOSCOPE_COCKTAILDB=1 is set explicitly: the starter guide states that any
    use by an uncleared team, even in a demo, can lead to disqualification — and
    this product ships non-alcoholic cans.
    """
    flag = os.environ.get("FOODOSCOPE_COCKTAILDB", "").strip().lower()
    return enabled() and flag in ("1", "true", "yes")


def _cache_path(path: str, params: Dict[str, Any]) -> pathlib.Path:
    digest = hashlib.sha1(json.dumps([path, params], sort_keys=True).encode()).hexdigest()
    return _CACHE_DIR / f"{digest}.json"


def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    """One GET against the gateway; None on any failure (off-campus, auth, 4xx/5xx)."""
    if not enabled():
        return None
    if path.startswith(COCKTAIL_PREFIX) and not cocktail_enabled():
        return None                      # policy gate: never touch /cocktaildb
    params = params or {}
    cache = _cache_path(path, params)
    if cache.is_file():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    try:
        res = httpx.get(
            f"{_base()}{path}",
            params=params,
            headers={"Authorization": f"Bearer {_key()}"},
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError:
        return None
    if res.status_code != 200:
        # A 4xx is an ANSWER, not an outage. RecipeDB replies 404
        # {"success": false, "message": "No recipes found with the given title."} to a
        # literal-title miss (measured live on "rajma chawal"), and collapsing that into
        # None made a miss look exactly like a dead gateway — so no caller could tell
        # "no such dish" from "database down". 5xx and network errors still return None.
        if not 400 <= res.status_code < 500:
            return None
        try:
            data = res.json()
        except ValueError:
            return None
        _CACHE_DIR.mkdir(exist_ok=True)
        try:
            cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return data
    try:
        data = res.json()
    except ValueError:
        return None
    _CACHE_DIR.mkdir(exist_ok=True)
    try:
        cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return data


def reachable() -> bool:
    """One cheap probe; False off-campus or with a bad key."""
    if not enabled():
        return False
    data = _get("/recipe2-api/recipe/recipeofday")
    return isinstance(data, dict) or isinstance(data, list)


def _num(val: Any) -> Optional[float]:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _recipe_row(r: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise one RecipeDB row. The API uses Recipe_id / Recipe_title, and the
    by-title payload is wrapped in {success, message, data, meta} — reading only
    id/name used to make this whole path return None."""
    rid = r.get("Recipe_id") or r.get("recipe_id") or r.get("id") or r.get("_id")
    return {
        "id": str(rid) if rid is not None else None,
        "name": r.get("Recipe_title") or r.get("name") or r.get("title"),
        "region": r.get("Region"),
        "sub_region": r.get("Sub_region"),
        "continent": r.get("Continent"),
        "calories": _num(r.get("Calories")) if _num(r.get("Calories")) is not None else _num(r.get("CaloriesNum")),
        "minutes": _num(r.get("total_time")) if _num(r.get("total_time")) is not None else _num(r.get("TotalTimeNum")),
        "servings": _num(r.get("servings")),
    }


def _nutrition(recipe: Dict[str, Any]) -> Dict[str, Any]:
    """RecipeDB ships nutrients as "<Name> (g)"/"(kcal)" columns; keep them."""
    out: Dict[str, Any] = {}
    for key, val in recipe.items():
        if not isinstance(key, str) or ("(g)" not in key and "(kcal)" not in key):
            continue
        label = key.split("(")[0].strip().lower().replace(", by difference", "")
        out[label] = _num(val)
    return out


def recipe_search(title: str, limit: int = 5) -> Optional[List[Dict[str, Any]]]:
    """RecipeDB by-title search -> [{id, name, region, calories, minutes}].

    Two different failures, kept apart: `[]` means the gateway answered and has no
    recipe by that title (a literal search — measured live, "rajma chawal" is a 404
    while "rajma" hits), `None` means the gateway itself was unreachable. Callers
    that only need "did I get rows" can treat them the same; the tool layer cannot,
    because it has to tell a model "no such dish" vs "database down".
    """
    data = _get(
        "/recipe2-api/recipe-bytitle/recipeByTitle",
        {"title": title, "page": 1, "limit": limit},
    )
    if data is None:
        return None
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("data") or data.get("recipes") or data.get("results") or []
    else:
        rows = []
    out = []
    for r in (rows if isinstance(rows, list) else []):
        if isinstance(r, dict):
            row = _recipe_row(r)
            if row.get("name"):
                out.append(row)
    return out


def recipe_detail(recipe_id: str) -> Optional[Dict[str, Any]]:
    """Full recipe: nutrition + ingredient list + method. None when unreachable."""
    data = _get(f"/recipe2-api/search-recipe/{recipe_id}")
    if not isinstance(data, dict):
        return None
    body = data.get("data") if isinstance(data.get("data"), dict) else data
    recipe = body.get("recipe") if isinstance(body.get("recipe"), dict) else body
    if not isinstance(recipe, dict):
        return None
    out = _recipe_row(recipe)
    out["nutrition"] = _nutrition(recipe)
    out["diet"] = {d: recipe.get(d) == "1.0"
                   for d in ("vegan", "lacto_vegetarian", "pescetarian")}
    out["ingredients"] = [
        {
            "name": (i.get("ingredient") or "").strip(),
            "phrase": (i.get("ingredient_Phrase") or "").strip() or None,
            "quantity": i.get("quantity"),
            "unit": i.get("unit"),
        }
        for i in (body.get("ingredients") or [])
        if isinstance(i, dict) and (i.get("ingredient") or "").strip()
    ]
    steps = body.get("instructions") or []
    if steps and isinstance(steps[0], dict):
        out["steps"] = (steps[0].get("steps") or "").strip()
    return out


def _walk_lists(obj: Any) -> List[Dict[str, Any]]:
    """Every dict found anywhere in the payload.

    The gateway wraps rows in different envelopes ({data: [...]}, a bare list, or
    a top-level {entityId, topSimilarEntities: [...]}), so callers should not have
    to know which. NOTE: this must *collect* the dicts — the earlier version only
    recursed and returned [], which silently disabled the whole pairing path.
    """
    found: List[Dict[str, Any]] = []
    if isinstance(obj, dict):
        found.append(obj)
        for val in obj.values():
            found += _walk_lists(val)
    elif isinstance(obj, list):
        for item in obj:
            found += _walk_lists(item)
    return found


def _pair_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalise one FlavorDB pairing row.

    The live rows are {entityName, category, similarMolecules, wikipedia}; the
    older key spellings are kept so a gateway change cannot silence the panel
    again (reading only `name`/`shared_molecules` did exactly that).
    """
    name = (
        row.get("entityName") or row.get("food") or row.get("name") or row.get("pair")
        or row.get("paired_with") or row.get("ingredient") or row.get("alias")
    )
    if not name:
        return None
    shared = (
        row.get("similarMolecules") or row.get("shared_molecules")
        or row.get("shared") or row.get("common_molecules")
    )
    if isinstance(shared, list):
        shared = len(shared)
    sim = None
    for k in ("similarity", "jaccard", "score", "similarity_score"):
        if isinstance(row.get(k), (int, float)):
            sim = float(row[k])
            break
    return {
        "name": str(name),
        "category": row.get("category"),
        "shared_molecules": shared if isinstance(shared, (int, float)) else None,
        "similarity": round(sim, 4) if sim is not None else None,
        "entity_id": row.get("entityId") or row.get("entity_id"),
    }


def molecule_ids_by_id(entity_id: Any) -> Optional[set]:
    """The FlavorDB molecule set for one entity id ({pubchem_id, ...}).

    This is the primitive a Jaccard actually needs. Read the real payload shape:
    GET /flavordb/entities/by-id/333/molecules-compact ->
        [{"pubchem_id": 86752, "common_name": "4-Menthen-8-ol"}, ...]
    """
    if entity_id is None:
        return None
    data = _get(f"/flavordb/entities/by-id/{entity_id}/molecules-compact")
    if not isinstance(data, list):
        return None
    ids = set()
    for row in data:
        if isinstance(row, dict):
            key = row.get("pubchem_id") or row.get("id") or row.get("common_name")
            if key is not None:
                ids.add(str(key))
    return ids or None


def molecule_count_by_id(entity_id: Any) -> Optional[int]:
    """How many flavour molecules FlavorDB has for one entity id."""
    ids = molecule_ids_by_id(entity_id)
    return len(ids) if ids else None


def _first_entity_id(payload: Any) -> Optional[Any]:
    """Dig a *numeric* entity id out of whichever wrapper the gateway used.

    FlavorDB answers entity lookups as {"content": [{entity_id: 339, _id: "<mongo>"}]}
    — the Mongo `_id` is not what molecules-compact wants, so entity_id comes first
    and "content" is one of the wrappers we unwrap.
    """
    KEYS = ("entity_id", "entityId", "id", "_id")

    def from_row(row: Dict[str, Any]) -> Optional[Any]:
        for k in KEYS:
            if row.get(k) is not None:
                return row[k]
        return None

    def from_list(items: Any) -> Optional[Any]:
        for c in items if isinstance(items, list) else []:
            if isinstance(c, dict):
                got = from_row(c)
                if got is not None:
                    return got
        return None

    if isinstance(payload, dict):
        got = from_row(payload)
        if got is not None:
            return got
        for key in ("content", "data", "entities", "results"):
            got = from_list(payload.get(key))
            if got is not None:
                return got
    elif isinstance(payload, list):
        return from_list(payload)
    return None


def _entity_molecule_ids(entity: str) -> Optional[set]:
    """Molecule set for one entity *name* (two cached hops: alias -> id -> molecules)."""
    ent = (entity or "").strip()
    if not ent:
        return None
    alias = _get("/flavordb/entities/by-entity-alias-readable",
                 {"entity_alias_readable": ent, "page": 0, "size": 1})
    return molecule_ids_by_id(_first_entity_id(alias))


def _entity_molecule_rows(entity: str) -> Optional[List[Dict[str, Any]]]:
    """FlavorDB molecule rows for an entity *name* -> [{name, pubchem_id}].

    Same two cached hops as `_entity_molecule_ids`, but it keeps the common_name so
    an agent can cite "gingerol" instead of a number.
    """
    ent = (entity or "").strip()
    if not ent:
        return None
    alias = _get("/flavordb/entities/by-entity-alias-readable",
                 {"entity_alias_readable": ent, "page": 0, "size": 1})
    eid = _first_entity_id(alias)
    if eid is None:
        return None
    data = _get(f"/flavordb/entities/by-id/{eid}/molecules-compact")
    if not isinstance(data, list):
        return None
    rows: List[Dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        name = row.get("common_name") or row.get("name")
        pub = row.get("pubchem_id") or row.get("id")
        if name or pub is not None:
            rows.append({"name": str(name) if name else None,
                         "pubchem_id": str(pub) if pub is not None else None})
    return rows or None


def entity_molecules(entity: str, limit: int = 400) -> Optional[List[Dict[str, Any]]]:
    """Named molecule list for one FlavorDB entity (None = unreachable/no entity)."""
    rows = _entity_molecule_rows(entity)
    return rows[:limit] if rows else None


def entity_molecule_count(entity: str) -> Optional[int]:
    """Molecule count for one FlavorDB entity name (cached)."""
    ids = _entity_molecule_ids(entity)
    return len(ids) if ids else None


def jaccard(shared: Optional[float], a: Optional[float], b: Optional[float]) -> Optional[float]:
    """Jaccard similarity over molecule sets: None unless all three inputs are real."""
    if shared is None or a is None or b is None:
        return None
    denom = float(a) + float(b) - float(shared)
    if denom <= 0:
        return None
    return round(float(shared) / denom, 4)


def pairings(ingredient: str, with_similarity: int = 3) -> Optional[List[Dict[str, Any]]]:
    """FlavorDB food-pair for an ingredient -> [{name, category, shared_molecules,
    reported_similar, similarity}] or None.

    Two different numbers, deliberately kept apart:
      * `reported_similar` is FlavorDB's own `similarMolecules` field, passed
        through untouched.
      * `shared_molecules` / `similarity` are RECOMPUTED here as a real set
        intersection and Jaccard over both entities' molecule sets
        (molecules-compact), for the first `with_similarity` partners.
    Why recompute: the reported field is not a set intersection. Measured live,
    mango reports 182 "similar" molecules against tea, which only has 85
    molecules — so treating it as |A ∩ B| yields Jaccard values above 1. The
    intersection is cheap (2 cached calls per partner) and never impossible.
    """
    data = _get("/flavordb/food/by-alias", {"food_pair": ingredient})
    if data is None:
        return None
    out: List[Dict[str, Any]] = []
    for row in _walk_lists(data):
        norm = _pair_row(row)
        if norm:
            out.append(norm)
        if len(out) >= 8:
            break
    if not out:
        return None
    src_id = data.get("entityId") if isinstance(data, dict) else None
    src_mols = molecule_ids_by_id(src_id)
    if src_mols:
        for row in out[:max(0, with_similarity)]:
            partner = _entity_molecule_ids(row["name"])
            if not partner:
                continue
            inter = len(src_mols & partner)
            union = len(src_mols | partner)
            row["reported_similar"] = row.get("shared_molecules")
            row["shared_molecules"] = inter
            row["similarity"] = round(inter / union, 4) if union else None
    return out


def cocktail_search(query: str, limit: int = 5) -> Optional[List[Dict[str, Any]]]:
    """GATED CocktailDB read: returns None unless the team is CLEARED.

    Only ever used as flavour-architecture inspiration for our non-alcoholic
    cans; the shipped product contains no alcohol. The gate lives in _get() too,
    so an uncleared build cannot make this call by accident.
    """
    if not cocktail_enabled():
        return None
    data = _get("/cocktaildb/cocktailRecipe-search/myBar",
                {"page": 1, "page_size": limit})
    if data is None:
        return None
    rows = data if isinstance(data, list) else _walk_lists(data)
    out = []
    for r in rows[:limit]:
        name = r.get("strDrink") or r.get("name") or r.get("cocktail_name") or r.get("Cocktail_name")
        if name:
            out.append({"name": str(name), "id": r.get("idDrink") or r.get("id") or r.get("_id")})
    return out or None
