"""C6 — Explicit static model routing (opt-in extension).

Routing is not required for the single-upstream MVP. When enabled, the client-requested
model ID is mapped to a configured upstream by **exact name match only**.

There is no semantic task classification, no dynamic model scoring, no load balancing,
and no learned routing. A paid model is never an implicit fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pocket_harness.config import Config, RouteConfig


class RoutingError(Exception):
    """Raised when a route cannot be resolved safely."""


@dataclass(frozen=True, slots=True)
class ResolvedRoute:
    """The upstream selected for a request."""

    requested_model: str
    upstream_alias: str
    base_url: str
    model: str
    context_length: int | None
    api_key_env: str | None
    paid: bool
    source: str  # "default" | "route"

    def as_event_dict(self) -> dict[str, Any]:
        return {
            "requested_model": self.requested_model,
            "upstream_alias": self.upstream_alias,
            "upstream_model": self.model,
            "route_source": self.source,
            "paid": self.paid,
        }


class Router:
    """Resolves a client-requested model ID to a configured upstream."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def resolve(self, requested_model: str) -> ResolvedRoute:
        """Resolve ``requested_model`` to an upstream.

        When routing is disabled the default upstream is always used, regardless of the
        requested model ID.
        """
        if not self.config.routing.enabled:
            upstream = self.config.upstream
            return ResolvedRoute(
                requested_model=requested_model,
                upstream_alias="default",
                base_url=upstream.base_url,
                model=upstream.model,
                context_length=upstream.context_length,
                api_key_env=upstream.api_key_env,
                paid=False,
                source="default",
            )

        route = self._find_route(requested_model)
        if route is None:
            upstream = self.config.upstream
            return ResolvedRoute(
                requested_model=requested_model,
                upstream_alias="default",
                base_url=upstream.base_url,
                model=upstream.model,
                context_length=upstream.context_length,
                api_key_env=upstream.api_key_env,
                paid=False,
                source="default",
            )
        return self._build_route(route, requested_model)

    def _find_route(self, requested_model: str) -> RouteConfig | None:
        """Find an enabled route by exact requested-model match."""
        for route in self.config.routes.values():
            if not route.enabled:
                continue
            if route.requested_model == requested_model:
                return route
        return None

    def _build_route(self, route: RouteConfig, requested_model: str) -> ResolvedRoute:
        if route.paid and not self.config.routing.allow_paid_fallback:
            # A paid route is allowed only when it was configured explicitly and
            # routing is on. `allow_paid_fallback` governs *fallback*, not explicit
            # selection, so an explicitly configured paid route stays usable.
            pass
        base_url = route.base_url or self.config.upstream.base_url
        context_length = route.context_length or self.config.upstream.context_length
        return ResolvedRoute(
            requested_model=requested_model,
            upstream_alias=route.upstream,
            base_url=base_url,
            model=route.upstream_model or route.requested_model,
            context_length=context_length,
            api_key_env=route.api_key_env,
            paid=route.paid,
            source="route",
        )

    def remote_routes(self) -> list[RouteConfig]:
        """Return the enabled routes that point at a remote upstream."""
        remote = []
        for route in self.config.routes.values():
            if not route.enabled:
                continue
            if route.base_url and not _is_local(route.base_url):
                remote.append(route)
        return remote


def _is_local(base_url: str) -> bool:
    """Return whether a base URL points at the local machine."""
    lowered = base_url.lower()
    return (
        "127.0.0.1" in lowered
        or "localhost" in lowered
        or "0.0.0.0" in lowered
        or "[::1]" in lowered
    )
