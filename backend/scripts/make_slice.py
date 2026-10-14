"""Extract the first N entities from a Wikidata JSON dump into a smaller gzip.

Used to create a smoke-test slice before committing to a multi-hour full-dump
run. The output preserves JSON-array framing so it can be fed to
load_from_dump.py (json mode) or parsed with stdlib json.

Usage (inside backend container):
  python /app/scripts/make_slice.py \
    --input  /data/wikidump/latest-all.json.gz \
    --output /tmp/slice.json.gz \
    --count  100000

Env-var equivalents: SLICE_INPUT, SLICE_OUTPUT, SLICE_COUNT.
"""

import argparse
import gzip
import logging
import os
import sys
import time

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("make_slice")


def make_slice(input_path: str, output_path: str, count: int) -> int:
    """Copy the first ``count`` entity lines from ``input_path`` to ``output_path``.

    Returns the number of entity lines actually written (may be fewer than
    ``count`` if the source is exhausted early, e.g. on a custom small dump).
    """
    if not os.path.exists(input_path):
        logger.error("Input not found: %s", input_path)
        sys.exit(1)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    start = time.time()
    written = 0
    previous: str | None = None  # buffered entity line (so we can strip the final trailing comma)

    with gzip.open(input_path, "rt", encoding="utf-8") as src, \
         gzip.open(output_path, "wt", encoding="utf-8") as dst:

        dst.write("[\n")

        for raw in src:
            s = raw.strip()
            if not s or s in ("[", "]"):
                continue
            if written >= count:
                break

            if previous is not None:
                dst.write(previous)
                dst.write("\n")
            previous = s  # hold one line back so we can strip its trailing comma
            written += 1

            if written % 10_000 == 0:
                elapsed = time.time() - start
                logger.info("  %d entities written (%.1fs)", written, elapsed)

        if previous is not None:
            if previous.endswith(","):
                previous = previous[:-1]
            dst.write(previous)
            dst.write("\n")

        dst.write("]\n")

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    logger.info(
        "Done: %d entities → %s (%.1f MB in %.1fs)",
        written, output_path, size_mb, time.time() - start,
    )
    return written


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=os.getenv("SLICE_INPUT", "/data/wikidump/latest-all.json.gz"))
    p.add_argument("--output", default=os.getenv("SLICE_OUTPUT", "/tmp/slice.json.gz"))
    p.add_argument("--count", type=int, default=int(os.getenv("SLICE_COUNT", "100000")))
    args = p.parse_args()
    make_slice(args.input, args.output, args.count)


if __name__ == "__main__":
    main()
