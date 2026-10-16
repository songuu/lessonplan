from functools import lru_cache

from jsonschema import Draft202012Validator

from app.ai.assets import AssetBundle


@lru_cache(maxsize=1)
def stream_event_validator():
    # A process uses one verified bundle; a new deployment loads the next asset revision.
    return Draft202012Validator(AssetBundle().json("schemas/stream_event.schema.json"))
