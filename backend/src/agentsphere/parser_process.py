"""Private stdin/stdout parser protocol. No database, storage key or credentials."""

import json
import logging
import math
import os
import sys

from agentsphere.process_limits import apply_child_limits


def main():
    output_limit = 8_000_000
    try:
        header = sys.stdin.buffer.readline(16_384)
        if not header.endswith(b"\n"):
            raise ValueError
        request = json.loads(header)
        size = request["size"]
        if type(size) is not int or not 1 <= size <= 10_000_000:
            raise ValueError
        memory = request["memory_mb"]
        timeout = request["limits"]["timeout_seconds"]
        if type(memory) is not int or not 128 <= memory <= 2048:
            raise ValueError
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError
        apply_child_limits(memory, math.ceil(timeout), int(sys.argv[1]))
        # Do not expose parser exceptions, original text, metadata or stderr.
        logging.disable(logging.CRITICAL)
        from agentsphere.extraction import ExtractionError, ExtractionLimits, extract_document

        limits = ExtractionLimits.model_validate(request["limits"])
        output_limit = request["output_limit"]
        if type(output_limit) is not int or not 65_536 <= output_limit <= 32_000_000:
            raise ValueError
        data = sys.stdin.buffer.read(size + 1)
        if len(data) != size:
            raise ValueError
        try:
            result = extract_document(data, request["content_type"], limits)
            response = {"result": result.model_dump()}
        except ExtractionError as error:
            response = {"error": error.code, "retryable": error.retryable}
    except MemoryError:
        response = {"error": "parser_memory_limit", "retryable": False}
    except BaseException:
        response = {"error": "parser_failed", "retryable": False}
    encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > output_limit:
        encoded = b'{"error":"parser_output_limit","retryable":false}'
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    # Deliberately independent of Settings: do not read backend/.env in this child.
    os.environ.clear()
    main()
