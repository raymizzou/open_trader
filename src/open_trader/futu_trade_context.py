from __future__ import annotations

from typing import Any


SYNC_QUERY_CONNECT_TIMEOUT_SECONDS = 10.0


class FutuTradeContextError(RuntimeError):
    """The installed futu SDK cannot create a bounded trade context."""


def create_futu_trade_context(
    *,
    host: str,
    port: int,
    filter_trdmarket: str | None = None,
) -> Any:
    """Create one trade context without allowing SDK constructor retries."""
    import futu

    context_type = getattr(futu, "OpenSecTradeContext", None)
    init_hook = getattr(context_type, "_init_connect_sync", None)
    timeout_setter = getattr(context_type, "set_sync_query_connect_timeout", None)
    ret_ok = getattr(futu, "RET_OK", None)
    if (
        not isinstance(context_type, type)
        or not callable(init_hook)
        or not callable(timeout_setter)
        or ret_ok is None
    ):
        raise FutuTradeContextError(
            "installed futu-api does not expose the required trade-context hooks"
        )

    class BoundedTradeContext(context_type):
        def _init_connect_sync(self) -> Any:
            if getattr(self, "_adapter_initialization_complete", False):
                return super()._init_connect_sync()
            self.set_sync_query_connect_timeout(
                SYNC_QUERY_CONNECT_TIMEOUT_SECONDS
            )
            try:
                result = super()._init_connect_sync()
            except BaseException as error:
                try:
                    self.close()
                except BaseException:
                    pass
                raise
            if result != ret_ok:
                error = FutuTradeContextError(
                    f"Futu trade-context initialization failed with return code {result!r}"
                )
                try:
                    self.close()
                except BaseException:
                    pass
                raise error
            self._adapter_initialization_complete = True
            return result

    kwargs: dict[str, Any] = {"host": host, "port": port}
    if filter_trdmarket is not None:
        kwargs["filter_trdmarket"] = filter_trdmarket
    return BoundedTradeContext(**kwargs)
