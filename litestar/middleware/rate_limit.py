from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import TYPE_CHECKING, Any, Callable, Literal, cast

from litestar.datastructures import MutableScopeHeaders
from litestar.enums import ScopeType
from litestar.exceptions import TooManyRequestsException
from litestar.middleware.base import AbstractMiddleware, DefineMiddleware
from litestar.serialization import decode_json, encode_json
from litestar.utils import AsyncCallable

__all__ = ("CacheObject", "RateLimitConfig", "RateLimitMiddleware")


if TYPE_CHECKING:
    from typing import Awaitable

    from litestar import Litestar
    from litestar.connection import Request
    from litestar.stores.base import Store
    from litestar.types import ASGIApp, Message, Receive, Scope, Send, SyncOrAsyncUnion


DurationUnit = Literal["second", "minute", "hour", "day"]

DURATION_VALUES: dict[DurationUnit, int] = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


@dataclass
class CacheObject:
    """Representation of a cached object's metadata."""

    __slots__ = ("history", "reset")

    history: list[int]
    reset: int


class RateLimitMiddleware(AbstractMiddleware):
    """Rate-limiting middleware."""

    __slots__ = ("app", "check_throttle_handler", "max_requests", "unit", "request_quota", "config")

    def __init__(self, app: ASGIApp, config: RateLimitConfig) -> None:
        """Initialize ``RateLimitMiddleware``.

        Args:
            app: The ``next`` ASGI app to call.
            config: An instance of RateLimitConfig.
        """
        super().__init__(
            app=app, exclude=config.exclude, exclude_opt_key=config.exclude_opt_key, scopes={ScopeType.HTTP}
        )
        self.check_throttle_handler = cast("Callable[[Request], Awaitable[bool]] | None", config.check_throttle_handler)
        self.config = config
        self.limit_dict = config.get_rate_limit_dict()
        self.sorted_units = sorted(self.limit_dict.keys(), key=lambda d: DURATION_VALUES[d])
        self.cache_expires_in = max(DURATION_VALUES[unit] for unit in self.sorted_units)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI callable.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive function.
            send: The ASGI send function.

        Returns:
            None
        """
        app = scope["app"]
        request: Request[Any, Any, Any] = app.request_class(scope)
        store = self.config.get_store_from_app(app)
        if await self.should_check_request(request=request):
            key = self.cache_key_from_request(request=request)
            cache_histories = await self.retrieve_cached_histories(key, store)
            if self.is_too_many_request(cache_histories=cache_histories):
                raise TooManyRequestsException(
                    headers=self.create_response_headers(cache_histories=cache_histories)
                    if self.config.set_rate_limit_headers
                    else None
                )
            cache_histories = await self.set_cached_histories(key=key, cache_histories=cache_histories, store=store)
            if self.config.set_rate_limit_headers:
                send = self.create_send_wrapper(send=send, cache_histories=cache_histories)

        await self.app(scope, receive, send)  # pyright: ignore

    def create_send_wrapper(self, send: Send, cache_histories: dict[DurationUnit, CacheObject]) -> Send:
        """Create a ``send`` function that wraps the original send to inject response headers.

        Args:
            send: The ASGI send function.
            cache_histories: A :class:`dict[DurationUnit, CacheObject]`.

        Returns:
            Send wrapper callable.
        """

        async def send_wrapper(message: Message) -> None:
            """Wrap the ASGI ``Send`` callable.

            Args:
                message: An ASGI ``Message``

            Returns:
                None
            """
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                headers = MutableScopeHeaders(message)
                for key, value in self.create_response_headers(cache_histories=cache_histories).items():
                    headers.add(key, value)
            await send(message)

        return send_wrapper

    def cache_key_from_request(self, request: Request[Any, Any, Any]) -> str:
        """Get a cache-key from a ``Request``

        Args:
            request: A :class:`Request <.connection.Request>` instance.

        Returns:
            A cache key.
        """
        host = request.client.host if request.client else "anonymous"
        identifier = request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP") or host
        route_handler = request.scope["route_handler"]
        if getattr(route_handler, "is_mount", False):
            identifier += "::mount"

        if getattr(route_handler, "is_static", False):
            identifier += "::static"

        return f"{type(self).__name__}::{identifier}"

    async def retrieve_cached_histories(self, key: str, store: Store) -> dict[DurationUnit, CacheObject]:
        """Retrieve a list of time stamps for the given duration unit.

        Args:
            key: Cache key.
            store: A :class:`Store <.stores.base.Store>`

        Returns:
            An :class:`dict[DurationUnit, CacheObject]`.
        """
        now = int(time())
        histories = {}
        for unit in self.limit_dict:
            histories[unit] = CacheObject(history=[], reset=now + DURATION_VALUES[unit])

        cached_string = await store.get(key)
        cache_histories: dict[DurationUnit, CacheObject] = {}
        if cached_string:
            cache_histories = decode_json(cached_string, type_=dict[DurationUnit, CacheObject])

            for unit in self.sorted_units:
                cache_object = cache_histories[unit]
                if cache_object.reset <= now:
                    continue

                duration = DURATION_VALUES[unit]
                while cache_object.history and cache_object.history[-1] <= now - duration:
                    cache_object.history.pop()
                histories[unit] = cache_object

        return histories

    def is_too_many_request(self, cache_histories: dict[DurationUnit, CacheObject]) -> bool:
        return any(len(cache_histories[unit].history) >= self.limit_dict[unit] for unit in cache_histories)

    async def set_cached_histories(self, key: str, cache_histories: dict[DurationUnit, CacheObject], store: Store) -> dict[DurationUnit, CacheObject]:
        """Store histories extended with the current timestamp in cache and returns the extended histories.

        Args:
            key: Cache key.
            cache_histories: A :class:`dict[DurationUnit, CacheObject]`.
            store: A :class:`Store <.stores.base.Store>`

        Returns:
            The histories with current timestamp included.
        """
        extended_histories = {}
        for unit, cache_object in cache_histories.items():
            extended_histories[unit] = CacheObject(
                history=[int(time()), *cache_object.history],
                reset=cache_object.reset
            )

        await store.set(key, encode_json(extended_histories), expires_in=self.cache_expires_in)
        return extended_histories

    async def should_check_request(self, request: Request[Any, Any, Any]) -> bool:
        """Return a boolean indicating if a request should be checked for rate limiting.

        Args:
            request: A :class:`Request <.connection.Request>` instance.

        Returns:
            Boolean dictating whether the request should be checked for rate-limiting.
        """
        if self.check_throttle_handler:
            return await self.check_throttle_handler(request)
        return True

    def create_response_headers(self, cache_histories: dict[DurationUnit, CacheObject]) -> dict[str, str]:
        """Create ratelimit response headers.

        Notes:
            * see the `IETF RateLimit draft <https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/>_`

        Args:
            cache_histories:A :class:`dict[DurationUnit, CacheObject]`.

        Returns:
            A dict of http headers.
        """
        remaining_requests_list = [
            (max_requests - len(cache_histories[unit].history), unit)
            if len(cache_histories[unit].history) <= max_requests else 0
            for unit, max_requests in self.limit_dict.items()
        ]

        main_policy = min(remaining_requests_list, key=lambda x: x[0])
        main_policy_remaining_requests, main_policy_unit = main_policy

        rate_limit_policy = ", ".join([
            f"{self.limit_dict[unit]}; w={DURATION_VALUES[unit]}"
            for unit in self.sorted_units
        ])

        return {
            self.config.rate_limit_policy_header_key: rate_limit_policy,
            self.config.rate_limit_limit_header_key: str(self.limit_dict[main_policy_unit]),
            self.config.rate_limit_remaining_header_key: str(main_policy_remaining_requests),
            self.config.rate_limit_reset_header_key: str(cache_histories[main_policy_unit].reset - int(time())),
        }


@dataclass
class RateLimitConfig:
    """Configuration for ``RateLimitMiddleware``"""

    rate_limit: tuple[DurationUnit, int] = field(default=None)
    """A tuple containing a time unit (second, minute, hour, day) and quantity, e.g. ("day", 1) or ("minute", 5)."""
    rate_limits: list[tuple[DurationUnit, int]] = field(default=None, )
    """A list of tuples each containing a time unit (second, minute, hour, day) and quantity, e.g. ("day", 1) or ("minute", 5).
    If multiple tuples with the same time unit is specified, the one with minimum quantity will apply.
    Will include the tuple from `rate_limit` if specified.
    """
    exclude: str | list[str] | None = field(default=None)
    """A pattern or list of patterns to skip in the rate limiting middleware."""
    exclude_opt_key: str | None = field(default=None)
    """An identifier to use on routes to disable rate limiting for a particular route."""
    check_throttle_handler: Callable[[Request[Any, Any, Any]], SyncOrAsyncUnion[bool]] | None = field(default=None)
    """Handler callable that receives the request instance, returning a boolean dictating whether or not the request
    should be checked for rate limiting.
    """
    middleware_class: type[RateLimitMiddleware] = field(default=RateLimitMiddleware)
    """The middleware class to use."""
    set_rate_limit_headers: bool = field(default=True)
    """Boolean dictating whether to set the rate limit headers on the response."""
    rate_limit_policy_header_key: str = field(default="RateLimit-Policy")
    """Key to use for the rate limit policy header."""
    rate_limit_remaining_header_key: str = field(default="RateLimit-Remaining")
    """Key to use for the rate limit remaining header."""
    rate_limit_reset_header_key: str = field(default="RateLimit-Reset")
    """Key to use for the rate limit reset header."""
    rate_limit_limit_header_key: str = field(default="RateLimit-Limit")
    """Key to use for the rate limit limit header."""
    store: str = "rate_limit"
    """Name of the :class:`Store <.stores.base.Store>` to use"""

    def __post_init__(self) -> None:
        if self.rate_limit is None and (self.rate_limits is None or len(self.rate_limits) == 0):
            raise Exception("At least one limit should be clarified by rate_limit or rate_limits.")
        if self.check_throttle_handler:
            self.check_throttle_handler = AsyncCallable(self.check_throttle_handler)  # type: ignore

    @property
    def middleware(self) -> DefineMiddleware:
        """Use this property to insert the config into a middleware list on one of the application layers.

        Examples:
            .. code-block: python

                from litestar import Litestar, Request, get
                from litestar.middleware.rate_limit import RateLimitConfig

                # limit to 10 requests per minute, excluding the schema path
                throttle_config = RateLimitConfig(rate_limit=("minute", 10), exclude=["/schema"])

                @get("/")
                def my_handler(request: Request) -> None:
                    ...

                app = Litestar(route_handlers=[my_handler], middleware=[throttle_config.middleware])

        Returns:
            An instance of :class:`DefineMiddleware <.middleware.base.DefineMiddleware>` including ``self`` as the
            config kwarg value.
        """
        return DefineMiddleware(self.middleware_class, config=self)

    def get_store_from_app(self, app: Litestar) -> Store:
        """Get the store defined in :attr:`store` from an :class:`Litestar <.app.Litestar>` instance."""
        return app.stores.get(self.store)

    def get_rate_limit_dict(self) -> dict[DurationUnit, int]:
        """Returns a dict mapping from a duration unit to the maximum number of requests will be allowed.

        Examples:
            .. code-block: python

                config = RateLimitConfig(rate_limit=("minute", 50), rate_limits=[("minute", 100), ("second", 5), ("second", 10)])
                config.get_rate_limit_dict() # returns {"minute":50,"second":5}
        """
        limit_dict: dict[DurationUnit, int] = {}

        if self.rate_limit is not None:
            limit_dict[self.rate_limit[0]] = self.rate_limit[1]

        if self.rate_limits is not None:
            for lim in self.rate_limits:
                if lim[0] in limit_dict:
                    limit_dict[lim[0]] = min(limit_dict[lim[0]], lim[1])
                else:
                    limit_dict[lim[0]] = lim[1]

        return limit_dict
