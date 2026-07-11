"""python -m stoker_agent entrypoint.

Exit codes: 0 clean drain, 2 config error, 3 HEC auth failure in
standalone mode, 4 dead-man expiry.
"""

from __future__ import annotations

import logging
import os
import signal
import sys

from .agent import Agent
from .config import ConfigError, load_config


def main(argv=None):
    # type: (list) -> int
    logging.basicConfig(
        level=os.environ.get("STOKER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    try:
        config = load_config()
    except ConfigError as exc:
        sys.stderr.write("stoker: config error: %s\n" % exc)
        return 2

    agent = Agent(config)

    def _on_signal(signum, _frame):
        agent.request_drain("signal-%d" % signum)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    return agent.run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
