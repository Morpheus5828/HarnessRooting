
from __future__ import annotations

import argparse
import os
import sys

from harnessopt.ui.controller import launch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="HarnessOpt - cheminement de harnais helicoptere")
    parser.add_argument("--debug", action="store_true",
                        help="traces detaillees en console")
    parser.add_argument("--demo", action="store_true",
                        help="cas de demonstration sans interface")
    parser.add_argument("--out", default="harness_demo.png",
                        help="figure produite par --demo")
    args = parser.parse_args(argv)

    if args.debug:
        os.environ["HARNESSOPT_DEBUG"] = "1"
    from harnessopt import log as applog
    if args.debug:
        applog.set_level(applog.DEBUG)


    launch()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
