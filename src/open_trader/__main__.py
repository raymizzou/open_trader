from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["frontend-gateway"]:
        from .frontend_gateway import main as frontend_gateway_main

        return frontend_gateway_main(args[1:])
    if args[:1] == ["account-api"]:
        from .account_api import main as account_api_main

        return account_api_main(args[1:])
    if args[:1] == ["account-api-parity"]:
        from .account_api import parity_main

        return parity_main(args[1:])
    if args[:1] == ["prediction-service"]:
        from .prediction_service import main as prediction_service_main

        return prediction_service_main(args[1:])
    from .cli import main as cli_main

    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
