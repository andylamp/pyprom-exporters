# Copyright (c) 2026 pyprom-exporters contributors
# SPDX-License-Identifier: Apache-2.0

"""Keep the original module invocation available; prefer ``uv run benchmark``."""

from pyprom_exporters.benchmarks.scalability import main

if __name__ == "__main__":
    main()
