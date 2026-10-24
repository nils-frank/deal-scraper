"""Command line: `deals run-watchlist` (the Docker image's default command) and
`deals find <query> [--when now|week|upcoming]`.

Configuration comes from the environment, see `.env.example`. `find` needs only
DEAL_SCRAPER_PLZ and DB_PATH; `run-watchlist` also needs the Home Assistant values.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from deals import marktguru, query, store, watchlist_run
from deals.schema import utcnow

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deals", description="Grocery flyer offers from Marktguru.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run-watchlist", help="match the Home Assistant watchlist, update its to-dos and push new offers")
    find = commands.add_parser("find", help="show the offers for a product, grouped and cheapest first")
    find.add_argument("query", nargs="+", help='what to look for, e.g. "Butter"')
    find.add_argument("--when", choices=query.WHEN_CHOICES, default="now",
                      help="now: valid today (on a Sunday: Monday); week: the next 7 days; upcoming: starts later")
    output = find.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="print the result as JSON")
    output.add_argument("--speech", action="store_true", help="print the short German answer for Home Assistant")
    find.add_argument("-v", "--verbose", action="store_true", help="log Marktguru and store activity to stderr")
    args = parser.parse_args(argv)

    if args.command == "run-watchlist":
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
        return watchlist_run.run()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format=LOG_FORMAT)
    plz = os.environ.get("DEAL_SCRAPER_PLZ")
    if not plz:
        print("deals find: set DEAL_SCRAPER_PLZ to the postal code to search", file=sys.stderr)
        return 2
    with store.open_store(os.environ.get("DB_PATH", store.DEFAULT_DB_PATH)) as conn:
        client = marktguru.Client(conn, plz, clock=utcnow)
        result = query.find_offers(client, " ".join(args.query), args.when)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    elif args.speech:
        print(query.format_speech(result, utcnow()))
    else:
        print(query.format_text(result, utcnow()))
    return 0
