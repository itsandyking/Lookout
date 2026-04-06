"""PriorityWeights dataclass for configurable audit scoring.

Defines the weight parameters that control how gap counts, inventory,
and online/GMC signals combine into a priority score.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

BOUNDS: dict[str, tuple[float, float]] = {
    "gap_image": (0.0, 3.0),
    "gap_variant_images": (0.0, 3.0),
    "gap_description": (0.0, 3.0),
    "gap_type": (0.0, 2.0),
    "gap_tags": (0.0, 2.0),
    "session_scale": (10.0, 1000.0),
    "impression_scale": (100.0, 10000.0),
    "disapproval_boost": (0.0, 2.0),
}

# Ordered list of the 8 continuous parameters (for array conversion)
_CONTINUOUS_PARAMS = [
    "gap_image",
    "gap_variant_images",
    "gap_description",
    "gap_type",
    "gap_tags",
    "session_scale",
    "impression_scale",
    "disapproval_boost",
]


@dataclass
class PriorityWeights:
    """Configurable weights for priority scoring."""

    gap_image: float = 1.0
    gap_variant_images: float = 1.0
    gap_description: float = 1.0
    gap_type: float = 0.5
    gap_tags: float = 0.5
    session_scale: float = 100.0
    impression_scale: float = 1000.0
    disapproval_boost: float = 0.5
    inventory_transform: str = "linear"  # "linear", "log", "sqrt"

    def to_dict(self) -> dict:
        """Serialize all fields to a plain dict."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, d: dict) -> PriorityWeights:
        """Deserialize from dict, ignoring unknown keys."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_array(self) -> list[float]:
        """Return the 8 continuous parameters as a flat array (for optimizers)."""
        return [getattr(self, name) for name in _CONTINUOUS_PARAMS]

    @classmethod
    def from_array(cls, arr: list[float], inventory_transform: str = "linear") -> PriorityWeights:
        """Construct from a flat array of 8 floats, clamping to BOUNDS."""
        if len(arr) != len(_CONTINUOUS_PARAMS):
            raise ValueError(f"Expected {len(_CONTINUOUS_PARAMS)} values, got {len(arr)}")
        kwargs: dict = {"inventory_transform": inventory_transform}
        for name, value in zip(_CONTINUOUS_PARAMS, arr):
            lo, hi = BOUNDS[name]
            kwargs[name] = max(lo, min(hi, value))
        return cls(**kwargs)
