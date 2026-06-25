import json
import os

PERSONAS_DIR = os.path.join(os.path.dirname(__file__), "..", "personas")
DEFAULT_PERSONA = "simple_scheduling"


def load_persona(name: str) -> dict:
    path = os.path.join(PERSONAS_DIR, f"{name}.json")
    with open(path) as f:
        return json.load(f)


def list_personas() -> list:
    return sorted(fname[:-5] for fname in os.listdir(PERSONAS_DIR) if fname.endswith(".json"))
