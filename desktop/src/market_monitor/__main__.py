"""Allow ``python -m market_monitor``."""

import sys

if __name__ == "__main__":
    # The service never needs the CLI's collector/report imports before it can
    # bind its listener. Keep all other commands on the established CLI path.
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        from market_monitor.serve_cli import main
    else:
        from market_monitor.cli import main

    sys.exit(main())
