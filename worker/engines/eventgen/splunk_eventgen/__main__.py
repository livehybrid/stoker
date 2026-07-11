import argparse
import logging
import os
import sys

from splunk_eventgen.eventgen_core import EventGenerator

FILE_LOCATION = os.path.dirname(os.path.abspath(__file__))


def _get_version():
    """
    @return: Version Number
    """
    try:
        from importlib.metadata import PackageNotFoundError, distribution

        try:
            dist = distribution("splunk_eventgen")
            return dist.version
        except PackageNotFoundError:
            return "dev"
    except Exception:
        return "Unknown"


EVENTGEN_VERSION = _get_version()


def parse_args():
    """Parse command line arguments"""
    subparser_dict = {}
    parser = argparse.ArgumentParser(
        prog="Eventgen", description="Splunk Event Generation Tool"
    )
    parser.add_argument(
        "-v", "--verbosity", action="count", help="increase output verbosity"
    )
    parser.add_argument(
        "--version",
        action="version",
        default=False,
        version="%(prog)s " + EVENTGEN_VERSION,
    )
    parser.add_argument("--modinput-mode", default=False)
    parser.add_argument("--counter-output", action="store_true", default=False)
    subparsers = parser.add_subparsers(
        title="commands", help="valid subcommands", dest="subcommand"
    )
    # Generate subparser
    generate_subparser = subparsers.add_parser(
        "generate", help="Generate events using a supplied config file"
    )
    generate_subparser.add_argument(
        "configfile",
        help="Location of eventgen.conf, app folder, or name of an app in $SPLUNK_HOME/etc/apps to run",
    )
    generate_subparser.add_argument(
        "-s", "--sample", help="Run specified sample only, disabling all other samples"
    )
    generate_subparser.add_argument(
        "--keepoutput",
        action="store_true",
        help="Keep original outputMode for the sample",
    )
    generate_subparser.add_argument(
        "--devnull", action="store_true", help="Set outputMode to devnull"
    )
    generate_subparser.add_argument(
        "--modinput",
        action="store_true",
        help="Set outputMode to modinput, to see metadata",
    )
    generate_subparser.add_argument("-c", "--count", type=int, help="Set sample count")
    generate_subparser.add_argument(
        "-i", "--interval", type=int, help="Set sample interval"
    )
    generate_subparser.add_argument(
        "-b", "--backfill", help="Set time to backfill from"
    )
    generate_subparser.add_argument(
        "-e",
        "--end",
        help="Set time to end generation at or a number of intervals to run",
    )
    generate_subparser.add_argument(
        "--generators", type=int, help="Number of GeneratorWorkers (mappers)"
    )
    generate_subparser.add_argument(
        "--outputters", type=int, help="Number of OutputWorkers (reducers)"
    )
    generate_subparser.add_argument(
        "--disableOutputQueue", action="store_true", help="Disable reducer step"
    )
    generate_subparser.add_argument(
        "--multiprocess",
        action="store_true",
        help="Use multiprocesing instead of threading",
    )
    generate_subparser.add_argument(
        "--profiler", action="store_true", help="Turn on cProfiler"
    )
    generate_subparser.add_argument(
        "--generator-queue-size",
        type=int,
        default=500,
        help="the max queue size for the "
        "generator queue, timer object puts all the generator tasks into this queue, default max size is 500",
    )
    generate_subparser.add_argument(
        "--disable-logging", action="store_true", help="disable logging"
    )
    # Help subparser
    # NOTE: Keep this at the end so we can use the subparser_dict.keys() to display valid commands
    help_subparser = subparsers.add_parser("help", help="Display usage on a subcommand")
    helpstr = "Help on a specific command, valid commands are: " + ", ".join(
        list(subparser_dict.keys()) + ["help"]
    )
    help_subparser.add_argument("command", nargs="?", default="default", help=helpstr)
    # add subparsers to the subparser dict, this will be used later for usage / help statements.
    subparser_dict["generate"] = generate_subparser
    subparser_dict["help"] = help_subparser

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(2)

    args = parser.parse_args()

    if "subcommand" not in args:
        parser.print_help()
        sys.exit(2)

    if args.subcommand == "help" and args.command == "default":
        parser.print_help()
        sys.exit(0)

    if args.subcommand == "help":
        if args.command in list(subparser_dict.keys()):
            subparser_dict[args.command].print_help()
        else:
            parser.print_help()
        sys.exit(0)

    # Allow passing of a Splunk app on the command line and expand the full path before passing up the chain
    if hasattr(args, "configfile") and not os.path.exists(args.configfile):
        if "SPLUNK_HOME" in os.environ:
            if os.path.isdir(
                os.path.join(os.environ["SPLUNK_HOME"], "etc", "apps", args.configfile)
            ):
                args.configfile = os.path.join(
                    os.environ["SPLUNK_HOME"], "etc", "apps", args.configfile
                )
        else:
            args.configfile = None
    return args


def convert_verbosity_count_to_logging_level(verbosity):
    if type(verbosity) == int:
        if verbosity == 0:
            return logging.ERROR
        elif verbosity == 1:
            return logging.INFO
        elif verbosity >= 2:
            return logging.DEBUG
        else:
            return logging.DEBUG
    else:
        return logging.ERROR


def main():
    args = parse_args()
    args.verbosity = convert_verbosity_count_to_logging_level(args.verbosity)
    if args.subcommand == "generate":
        eventgen = EventGenerator(args=args)
        eventgen.start()
    sys.exit(0)


if __name__ == "__main__":
    main()
