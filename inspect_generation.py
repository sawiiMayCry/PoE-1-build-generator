"""Developer inspection of installed PoB definitions; writes only local data."""
import json
from pathlib import Path
from pob_engine import get_worker
from services import game_context, market_data

def main():
    root = Path(__file__).resolve().parent
    metadata = get_worker(root, root / "data").request("metadata")
    (root / "data" / "pob_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    print({"gems": len(metadata["gems"]), "bases": len(metadata["bases"]), "mods": len(metadata["mods"])})
    context = game_context()
    context["pobHome"] = str(context["pobHome"])
    (root / "data" / "development_context.json").write_text(json.dumps(context), encoding="utf-8")
    (root / "data" / "development_market.json").write_text(json.dumps(market_data(context["league"])), encoding="utf-8")
    print({key: context[key] for key in ("league", "treeVersion", "officialRelease")})


if __name__ == "__main__":
    main()
