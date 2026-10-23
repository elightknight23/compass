"""Index the regulations corpus into ChromaDB — both chunking strategies.

Builds two collections from docs/regulations/:
  compass_regulations_naive   fixed 500-char slices
  compass_regulations         heading-aware chunks with header-path breadcrumbs

Each collection stores a hash of the corpus it was built from, so the server
can tell at startup when a regulation has been edited since.

Run after ANY corpus edit:  python scripts/index_docs.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import rag  # noqa: E402


def main() -> None:
    try:
        result = rag.build_index()
    except FileNotFoundError as e:
        sys.exit(str(e))

    print(f"{'document':<32} {'naive':>6} {'structured':>11}")
    for row in result["files"]:
        print(f"{row['source']:<32} {row['naive']:>6} {row['structured']:>11}")
    print(
        f"\nIndexed {result['naive']} naive + {result['structured']} structured "
        f"chunks into {rag.CHROMA_DIR} (corpus {result['corpus_hash']})"
    )


if __name__ == "__main__":
    main()
