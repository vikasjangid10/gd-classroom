"""A multi-provider LLM fallback chain.

Import from ``facade`` and nothing else — the modules underneath it are one decision
each (which provider, what a failure means, what is allowed to serve, the walk, what to
record) and are only split up so that none of them has to know about the others.
"""

from app.infrastructure.llm.facade import (
    Answer,
    ChainConfigurationError,
    ChainExhausted,
    LlmGateway,
    LlmLanes,
    build_lanes,
)

__all__ = [
    "Answer",
    "ChainConfigurationError",
    "ChainExhausted",
    "LlmGateway",
    "LlmLanes",
    "build_lanes",
]
